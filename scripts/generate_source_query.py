"""Generate a report source query from the legacy Sales SP.

The legacy SP builds a dynamic SELECT that unions live, archive and SideUp data with
all detail-subquery joins already in place.  This script extracts that query,
neutralises dynamic variables, and writes it to app/report_queries/sales_report.sql.

The shared chat SP can then receive this query as "SourceQuery" and wrap it as
"FROM (<SourceQuery>) a" without creating any database object in the tenant DB.
"""
import re
from pathlib import Path
from typing import List

SP_PATH = Path(__file__).resolve().parent.parent / "Sample_SQL_Sp" / "sample_salessp.sql"
OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "report_queries" / "sales_report.sql"

SQL2_MARKER = "SET @SQL2 = '"


def _extract_string_literals(section: str) -> str:
    lines = section.splitlines()
    parts = []
    in_string = False
    for line in lines:
        if not in_string:
            m = re.match(r"\s*SET\s+@SQL\w+\s*=\s*'(.*)$", line, re.IGNORECASE)
            if not m:
                continue
            content = m.group(1)
            if content.endswith("'") and not content.endswith("''"):
                content = content[:-1]
                parts.append(content)
                continue
            in_string = True
            parts.append(content)
            continue
        if re.fullmatch(r"\s*'", line):
            in_string = False
            continue
        parts.append(line)
    raw = "\n".join(parts)
    return raw.replace("''", "'")


def _strip_dynamic_sql_boundaries(body: str) -> str:
    """Remove the boundaries between concatenated @SQLn string variables.

    The legacy SP builds the query across SET @SQLn = '...' fragments. After
    extraction each fragment ends with a closing quote and the next fragment
    starts with SET @SQLn = '.  We merge them into one valid SQL statement by
    stripping those markers.
    """
    lines = body.splitlines()
    out: List[str] = []
    for line in lines:
        if re.fullmatch(r"\s*SET\s+@SQL\w+\s*=\s*'", line):
            if out and out[-1].rstrip().endswith("'"):
                out[-1] = out[-1].rstrip()[:-1].rstrip()
            continue
        if re.fullmatch(r"\s*';\s*", line) or re.fullmatch(r"\s*'\s*", line):
            continue
        out.append(line)
    return "\n".join(out)


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 1
    i = open_idx + 1
    while i < len(text) and depth > 0:
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
        i += 1
    if depth != 0:
        raise ValueError("Unbalanced parentheses in CTE")
    return i - 1


def _find_last_section(sql: str) -> str:
    # The latest report definition always starts with the dynamic SQL variable
    # @SQL2.  Use the last occurrence so we pick the current version of the SP.
    idx = sql.rfind(SQL2_MARKER)
    if idx == -1:
        raise ValueError("Could not find SET @SQL2 section")
    end = sql.find(";DECLARE @FromDate1 AS DATETIME", idx)
    if end == -1:
        end = len(sql)
    return sql[idx:end]


def _inline_cte(body: str) -> str:
    """Remove the leading CTE and inline it as derived tables.

    The source query must be valid when wrapped as "FROM (<query>) DI" by the
    shared SP.  SQL Server does not allow a CTE inside a derived table, so we
    replace each "FROM CustomizePrintCTE" reference with the CTE body directly.
    """
    cte_marker = ";WITH CustomizePrintCTE AS ("
    cte_start = body.find(cte_marker)
    if cte_start == -1:
        return body
    inner_open = cte_start + len(cte_marker)
    inner_close = _find_matching_paren(body, inner_open)
    cte_body = body[inner_open:inner_close]

    # Remove the CTE prefix (including any leading whitespace/semicolons).
    after_cte = body[inner_close + 1:]
    after_cte = after_cte.lstrip()

    # Normalize line endings for replacement.
    cte_body_one_line = " ".join(cte_body.split())

    # Replace each reference: "FROM CustomizePrintCTE WITH (NOLOCK)" with the
    # inline derived table.  The WHERE clause following the reference is kept.
    after_cte = re.sub(
        r"FROM\s+CustomizePrintCTE\s+WITH\s*\(\s*NOLOCK\s*\)",
        f"FROM ({cte_body_one_line}) AS CustomizePrintCTE",
        after_cte,
        flags=re.IGNORECASE,
    )
    return after_cte


def generate() -> str:
    sql = SP_PATH.read_text(encoding="utf-8")
    section = _find_last_section(sql)
    body = _extract_string_literals(section)
    body = _strip_dynamic_sql_boundaries(body)
    body = _inline_cte(body)

    # Remove the ImageUploadLogicalPath variable declaration if present.
    body = re.sub(r"\s*;declare\s+@ImageUploadLogicalPath[\s\S]*?\n", "\n", body, flags=re.IGNORECASE)

    # Neutralise dynamic SP variables.
    body = re.sub(r"\['\+@dbName\+'\]\.dbo\.", "[dbo].", body)
    body = re.sub(r"'\+@dbName\+'\.dbo\.", "[dbo].", body)
    body = re.sub(r"'\s*\+\s*@WhereClause\s*\+\s*'", "1=1", body)
    body = re.sub(r"\b@WhereClause\b", "1=1", body)
    body = body.replace("''+@IsshowCustomerName+''", "''")
    body = re.sub(
        r",\s*'\s*\+\s*replace\(isnull\(DI\.StockBarcode,''\),'_',''\)\s+as\s+jobno_excel",
        ",replace(isnull(DI.StockBarcode,''),'_','') as jobno_excel",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r",\s*'\s*'\s*'\s*'\s*\+\s*replace\(isnull\(DI\.StockBarcode,''\),'_',''\)\s+as\s+jobno_excel",
        ",replace(isnull(DI.StockBarcode,''),'_','') as jobno_excel",
        body,
        flags=re.IGNORECASE,
    )

    # Convert the temp-table insert wrapper into a plain SELECT.  The result is a
    # query that returns the same report-ready row as the legacy SP.
    body = re.sub(
        r"\s*select\s+a\.\*\s+into\s+JewellerySaleMixReport_AMT_\'\+@randno\+\'_2\s+from\s*",
        "\nSELECT a.*,\n"
        "       a.MasterManagement_BusinessClassname AS CustomerType,\n"
        "       a.IsSampleLineJob AS jobtype,\n"
        "       a.GroupJob AS IsClub,\n"
        "       a.LabourAmount AS totalLabourAmt,\n"
        "       a.OtherAmount AS totalOtherAmt,\n"
        "       a.usermanagement_salesrepcode AS SalesRep,\n"
        "       a.D_F_Pcs_Cm AS Co_DiaPCS,\n"
        "       a.D_F_Wt_Cm AS Co_DiaWt,\n"
        "       a.D_F_Pcs_Ct AS Cu_DiaPCS,\n"
        "       a.D_F_Wt_Ct AS Cu_DiaWt\nFROM\n",
        body,
        flags=re.IGNORECASE,
    )

    # Trim everything after the outer derived table closes.
    body = re.sub(r"\)\s*as\s+a\s*';[\s\S]*$", ") as a", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*';\s*$", "", body)

    # Strip comments so the wrapped query passes SP-side validation for ; -- /* */
    body = re.sub(r"/\*[\s\S]*?\*/", "", body)
    body = re.sub(r"\s*--[^\n]*", "", body)

    return body


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    query = generate()
    OUT_PATH.write_text(query, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
