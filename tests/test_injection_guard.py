"""Comprehensive SQL injection test suite â€” verifies the SQL guard blocks
all attack vectors BEFORE they reach the real SP.

This test uses a DUMMY SP mock (no real database, no real API calls).
It verifies:
1. SQL Guard Layer 1-5 blocks all known injection patterns
2. _build_p() produces safe SP parameters (no dangerous content in AIWhereClause)
3. Filter value sanitization blocks injection via filter inputs
4. The full pipeline (guard â†’ _build_p â†’ SP params) never lets dangerous
   content through in ANY field

Run:  python tests\test_injection_guard.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.sql_guard import validate_where_clause
from app.services.real_api_client import _build_p, _sanitize_filter_value, _xml_escape
from app.services.intent import IntentSpec

REPORT_KEY = "sales_report"


def _make_spec(metric="Amount", agg="sum", dim="", limit=0):
    return IntentSpec(
        report_key=REPORT_KEY, intent="test",
        metric_key=metric, aggregation=agg, dimension=dim,
        sort="desc", limit=limit, unit="currency",
        is_field_metric=False, clear_filters=[], override_filters={},
    )


# ============================================================
# Attack vectors â€” every known SQL injection pattern
# ============================================================

ATTACK_VECTORS = [
    # --- Stacked queries ---
    ("; DROP TABLE users", "stacked DROP TABLE"),
    ("'; DROP TABLE users; --", "stacked DROP with quote escape"),
    ("DI.Metal_Type_Name='gold'; DROP TABLE Stockmanagement_dcb", "stacked DROP after legit condition"),
    ("1=1; DELETE FROM users", "stacked DELETE"),
    ("1=1; UPDATE users SET role='admin'", "stacked UPDATE"),
    ("1=1; INSERT INTO users VALUES(1,'hack')", "stacked INSERT"),
    ("1=1; TRUNCATE TABLE users", "stacked TRUNCATE"),
    ("1=1; ALTER TABLE users ADD col2 INT", "stacked ALTER"),
    ("1=1; CREATE TABLE hack(id INT)", "stacked CREATE"),
    ("1=1; GRANT SELECT ON users TO public", "stacked GRANT"),
    ("1=1; REVOKE SELECT ON users TO public", "stacked REVOKE"),
    ("1=1; MERGE INTO users USING hack ON 1=1", "stacked MERGE"),

    # --- System access ---
    ("; EXEC xp_cmdshell('format c:')", "xp_cmdshell"),
    ("DI.col = 1; EXEC master..xp_cmdshell('dir')", "xp_cmdshell via EXEC"),
    ("; SP_EXECUTESQL('DROP TABLE users')", "SP_EXECUTESQL"),
    ("DI.col = 1; SP_OACREATE('Scripting.FileSystemObject')", "SP_OACREATE"),
    ("DI.col = 1; SP_OAMETHOD(@obj,'Run','cmd')", "SP_OAMETHOD"),
    ("DI.col = 1; SP_OADESTROY(@obj)", "SP_OADESTROY"),
    ("OPENROWSET('SQLNCLI','server';'sa';'pwd','SELECT * FROM users')", "OPENROWSET"),
    ("OPENDATASOURCE('SQLNCLI','server';'sa';'pwd')...users", "OPENDATASOURCE"),
    ("OPENQUERY(linkedserver,'SELECT * FROM users')", "OPENQUERY"),
    ("DI.col = 1; SHUTDOWN", "SHUTDOWN"),
    ("DI.col = 1; KILL 54", "KILL process"),

    # --- UNION injection ---
    ("1=1 UNION SELECT password FROM users", "UNION SELECT"),
    ("1=1 UNION ALL SELECT username,password FROM users", "UNION ALL"),
    ("DI.col = 1 UNION SELECT * FROM sys.tables", "UNION sys.tables"),

    # --- Subquery injection ---
    ("DI.col = (SELECT TOP 1 password FROM users)", "scalar subquery"),
    ("DI.col IN (SELECT id FROM hack)", "IN subquery (non-allowed table)"),
    ("DI.col = 1 AND EXISTS(SELECT * FROM users)", "EXISTS subquery"),

    # --- Comment injection ---
    ("-- comment injection", "line comment"),
    ("DI.col = 1 /* block comment */", "block comment"),
    ("DI.col = 1 --", "trailing comment"),
    ("DI.col = 1 /*", "unterminated block comment"),

    # --- Information schema / system tables ---
    ("DI.col IN (SELECT name FROM INFORMATION_SCHEMA.TABLES)", "INFORMATION_SCHEMA"),
    ("DI.col IN (SELECT name FROM sysobjects)", "sysobjects"),
    ("DI.col IN (SELECT name FROM syscolumns)", "syscolumns"),

    # --- Type casting / encoding ---
    ("DI.col::int = 1", "double-colon cast"),
    ("CAST('1'' OR ''1''=''1' AS INT)", "CAST injection"),
    ("CONVERT(INT, '1 OR 1=1')", "CONVERT injection"),

    # --- Time-based blind injection ---
    ("WAITFOR DELAY '0:0:10'", "WAITFOR DELAY"),
    ("DI.col = 1; WAITFOR DELAY '0:0:10'", "stacked WAITFOR"),

    # --- Dangerous operators ---
    ("DI.col INTO #temp", "INTO temp table"),
    ("DI.col = 1 OUTPUT inserted.*", "OUTPUT clause"),
    ("DECLARE @c CURSOR FOR SELECT * FROM users", "CURSOR"),
    ("FETCH NEXT FROM @c", "FETCH"),

    # --- Excessive length (DoS) ---
    ("A" * 3000, "DoS via excessive length"),

    # --- Computed column injection (should be rejected) ---
    ("DI.GoldWt > 10", "computed column in ai_where"),
    ("DI.GoldAmt > 1000", "computed metric in ai_where"),
    ("DI.SilverWt BETWEEN 10 AND 50", "computed silver weight"),

    # --- Unauthorized columns ---
    ("DI.secret_column = 'admin'", "unknown column"),
    ("DI.password = 'admin'", "password column"),
    ("DI.user_role = 'admin'", "role column"),

    # --- Null byte injection ---
    ("DI.col = 1\x00; DROP TABLE users", "null byte + stacked query"),
]


# ============================================================
# Filter value injection vectors
# ============================================================

FILTER_ATTACK_VALUES = [
    "gold'; DROP TABLE users; --",
    "gold; DELETE FROM users",
    "gold/*comment*/",
    "gold\x00; EXEC xp_cmdshell('dir')",
    "1 OR 1=1",
    "1; SHUTDOWN",
    "1' UNION SELECT password FROM users--",
    "gold' OR '1'='1",
    "1; WAITFOR DELAY '0:0:10'",
    "1; INSERT INTO users VALUES(1,'hack')",
]


# ============================================================
# Tests
# ============================================================

def test_sql_guard_direct():
    """Test 1: SQL Guard directly blocks all attack vectors."""
    print("=" * 80)
    print("TEST 1: SQL Guard Direct â€” Attack Vector Blocking")
    print("=" * 80)
    passed = 0
    failed = 0

    for where_clause, desc in ATTACK_VECTORS:
        safe_clause, reason = validate_where_clause(where_clause, REPORT_KEY)
        blocked = (safe_clause == "")  # blocked = empty clause returned

        # All attack vectors should be blocked (empty safe_clause)
        # Exception: empty input returns empty (not an attack)
        if blocked:
            passed += 1
            print(f"  PASS | BLOCKED | {desc}")
        else:
            failed += 1
            print(f"  FAIL | ALLOWED | {desc} | clause: {safe_clause[:60]}")

    print(f"\n  Result: {passed} blocked, {failed} allowed (should be 0)")
    return failed == 0


def test_legitimate_queries_pass():
    """Test 2: Legitimate WHERE clauses are NOT blocked."""
    print("\n" + "=" * 80)
    print("TEST 2: Legitimate Queries Pass Through")
    print("=" * 80)

    legit_queries = [
        ("DI.Metal_Type_Name = 'gold'", "simple equality"),
        ("DI.Metal_Type_Name = 'gold' AND DI.Discount > 1000", "compound AND"),
        ("DI.designno LIKE '%diamond%'", "LIKE pattern"),
        ("DI.Metal_Type_Name IN ('gold', 'silver')", "IN list"),
        ("DI.isWithHallMark = 1", "hallmark flag"),
        ("DI.Discount > 1000", "numeric comparison"),
        ("DI.Metal_Type_Name = 'gold' OR DI.Metal_Type_Name = 'silver'", "OR condition"),
        ("DI.grosswt BETWEEN 10 AND 50", "BETWEEN"),
        ("DI.entrydate > '2024-01-01'", "date comparison"),
        ("DI.Metal_Type_Name IS NOT NULL", "IS NOT NULL"),
        ("DI.categoryname = 'Ring' AND DI.Metal_Type_Name = 'gold'", "multi-filter"),
        ("DI.mastermanagement_goldtypename LIKE '%18K%'", "gold purity LIKE"),
        ("DI.mastermanagement_goldtypename LIKE '%22K%' AND DI.StockDocumentNo='JS4'", "purity + invoice"),
        ("DI.isWithHallMark = 1 AND DI.Metal_Type_Name = 'gold'", "hallmark + metal"),
        ("DI.Stockmanagement_QCStatusname = 'Pass'", "QC status"),
    ]

    passed = 0
    failed = 0
    for where_clause, desc in legit_queries:
        safe_clause, reason = validate_where_clause(where_clause, REPORT_KEY)
        allowed = (safe_clause != "")

        if allowed:
            passed += 1
            print(f"  PASS | ALLOWED | {desc}")
        else:
            failed += 1
            print(f"  FAIL | BLOCKED | {desc} | reason: {reason}")

    print(f"\n  Result: {passed} allowed, {failed} blocked (should be 0)")
    return failed == 0


def test_build_p_no_injection():
    """Test 3: _build_p() never puts dangerous content in SP parameters."""
    print("\n" + "=" * 80)
    print("TEST 3: _build_p() â€” SP Parameters Safe After Guard")
    print("=" * 80)

    # Dangerous patterns that must NEVER appear in any SP parameter
    DANGEROUS_PATTERNS = [
        r"DROP\s+TABLE",
        r"DELETE\s+FROM",
        r"INSERT\s+INTO",
        r"UPDATE\s+.*SET",
        r"TRUNCATE",
        r"ALTER\s+TABLE",
        r"CREATE\s+TABLE",
        r"EXEC\b",
        r"SP_EXECUTESQL",
        r"XP_CMDSHELL",
        r"OPENROWSET",
        r"OPENDATASOURCE",
        r"SHUTDOWN",
        r"KILL\s+\d",
        r"UNION\s+SELECT",
        r"INFORMATION_SCHEMA",
        r"sysobjects",
        r"syscolumns",
        r"WAITFOR",
        r"MERGE\s+INTO",
        r"GRANT\s+",
        r"REVOKE\s+",
    ]

    passed = 0
    failed = 0

    for attack_clause, desc in ATTACK_VECTORS:
        # Run the attack through the guard first (like the real flow does)
        safe_clause, reason = validate_where_clause(attack_clause, REPORT_KEY)

        # Build SP params with the (now sanitized) clause
        spec = _make_spec()
        try:
            p_json = _build_p(REPORT_KEY, spec, {}, safe_clause)
            p = json.loads(p_json)
        except Exception as e:
            failed += 1
            print(f"  FAIL | _build_p crashed | {desc} | {e}")
            continue

        # Check ALL string fields in the SP params for dangerous patterns
        all_values = " ".join(str(v) for v in p.values() if isinstance(v, str))
        all_values_upper = all_values.upper()

        found_dangerous = False
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, all_values_upper, re.IGNORECASE):
                found_dangerous = True
                failed += 1
                print(f"  FAIL | DANGER in SP params | {desc} | pattern: {pattern}")
                print(f"       AIWhereClause: {p.get('AIWhereClause', '')[:100]}")
                break

        if not found_dangerous:
            passed += 1
            print(f"  PASS | SAFE | {desc} | AIWhereClause={p.get('AIWhereClause', '')[:50]}")

    print(f"\n  Result: {passed} safe, {failed} dangerous (should be 0)")
    return failed == 0


def test_filter_value_sanitization():
    """Test 4: Filter value sanitization blocks injection via filter inputs."""
    print("\n" + "=" * 80)
    print("TEST 4: Filter Value Sanitization")
    print("=" * 80)

    DANGEROUS_IN_FILTER = [
        r"DROP\s+TABLE",
        r"DELETE\s+FROM",
        r"INSERT\s+INTO",
        r"EXEC\b",
        r"XP_CMDSHELL",
        r"UNION\s+SELECT",
        r"WAITFOR",
        r"SHUTDOWN",
    ]

    passed = 0
    failed = 0

    for attack_val in FILTER_ATTACK_VALUES:
        sanitized = _sanitize_filter_value(attack_val)
        sanitized_upper = sanitized.upper()

        found_dangerous = False
        for pattern in DANGEROUS_IN_FILTER:
            if re.search(pattern, sanitized_upper, re.IGNORECASE):
                found_dangerous = True
                break

        # Also check semicolons and comments are removed
        if ";" in sanitized or "--" in sanitized or "/*" in sanitized:
            found_dangerous = True

        if found_dangerous:
            failed += 1
            print(f"  FAIL | DANGER | input={attack_val[:40]} | output={sanitized[:40]}")
        else:
            passed += 1
            print(f"  PASS | SAFE | input={attack_val[:30]} | output={sanitized[:30]}")

    print(f"\n  Result: {passed} safe, {failed} dangerous (should be 0)")
    return failed == 0


def test_full_pipeline():
    """Test 5: Full pipeline â€” attack in ai_where â†’ guard â†’ _build_p â†’ SP params."""
    print("\n" + "=" * 80)
    print("TEST 5: Full Pipeline - Guard -> _build_p -> SP Params")
    print("=" * 80)

    # Simulate the real flow: LLM generates a malicious ai_where,
    # the guard validates it, then _build_p builds the SP params
    pipeline_attacks = [
        # (attack_ai_where, filters, description)
        ("DI.Metal_Type_Name='gold'; DROP TABLE users", {}, "DROP TABLE after legit filter"),
        ("DI.Metal_Type_Name='gold'; EXEC xp_cmdshell('dir')", {}, "xp_cmdshell after legit filter"),
        ("1=1 UNION SELECT password FROM users", {}, "UNION injection"),
        ("DI.col = (SELECT TOP 1 password FROM users)", {}, "scalar subquery"),
        ("DI.Metal_Type_Name='gold'--", {}, "comment injection"),
        ("DI.Metal_Type_Name='gold'/*", {}, "block comment"),
        ("DI.Metal_Type_Name='gold'; SHUTDOWN", {}, "SHUTDOWN"),
        ("DI.Metal_Type_Name='gold'; INSERT INTO users VALUES(1,'x')", {}, "INSERT"),
        ("DI.Metal_Type_Name='gold'; DELETE FROM users", {}, "DELETE"),
        ("DI.Metal_Type_Name='gold'; ALTER TABLE users ADD x INT", {}, "ALTER"),
        ("WAITFOR DELAY '0:0:10'", {}, "time-based blind"),
        ("DI.GoldWt > 10", {}, "computed column (should be rejected)"),
        ("DI.password = 'admin'", {}, "unauthorized column"),
        ("A" * 3000, {}, "DoS via length"),
    ]

    DANGEROUS_PATTERNS = [
        r"DROP\s+TABLE", r"DELETE\s+FROM", r"INSERT\s+INTO", r"EXEC\b",
        r"XP_CMDSHELL", r"UNION\s+SELECT", r"SHUTDOWN", r"WAITFOR",
        r"ALTER\s+TABLE", r"CREATE\s+TABLE", r"TRUNCATE", r"MERGE\s+INTO",
    ]

    passed = 0
    failed = 0

    for attack_where, filters, desc in pipeline_attacks:
        # Step 1: Guard validates the WHERE clause
        safe_where, reason = validate_where_clause(attack_where, REPORT_KEY)

        # Step 2: _build_p builds SP params with the safe clause
        spec = _make_spec()
        try:
            p_json = _build_p(REPORT_KEY, spec, filters, safe_where)
            p = json.loads(p_json)
        except Exception as e:
            failed += 1
            print(f"  FAIL | _build_p crashed | {desc} | {e}")
            continue

        # Step 3: Verify no dangerous content in ANY SP param field
        ai_where_out = p.get("AIWhereClause", "")
        all_values = " ".join(str(v) for v in p.values() if isinstance(v, str))
        all_upper = all_values.upper()

        found_dangerous = False
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, all_upper, re.IGNORECASE):
                found_dangerous = True
                break

        if found_dangerous:
            failed += 1
            print(f"  FAIL | DANGER leaked to SP | {desc}")
            print(f"       AIWhereClause: {ai_where_out[:80]}")
        else:
            passed += 1
            status = "BLOCKED" if safe_where == "" else "ALLOWED"
            print(f"  PASS | {status} | {desc} | AIWhereClause={ai_where_out[:40]}")

    print(f"\n  Result: {passed} safe, {failed} dangerous (should be 0)")
    return failed == 0


def test_xml_escape_safety():
    """Test 6: XML escaping doesn't introduce injection vectors."""
    print("\n" + "=" * 80)
    print("TEST 6: XML Escape Safety")
    print("=" * 80)

    # Even if a dangerous string gets through the guard somehow,
    # XML escaping should not create new injection vectors
    test_inputs = [
        "DI.col <> 'gold'",  # <> operator
        "DI.col != 'gold'",  # != operator
        "DI.col < 'gold' AND DI.col > 'silver'",  # < and > operators
        "DI.col & 'gold'",  # & operator (invalid SQL but test escaping)
    ]

    passed = 0
    failed = 0

    for inp in test_inputs:
        escaped = _xml_escape(inp)
        # Verify XML entities are properly escaped
        if "<" in escaped or ">" in escaped or "&" in escaped:
            # Check that the escaped version doesn't contain raw < > &
            # that could break XML parsing
            raw_angle = escaped.count("<") + escaped.count(">")
            if raw_angle == 0:
                passed += 1
                print(f"  PASS | {inp[:40]} | â†’ {escaped[:50]}")
            else:
                # Some raw < > may remain from entities themselves
                # Check they're only in entity form
                if "<" not in escaped.replace("<", "").replace(">", "").replace("&", ""):
                    passed += 1
                    print(f"  PASS | {inp[:40]} | â†’ {escaped[:50]}")
                else:
                    failed += 1
                    print(f"  FAIL | Raw < > in escaped output | {inp[:40]} | {escaped[:50]}")
        else:
            passed += 1
            print(f"  PASS | {inp[:40]} | â†’ {escaped[:50]} (no special chars)")

    print(f"\n  Result: {passed} safe, {failed} unsafe (should be 0)")
    return failed == 0


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 80)
    print("SQL INJECTION GUARD â€” COMPREHENSIVE TEST SUITE")
    print("Using DUMMY SP mock (no real database, no real API calls)")
    print("=" * 80)

    results = []
    results.append(("SQL Guard Direct", test_sql_guard_direct()))
    results.append(("Legitimate Queries Pass", test_legitimate_queries_pass()))
    results.append(("_build_p Safe Output", test_build_p_no_injection()))
    results.append(("Filter Value Sanitization", test_filter_value_sanitization()))
    results.append(("Full Pipeline", test_full_pipeline()))
    results.append(("XML Escape Safety", test_xml_escape_safety()))

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"  {status} | {name}")

    print()
    if all_passed:
        print("ALL TESTS PASSED â€” SQL Guard is working correctly!")
        print("Safe to try the real SP â€” no injection can get through.")
    else:
        print("SOME TESTS FAILED â€” fix before using real SP!")
    print("=" * 80)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

