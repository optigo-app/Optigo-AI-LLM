"""ParseResult — structured query intent returned by the LLM, plus the helpers
that convert it into IntentSpec / validated filters / SP parameters.

Also holds ``validate_ai_where`` (the ai_where safety guard) since it is only
consumed by ``ParseResult.generate_where_clause``.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Set

from app.services.intent import IntentSpec
from app.services.column_registry import _REGISTRY as _COLUMN_REGISTRY
from app.services.column_registry import (
    get_filter_invalid_values,
    get_invalid_value_columns,
)
from app.services.catalog_builder import _get_cached_valid_columns
from app.services.metric_validator import get_metric_unit, get_metric_unit_label

logger = logging.getLogger(__name__)


def expand_computed_where_refs(ai_where: str, report_key: str = "sales_report") -> str:
    """Rewrite ``DI.<computed-column>`` references inside a WHERE clause.

    The LLM sometimes emits filters on computed output columns (e.g.
    ``DI.SoldPending = 'pending'``). Those names don't exist as physical
    columns, so we substitute the column's configured ``dimension_expr``
    (falling back to ``metric_expr``) wrapped in parentheses. Columns marked
    ``not_in_where``/``computed_only`` are never expanded — they stay as refs
    and get rejected by the caller's validation pass.
    """
    if not ai_where:
        return ai_where
    columns = _COLUMN_REGISTRY.get(report_key, {}).get("columns", {})
    if not columns:
        return ai_where
    from app.services.column_registry import get_computed_only_names, get_canonical_values
    computed_only = {n.lower() for n in get_computed_only_names(report_key)}
    name_map = {name.lower(): meta for name, meta in columns.items()}

    # Normalize LIKE literals whose exact value is a canonical-vocabulary key:
    # 'polishing' -> 'Polish' so LIKE '%Polish%' matches 'Pre Polish-Issue'.
    # Applies to both `DI.<field> LIKE` and inlined-expression forms.
    canon_all: dict = {}
    report_cfg = _COLUMN_REGISTRY.get(report_key, {})
    for field_map in (report_cfg.get("canonical_values") or {}).values():
        for k, v in field_map.items():
            canon_all.setdefault(str(k).strip().lower(), str(v))

    def _canon_like(match: "re.Match") -> str:
        lit = match.group(2)
        mapped = canon_all.get(lit.strip().lower())
        return f"{match.group(1)}'%{mapped if mapped else lit}%'"

    if canon_all:
        ai_where = re.sub(
            r"(LIKE\s+)'%([^'%]*)%'",
            _canon_like, ai_where, flags=re.IGNORECASE,
        )

    protected: Dict[str, str] = {}
    for _ in range(5):  # bounded: expressions may nest other computed refs
        refs = re.findall(r'DI\.(\w+)', ai_where, re.IGNORECASE)
        rewritten = False
        for ref in refs:
            key = ref.lower()
            if key in computed_only:
                continue
            meta = name_map.get(key)
            if meta and meta.get("computed"):
                expr = meta.get("dimension_expr") or meta.get("metric_expr") or ""
                if expr:
                    # A self-reference inside the expression (e.g.
                    # dimension_expr = ISNULL(DI.CustomerFullName,'')) means
                    # the physical column — shield it so the loop can't
                    # re-expand it into ISNULL(ISNULL(...)) garbage.
                    sentinel = f"__SELFREF_{len(protected)}__"
                    protected[sentinel] = f"DI.{ref}"
                    expr_safe = re.sub(
                        r'\bDI\.' + re.escape(ref) + r'\b', sentinel,
                        expr, flags=re.IGNORECASE,
                    )
                    ai_where = re.sub(
                        r'\bDI\.' + re.escape(ref) + r'\b',
                        f"({expr_safe})", ai_where, flags=re.IGNORECASE,
                    )
                    rewritten = True
        if not rewritten:
            break
    for sentinel, original in protected.items():
        ai_where = ai_where.replace(sentinel, original)
    return ai_where


# The SP validates AIWhereClause at 2000 chars; keep headroom for transport
# escaping.
_AIWHERE_MAX_LEN = 1900


def _split_top_level_and(sql: str) -> List[str]:
    """Split a WHERE clause on top-level AND only — ANDs inside parentheses
    or string literals stay with their clause."""
    parts, depth, cur, i = [], 0, [], 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1 : j + 2] == "'":  # escaped '' literal quote
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            cur.append(sql[i:j])
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and sql[i : i + 5].upper() == " AND ":
            parts.append("".join(cur))
            cur = []
            i += 5
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _has_top_level(sql: str, op: str) -> bool:
    """True when ``sql`` contains ``op`` (' OR ', ' AND ') at paren depth 0 —
    operators inside expressions or string literals don't count."""
    depth, i, n = 0, 0, len(sql)
    opu = op.upper()
    m = len(op)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and sql[i : i + m].upper() == opu:
            return True
        i += 1
    return False


def _compact_repeated_operands(ai_where: str) -> str:
    """Merge AND-ed LIKE predicates that repeat one identical operand.

    Expanded computed columns (e.g. the ~1.4k-char department CASE) overflow
    the SP's 2000-char AIWhereClause limit when the same operand is LIKE-ed
    more than once. ``E LIKE '%a%' AND E LIKE '%b%'`` is equivalent to
    ``E LIKE '%a%b%'`` (same order requirement) — merging removes the
    duplicated expression.
    """
    pat = re.compile(r"^(?P<op>.*?)\s+LIKE\s+'%(?P<lit>[^']*)%'\s*$",
                     re.IGNORECASE | re.DOTALL)
    parts: List[str] = []
    for part in _split_top_level_and(ai_where):
        # The multi-word LIKE splitter wraps AND-chains in parens — unwrap them
        # (safe only when the group holds pure ANDs; an OR inside must keep
        # its parens or precedence changes).
        inner = ""
        if part.startswith("(") and part.endswith(")"):
            depth = 0
            for k, ch in enumerate(part):
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0 and k < len(part) - 1:
                    break  # outer paren closes early — not a whole-wrapped group
            else:
                inner = part[1:-1]
        if inner and not _has_top_level(inner, " OR "):
            subs = _split_top_level_and(inner)
            if len(subs) > 1:
                parts.extend(subs)
                continue
        parts.append(part)
    merged: List[str] = []
    i = 0
    while i < len(parts):
        m = pat.match(parts[i])
        if not m:
            merged.append(parts[i])
            i += 1
            continue
        lits = [m.group("lit")]
        j = i + 1
        while j < len(parts):
            m2 = pat.match(parts[j])
            if m2 and m2.group("op") == m.group("op"):
                lits.append(m2.group("lit"))
                j += 1
            else:
                break
        if len(lits) > 1:
            merged.append(f"{m.group('op')} LIKE '%{'%'.join(lits)}%'")
        else:
            merged.append(parts[i])
        i = j
    return " AND ".join(merged)


def validate_ai_where(ai_where: str, report_key: str = "sales_report") -> str:
    """Validate the LLM-generated WHERE clause.

    Scalable guards:
    1. Reject any clause containing SELECT (subqueries) — we can't validate
       subquery column references against unknown table schemas, and the SP
       doesn't support them reliably.
    2. Extract all DI.<column> references and check each against the column
       registry. If any reference is a computed metric (not a physical column),
       the entire clause is nullified to prevent SQL errors.

    No matter what the LLM generates, invalid column references are caught
    before reaching the SP.
    """
    if not ai_where or not ai_where.strip():
        return ""

    ai_where = ai_where.strip()

    # Guard 1: Reject subqueries entirely
    if re.search(r'\bSELECT\b', ai_where, re.IGNORECASE):
        logger.warning(
            "ai_where validation: subquery (SELECT) detected — nullifying ai_where: %s",
            ai_where[:100]
        )
        return ""

    # Guard 2b: drop AND-ed clauses whose literal is a declared-invalid filter
    # value for the DI column they reference (e.g. the LLM leaks a metric word
    # like 'diamond' into a CustomerName LIKE clause). Runs on the raw clause —
    # after the LIKE splitter, fragments like '%diamond%' would look invalid
    # even though the full name 'diamond traders' is legitimate.
    invalid_cols = get_invalid_value_columns(report_key)
    if invalid_cols:
        kept = []
        dropped_any = False
        for clause in re.split(r"\s+AND\s+", ai_where, flags=re.IGNORECASE):
            clause = clause.strip()
            if not clause:
                continue
            refs = {r.lower() for r in re.findall(r"DI\.(\w+)", clause, re.IGNORECASE)}
            literals = [
                re.sub(r"%", "", lit).replace("''", "'").strip().lower()
                for lit in re.findall(r"'((?:''|[^'])*)'", clause)
            ]
            drop = False
            for ref in refs:
                invalid = invalid_cols.get(ref)
                if invalid and any(lit in invalid for lit in literals):
                    drop = True
                    break
            if drop:
                dropped_any = True
                logger.warning(
                    "ai_where validation: dropping clause with invalid value "
                    "for DI column: %s", clause[:100]
                )
            else:
                kept.append(clause)
        if dropped_any:
            ai_where = " AND ".join(kept)
            if not ai_where:
                return ""

    # Guard 2ab: split multi-word LIKE literals into per-word AND LIKEs.
    # Stored names have irregular spacing ('DEEPAK  PAREEK') so a literal
    # LIKE '%DEEPAK PAREEK%' misses; ('%DEEPAK%' AND '%PAREEK%') matches.
    # Runs on the raw clause — expansion can wrap operands in extra parens
    # that the operand pattern would no longer match.
    _operand = (
        r"(?:(?:ISNULL|COALESCE)\s*\((?:[^()]|\([^()]*\))*\)"
        r"|DI\.\w+(?:\s*\+\s*DI\.\w+)*)"
    )

    def _split_like(m: "re.Match") -> str:
        expr, lit = m.group(1), m.group(2)
        words = [w for w in re.split(r"\s+", lit.replace("%", " ").strip()) if w]
        if len(words) < 2:
            return m.group(0)
        return "(" + " AND ".join(f"{expr} LIKE '%{w}%'" for w in words) + ")"

    ai_where = re.sub(
        rf"({_operand})\s+LIKE\s+'([^']*\s[^']*)'", _split_like, ai_where,
        flags=re.IGNORECASE,
    )

    # Guard 2a: expand DI.<computed-column> refs into their configured expressions
    ai_where = expand_computed_where_refs(ai_where, report_key)

    # Guard 2d: the SP caps AIWhereClause at 2000 chars. Expanded computed
    # operands repeat per predicate, so first merge same-operand LIKE chains;
    # if it still doesn't fit, fail closed rather than hit an SP rejection.
    if len(ai_where) > _AIWHERE_MAX_LEN:
        ai_where = _compact_repeated_operands(ai_where)
    if len(ai_where) > _AIWHERE_MAX_LEN:
        logger.warning(
            "ai_where validation: expanded clause exceeds SP limit "
            "(%d chars) — nullifying ai_where", len(ai_where),
        )
        return ""

    # Guard 2c: Validate all DI.<column> references against the column registry
    valid_cols = _get_cached_valid_columns(report_key)
    refs = re.findall(r'DI\.(\w+)', ai_where, re.IGNORECASE)

    for ref in refs:
        if ref.lower() not in valid_cols:
            logger.warning(
                "ai_where validation: DI.%s is not a valid base-table column "
                "(computed or unknown) — nullifying ai_where: %s",
                ref, ai_where[:100]
            )
            return ""

    return ai_where


# Simple one-column predicates we can safely retarget: complex clauses
# (nested AND/OR, arithmetic, functions over several columns) are left alone.
_PREDICATE_RE = re.compile(
    r"^\s*\(?\s*"
    r"(?P<operand>(?:ISNULL|COALESCE)\s*\([^()]*\)|(?:DI\.)?\w+(?:\s*\+\s*(?:DI\.)?\w+)*)"
    r"\s*(?P<op>NOT\s+IN|IN|NOT\s+LIKE|LIKE|=|<>|!=)\s*"
    r"\(?\s*(?P<lits>'(?:''|[^'])*'(?:\s*,\s*'(?:''|[^'])*')*|\d+(?:\s*,\s*\d+)*)\s*\)?\s*$",
    re.IGNORECASE,
)

# Words that must never be "rescued" as filter values — they are question
# scaffolding or time vocabulary, not ERP data values. Observed false
# positives: 'you' -> brandname 'YOUR BRAND', 'yesterday' -> has_quotation
# 'Yes' (prefix containment on a word the date parser already consumed).
_RESCUE_STOPWORDS = {
    # function / question words
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am",
    "can", "could", "would", "should", "shall", "will", "may", "might", "must",
    "do", "does", "did", "done", "has", "have", "had",
    "i", "me", "my", "we", "our", "us", "you", "your", "yours", "he", "she",
    "his", "her", "it", "its", "they", "them", "their",
    "this", "that", "these", "those", "there", "here",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "tell", "show", "give", "get", "got", "list", "find", "know", "want",
    "need", "please", "say", "said", "ask", "check", "let", "make", "made",
    "all", "any", "some", "none", "each", "every", "either", "neither",
    "and", "or", "but", "if", "then", "than", "so", "as", "of", "in", "on",
    "at", "for", "to", "from", "by", "with", "without", "about", "into",
    "over", "under", "between", "through", "against", "per", "via",
    "not", "no", "nor", "yes", "only", "just", "also", "too", "very",
    "currently", "now", "available", "still", "already", "yet",
    "more", "most", "less", "least", "much", "many", "few", "several",
    "new", "old", "same", "other", "another", "such", "own",
    "number", "count", "total", "value", "detail", "details", "data",
    "thing", "things", "stuff", "kind", "type", "sort",
    # temporal vocabulary — consumed by the date parser; rescuing them as
    # values (e.g. 'yesterday' -> 'Yes' via prefix) is always wrong
    "today", "yesterday", "tomorrow", "day", "days", "daily",
    "week", "weeks", "weekly", "month", "months", "monthly",
    "year", "years", "yearly", "quarter", "quarters", "quarterly",
    "date", "dates", "time", "period", "range", "ago", "before", "after",
    "last", "next", "previous", "past", "recent", "since", "until", "till",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
}


def _field_naming_words(
    cfg: Dict[str, Any], columns: Dict[str, Any]
) -> Set[str]:
    """Words/phrases that NAME a field — never a filter value.

    'design' is a dimension alias; rescuing it as producttypename='Designer
    Pendant' silently corrupts 'top 5 design ...' questions. Same for column
    names, filter aliases, metric labels/aliases, and generic routing terms.
    """
    names: Set[str] = set()
    for cname, meta in columns.items():
        names.add(cname.lower())
        if isinstance(meta, dict):
            sql = (meta.get("sql") or "").strip().lower()
            if sql:
                names.add(sql)
            for a in (meta.get("aliases") or []):
                names.add(str(a).strip().lower())
            for a in ((meta.get("filter") or {}).get("aliases") or []):
                names.add(str(a).strip().lower())
            lbl = (meta.get("label") or "").strip().lower()
            if lbl:
                names.add(lbl)
    for a in (cfg.get("dimension_aliases") or {}):
        names.add(str(a).strip().lower())
    for a in (cfg.get("filter_key_map") or {}):
        names.add(str(a).strip().lower())
    for mkey, meta in (cfg.get("metric_catalog") or {}).items():
        names.add(str(mkey).lower())
        if isinstance(meta, dict):
            lbl = (meta.get("label") or "").strip().lower()
            if lbl:
                names.add(lbl)
            for a in meta.get("aliases") or []:
                names.add(str(a).strip().lower())
    for skey in (cfg.get("special_metrics") or {}):
        names.add(str(skey).lower())
    try:
        from app.services.column_registry import get_generic_routing_terms
        names |= get_generic_routing_terms()
    except Exception:
        pass
    # Expand to individual words too — 'job no' / 'order type' phrases and
    # their parts ('order', 'type') are all field-naming, not values.
    words = set(names)
    for phrase in list(names):
        words.update(re.findall(r"[a-z0-9]+", phrase))
    return names | {w for w in words if len(w) >= 3}


def retarget_misplaced_filters(question: str, parsed: "ParseResult") -> None:
    """Move misplaced qualifiers onto the column their value belongs to.

    The LLM parks unknown qualifier words on whichever column it knows best
    (``department LIKE '%RND%'`` when RND is an order-type value; 'repair'
    dropped entirely). For each simple predicate in ai_where the literal is
    checked against EVERY column's vocabulary (canonical + live masters):
    when it belongs to exactly one other column, the clause is removed and a
    structured filter is injected — which then flows through
    validated_filters/canonical normalization like any parser-produced
    filter. A second pass scans the question for vocabulary values that
    appear in no filter and no ai_where literal, rescuing dropped qualifiers.
    Ambiguous matches (two columns claim the word) are left untouched.
    """
    from app.services.master_data import find_column_for_value

    report_key = parsed.report_key
    cfg = _COLUMN_REGISTRY.get(report_key, {})
    columns = cfg.get("columns", {}) or {}
    if not columns:
        return

    key_by_name: Dict[str, str] = {}
    for name, meta in columns.items():
        if isinstance(meta, dict):
            key_by_name[name.lower()] = name
            sql = (meta.get("sql") or "").lower()
            if sql:
                key_by_name.setdefault(sql, name)
    # Alias spellings too — the LLM writes operands like `jobtype` where the
    # column is actually named `orders` (order_report). Real column names were
    # registered first so aliases can never shadow them.
    def _alias_forms(a: str):
        a = a.strip().lower()
        return (a, a.replace("_", "").replace(" ", ""))
    for name, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        for a in ((meta.get("filter") or {}).get("aliases") or []):
            for form in _alias_forms(str(a)):
                key_by_name.setdefault(form, name)
    for a, t in (cfg.get("dimension_aliases") or {}).items():
        if t in columns:
            for form in _alias_forms(str(a)):
                key_by_name.setdefault(form, t)
    for a, t in (cfg.get("filter_key_map") or {}).items():
        if t in columns:
            for form in _alias_forms(str(a)):
                key_by_name.setdefault(form, t)

    # Column -> filter key that reaches it. Filter keys must come from
    # filter_key_map (validated_filters only allows those aliases); a column
    # with its own `filter` block is reachable under its own name.
    fkm = cfg.get("filter_key_map", {}) or {}
    col_to_key: Dict[str, str] = {}
    for alias, target in fkm.items():
        if target not in ("_date", "_name_filter"):
            col_to_key.setdefault(target, alias)

    def filter_key_for(col: str) -> Optional[str]:
        if col in col_to_key:
            return col_to_key[col]
        return col if isinstance(columns.get(col), dict) and columns[col].get("filter") else None

    def resolve_col(operand: str) -> Optional[str]:
        refs = re.findall(r"DI\.(\w+)", operand, re.IGNORECASE)
        toks = re.findall(r"\w+", operand)
        ident = (refs[-1] if refs else (toks[-1] if toks else "")).lower()
        return key_by_name.get(ident)

    # --- pass 1: retarget ai_where literals --------------------------------
    ai_where = (parsed.ai_where or "").strip()
    if ai_where:
        kept, changed = [], False
        for clause in _split_top_level_and(ai_where):
            m = _PREDICATE_RE.match(clause)
            if not m:
                kept.append(clause)
                continue
            operand = m.group("operand")
            stated = resolve_col(operand)
            op = m.group("op").upper()
            negated = op in ("<>", "!=", "NOT IN", "NOT LIKE")
            lits = [
                lit.replace("''", "'").strip().strip("%").strip()
                for lit in re.findall(r"'((?:''|[^'])*)'", m.group("lits"))
            ]
            # Unquoted numeric literals (`has_quotation = 1`) — the LLM treats
            # Yes/No CASE columns as bit flags.
            lits += re.findall(
                r"\d+", re.sub(r"'(?:''|[^'])*'", "", m.group("lits"))
            )
            targets = set()
            for lit in lits:
                hit = find_column_for_value(report_key, lit)
                if hit:
                    targets.add(hit[0])
            # Move to a structured filter when (a) the literal belongs to a
            # different column's vocabulary, or (b) the stated column can't
            # appear in a WHERE at all (computed with subquery / not_in_where)
            # — bare `priority LIKE '%High%'` would just be a SQL error.
            stated_meta = columns.get(stated, {}) if stated else {}
            stated_unwhereable = bool(
                stated_meta.get("not_in_where")
                or ("select" in str(stated_meta.get("dimension_expr") or stated_meta.get("de") or "").lower())
            )
            # `=`/`IN` on a computed column belongs in a structured filter —
            # canonical/live normalization can then map values the LLM got
            # wrong ('1' on a Yes/No CASE -> 'Yes'). LIKE keeps its expanded
            # form in ai_where since substring match has no filter equivalent.
            stated_computed_eq = bool(
                stated_meta.get("computed") and op in ("=", "IN")
            )
            wrong_col = len(targets) == 1 and targets != {stated}
            if not negated and (wrong_col or (stated and (stated_unwhereable or stated_computed_eq))):
                tgt = next(iter(targets)) if wrong_col else stated
                fkey = filter_key_for(tgt)
                if fkey:
                    for lit in lits:
                        hit = find_column_for_value(report_key, lit)
                        val = hit[1] if hit and hit[0] == tgt else lit
                        if fkey not in parsed.filters:
                            parsed.filters[fkey] = val
                    changed = True
                    logger.info(
                        "retarget: moved %r from %s to column %s",
                        lits, stated, tgt,
                    )
                    continue
            # Normalize a bare operand to DI.<name> so guard 2c validates it
            # and computed refs expand: `priority LIKE` -> `DI.priority LIKE`.
            if stated and "DI." not in operand.upper():
                meta = columns.get(stated, {})
                target_name = stated if meta.get("computed") else (meta.get("sql") or stated)
                clause = (
                    clause[:m.start("operand")] + f"DI.{target_name}"
                    + clause[m.end("operand"):]
                )
                changed = True
            kept.append(clause)
        if changed:
            parsed.ai_where = " AND ".join(kept)

    # --- pass 2: rescue qualifiers the LLM dropped entirely ----------------
    used_cols = {resolve_col(m.group("operand"))
                 for m in map(_PREDICATE_RE.match, _split_top_level_and(parsed.ai_where or ""))
                 if m}
    used_cols.discard(None)
    for fkey in parsed.filters:
        used_cols.add(fkm.get(fkey.lower(), fkey))
    # Snapshot of columns actually filtered (ai_where operands + filters).
    filtered_cols = set(used_cols)
    # The group-by dimension's own vocabulary isn't a "dropped qualifier" —
    # "orders by sold pending" must not gain a SoldPending='sold' filter.
    dim_col = resolve_col(parsed.dimension) if parsed.dimension else None
    if dim_col:
        used_cols.add(dim_col)
    covered_words = set()
    for fval in parsed.filters.values():
        covered_words.update(re.findall(r"[a-z0-9]+", str(fval).lower()))
    for lit in re.findall(r"'((?:''|[^'])*)'", parsed.ai_where or ""):
        covered_words.update(re.findall(r"[a-z0-9]+", lit.replace("%", " ").lower()))

    # Rescue stop-set: question scaffolding + temporal words (already consumed
    # by the date parser) are never rescued. Field-NAMING words ('design' is a
    # dimension alias, not a value) are blocked from *containment* matches —
    # 'design' -> producttypename 'Designer Pendant' corrupts the question —
    # but an exact value hit still wins ('sales order' is a dim alias AND the
    # IsCompanyJob value 'Sales Order').
    field_names = _field_naming_words(cfg, columns)
    words = re.findall(r"[a-z0-9&.'-]+", (question or "").lower())
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n]).strip(".'-")
            if len(phrase) < 3 or phrase in _RESCUE_STOPWORDS:
                continue
            hit = find_column_for_value(
                report_key, phrase, exclude=used_cols, allow_long_needle=False
            )
            if not hit:
                continue
            col, val = hit
            if phrase in field_names and val.lower() != phrase:
                continue  # field-named word, fuzzy hit — almost surely wrong
            if covered_words & set(re.findall(r"[a-z0-9]+", val.lower())):
                continue  # the matched value's own words are already filtered
            fkey = filter_key_for(col)
            if not fkey or fkey in parsed.filters:
                continue
            parsed.filters[fkey] = val
            used_cols.add(col)
            covered_words.update(re.findall(r"[a-z0-9]+", val.lower()))
            logger.info("retarget: rescued dropped qualifier %r -> %s=%r", phrase, col, val)

    # --- pass 3: collapse a filtered question's redundant dimension --------
    # "total products in filing department" parsed as dim=department +
    # department~filing returns only the TOP filing stage under limit=1
    # (Filing-Issue 128) instead of the whole-department total. A scoped
    # question with no breakdown marker (by/per/each/wise) and no ranking
    # marker (top/best/…) wants the scalar — drop the dimension.
    if dim_col and filtered_cols and not re.search(
        r"\b(by|per|each|wise|breakdown|breakup|vs|versus|top|best|worst|"
        r"largest|smallest|most|least|highest|lowest|leading|bottom)\b",
        (question or "").lower(),
    ) and (dim_col in filtered_cols or (parsed.limit or 1) <= 1):
        logger.info("retarget: dropped redundant dimension %r", parsed.dimension)
        parsed.dimension = None


class ParseResult:
    """Structured query intent from the LLM."""
    def __init__(self, data: Dict[str, Any]):
        self.report_key: str = data.get("report_key", "sales_report")
        self.metric: str = data.get("metric", "Amount")
        self.dimension: Optional[str] = data.get("dimension")
        self.aggregation: str = data.get("aggregation", "sum")
        self.limit: int = int(data.get("limit", 1) or 1)
        self.filters: Dict[str, str] = data.get("filters", {}) or {}
        self.date_filter: Optional[Dict[str, str]] = data.get("date_filter")
        self.sort: str = data.get("sort", "desc")
        self.ai_where: Optional[str] = data.get("ai_where")
        self.clarify: Optional[str] = data.get("clarify")
        self.extra_metrics: List[str] = data.get("extra_metrics", []) or []
        self.intent: Optional[str] = data.get("intent")
        _conf = data.get("confidence")
        self.confidence: float = max(0.0, min(1.0, float(_conf if _conf is not None else 0.8)))
        # The LLM's own confidence, preserved separately — finalize_query
        # overwrites `confidence` with the routing-source score.
        self.model_confidence: float = self.confidence
        self.alternatives: List[Dict[str, Any]] = data.get("alternatives", []) or []
        self.ai_where_dropped: bool = False  # set when validation nullifies a supplied clause
        self.raw: Dict[str, Any] = data

    def to_intent_spec(self) -> IntentSpec:
        """Convert to IntentSpec for the existing pipeline."""
        # Dynamically read filter_only columns from registry (scalable — no hardcoding)
        report_cfg = _COLUMN_REGISTRY.get(self.report_key, {})
        columns = report_cfg.get("columns", {})
        _FILTER_ONLY = {
            name for name, meta in columns.items()
            if meta.get("filter_only")
        }
        metric = self.metric
        # filter_only columns can't be GROUP BY'd or summed, but a scalar
        # MAX()/MIN() text lookup ('what is its customer type') is valid —
        # only block value-aggregations.
        if metric in _FILTER_ONLY and self.aggregation not in ("max", "min"):
            metric = "Amount"
        spec = IntentSpec(report_key=self.report_key)
        spec.intent = self.intent or f"semantic_{metric}"
        spec.metric_key = metric
        spec.aggregation = self.aggregation
        spec.dimension = self.dimension or ""
        # Guard: filter-only columns cannot be used as dimensions (GROUP BY)
        if spec.dimension in _FILTER_ONLY:
            spec.dimension = ""
        spec.limit = self.limit
        spec.sort = self.sort
        spec.ai_where = self.ai_where or ""

        # Determine unit using centralized classification
        spec.unit = get_metric_unit(metric, self.report_key)
        spec.unit_label = get_metric_unit_label(metric, self.report_key)
        return spec

    def to_validated_filters(self) -> Dict[str, Any]:
        """Convert filters + date_filter to the format expected by _build_p."""
        result: Dict[str, Any] = {}
        for fname, fval in self.filters.items():
            result[fname] = fval
        if self.date_filter:
            preset = self.date_filter.get("preset", "")
            if preset:
                from app.services.orchestrator import resolve_preset_dates
                start, end = resolve_preset_dates(preset)
                if start and end:
                    result["start_date"] = start
                    result["end_date"] = end
            else:
                # Explicit date range: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
                start = self.date_filter.get("start", "")
                end = self.date_filter.get("end", "")
                if start and end:
                    # Validate start <= end to prevent empty results from reversed ranges
                    if start <= end:
                        result["start_date"] = start
                        result["end_date"] = end
                    else:
                        # Swap reversed range
                        result["start_date"] = end
                        result["end_date"] = start
        return result

    def generate_where_clause(self) -> str:
        """Generate SQL WHERE clause for name-based filters and AI-generated conditions.

        Code-based filters (metal_type, category, etc.) are handled by the SP's
        FilterHeader/FilterValue mechanism. Name-based filters (sales_rep, customer)
        need LIKE matching against concatenated name columns.

        Name-filter SQL expressions now come from the per-report `name_filter_map`
        in report_columns/*.json, so any report can define its own customer/name
        filter logic without editing Python.

        The ai_where field from the LLM is also included here. It will be
        validated by sql_guard.validate_where_clause() before being sent to the SP.
        """
        clauses = []
        report_cfg = _COLUMN_REGISTRY.get(self.report_key, {})
        name_filter_map = report_cfg.get("name_filter_map", {})
        # Expand aliases from filter_key_map that point to name_filter_map keys.
        filter_key_map = report_cfg.get("filter_key_map", {})
        _NAME_FILTERS: Dict[str, str] = dict(name_filter_map)
        for alias, target in filter_key_map.items():
            if target in name_filter_map and alias.lower() not in _NAME_FILTERS:
                _NAME_FILTERS[alias.lower()] = name_filter_map[target]

        for fname, fval in self.filters.items():
            col_expr = _NAME_FILTERS.get(fname.lower())
            if col_expr:
                fkey = fname.lower()
                # Resolve invalid values against the alias AND its name_filter_map target
                invalid = get_filter_invalid_values(self.report_key, fkey)
                for alias, target in filter_key_map.items():
                    if name_filter_map.get(target) == col_expr:
                        invalid |= get_filter_invalid_values(self.report_key, target)
                if str(fval).lower().strip() in invalid:
                    logger.warning(
                        "generate_where_clause: dropping %s filter — '%s' is not a valid value",
                        fname, fval
                    )
                    continue
                # Split into words and LIKE each one: stored names often have
                # irregular spacing ('Harrine  Trivedi'), so a literal
                # '%Harrine Trivedi%' misses rows a per-word AND matches.
                # Role/filler words ('customer code vidsy' -> 'vidsy') never
                # appear inside stored names — strip them first.
                _NAME_FILLER = {
                    "customer", "client", "code", "name", "named", "called",
                    "no", "no.", "number", "num", "id", "the", "a", "an",
                }
                raw_terms = [t for t in str(fval).split() if t.strip()]
                terms = [t for t in raw_terms if t.lower().strip(".") not in _NAME_FILLER] or raw_terms
                terms = [t.replace("'", "''") for t in terms]
                if terms:
                    clauses.append("(" + " AND ".join(f"{col_expr} LIKE '%{t}%'" for t in terms) + ")")

        # Add AI-generated WHERE clause (validated against column registry)
        if self.ai_where and self.ai_where.strip():
            validated = validate_ai_where(self.ai_where.strip(), self.report_key)
            if not validated:
                # The LLM supplied a filter that failed validation — the query
                # is now running UNFILTERED. Flag it so the answer can say so
                # instead of silently returning grand totals.
                self.ai_where_dropped = True
            if validated:
                # The LLM often repeats a name filter in ai_where
                # ('customer X' → filter + CustomerFullName LIKE). The extra
                # AND-ed clause hits a *different* name column and silently
                # empties results, so drop fragments that only re-check the
                # name words already filtered above.
                name_refs = {
                    ref.lower()
                    for expr in _NAME_FILTERS.values()
                    for ref in re.findall(r"DI\.(\w+)", expr, re.IGNORECASE)
                }
                name_words = {
                    w.lower()
                    for fval in self.filters.values()
                    for w in str(fval).split()
                }
                if name_refs and name_words:
                    kept = []
                    for part in _split_top_level_and(validated):
                        refs = {r.lower() for r in re.findall(r"DI\.(\w+)", part, re.IGNORECASE)}
                        lits = {
                            w.lower()
                            for lit in re.findall(r"'((?:''|[^'])*)'", part)
                            for w in re.sub(r"[%'']", " ", lit).split()
                        }
                        if refs <= name_refs and lits and lits <= name_words:
                            continue
                        kept.append(part)
                    validated = " AND ".join(kept)
            if validated:
                clauses.append(validated)

        if clauses:
            return " AND ".join(clauses)
        return ""

    def to_query_plan(self, question: str = ""):
        from app.services.query_plan import QueryPlan
        plan = QueryPlan.from_parse_result(self)
        plan.classify_complexity(question)
        return plan

    def __repr__(self) -> str:
        return (f"ParseResult(report={self.report_key}, metric={self.metric}, "
                f"dim={self.dimension}, agg={self.aggregation}, limit={self.limit}, "
                f"filters={self.filters}, date={self.date_filter})")


