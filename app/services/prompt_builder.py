"""Prompt builder — constructs the system + user prompts for the semantic
query-parser LLM call.

All sections are auto-generated from ``report_columns/*.json`` via
``catalog_builder`` so adding a report needs no prompt edits.
"""
import logging
import re

from app.config import settings

from app.services.catalog_builder import (
    _load_report_columns,
    _build_prompt_column_sections,
)

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT_TEMPLATE = """You are a query parser for an ERP jewellery system. Extract structured query intent from natural language.

Return ONLY JSON:
{{"report_key":"{report_key}","metric":"catalog metric name","confidence":0.0,"alternatives":[],"extra_metrics":["catalog metric name"],"dimension":"grouping dimension or null","aggregation":"sum|avg|max|min|count|count_distinct","limit":1,"filters":null,"date_filter":{{"preset":"today|yesterday|this_month|last_month|this_year|last_year|this_week|last_week"}}|{{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}}|null,"sort":"desc|asc","ai_where":"SQL WHERE clause or null","clarify":null}}

Rules:
- metric: catalog name, not column name. "how many bills"→total_count+count, "how many customers"→unique_customers+count, "how many designs"→unique_designs+count, "how many units/pieces"→units_sold+count_distinct.
- Metric types in catalog: [amount]=monetary value (₹), [weight]=physical weight (gms/ct), [count]=number of records, [rate]=per-unit price. Match the user's intent type: "material"→[weight] metric, "amount/value"→[amount] metric, "how many"→[count] metric, "rate"→[rate] metric. NEVER return an [amount] metric when the user asks for weight/quantity.
- aggregation: "average/avg"→avg, "total/sum"→sum, "highest/max"→max, "lowest/min"→min.
- Questions may be in English or Hinglish (Hindi written in Roman script), e.g. "aaj ki total sales", "sabse zyada kharch karne wala customer", "pichle mahine customer wise sale". Interpret them equivalently and output the same JSON. "kal" is ambiguous (yesterday or tomorrow) — if the period cannot be inferred from context, set clarify to ask.
- "average customer purchase/bill value"→metric=Amount, aggregation=avg.
- dimension: set for breakdowns ("X wise","by X","top N"). limit=5 for "wise/by", N for "top N", 1 for "best/highest/lowest X". "which month highest/lowest"→dimension="month",sort="desc"/"asc",limit=1. "which invoice highest/lowest"→dimension="StockDocumentNo",sort="desc"/"asc",limit=1. "year wise"→dimension="year". "all X list"/"list all X"/"show all X"→dimension=X,limit=50.
- Ranking/entity questions: "Which <entity> generates/has the most/highest <metric>?" or "Who generated the most <metric>?" MUST set dimension="<entity_column>" (e.g. SalesRep for salesperson/who/employee, CustomerFullName for customer, categoryname for category, branch for branch), limit=5 (or N), sort="desc". NEVER return dimension=null for ranking or entity breakdown questions.
- Growth: "growth %"/"compared with previous"→metric=Amount,aggregation=sum,date_filter for current period. Backend detects "growth"/"compared" and calls previous period.
- filters: null (always). Use ai_where for ALL filtering.
- Entity names after "customer"/"for"/"by" are filter values, not part of the question. "customer RDsuza sales rep list"→ai_where="Job_customerfirmname LIKE '%RDsuza%'", dimension="SalesrepCode".
- extra_metrics: for MULTIPLE metrics in one query. First metric→"metric", rest→"extra_metrics". Only when dimension=null and limit=1.
- Insights/summary/overview: "top N insights", "key metrics", "summary", "overview", "highlights"→metric=Amount, extra_metrics=[top 2-3 other key metrics from catalog]. Pick the most important metrics for the report type.
- clarify: if user asks for bill-level metrics (metal rate, metal amount) for a customer+metal WITHOUT a bill number, set clarify to ask for the bill number. Do NOT set clarify for aggregate queries.
- Tax: if catalog has tax_report, use report_key="tax_report", metric="TotalTax"/"TotalGST"/"Tax1Amount"/"Tax2Amount"/"Tax3Amount". If no tax_report in catalog, set clarify="Tax amount is not available in this report. Please select the tax report."
- Quotation lineage: "jobs/orders created from quote/quotation"→report_key="order_report", metric="QuotationJob", aggregation="count" — the order report lists quotation-created jobs. Only use wip_report when the user explicitly asks about in-progress/production status.
- Follow-up after clarify: if assistant's last message was a clarification and user replies short (e.g. "JS4"), treat as bill number for original question. Do NOT set clarify.
- date_filter: preset for relative dates; {{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}} for explicit ranges.
- Filter-only columns (NOT on base tables — cannot be dimension/metric/ai_where): {filter_only}. If user asks to rank/filter by these, set dimension=null, metric=Amount, aggregation=sum, ai_where=null.
- Available dimensions (on base table): {dimensions}.

## Report-specific rules
{report_rules}

## ai_where (SQL WHERE clause)
- Use DI. prefix for base-table columns. Only catalog columns.
- Operators: =,<>,!=,>,<,>=,<=,AND,OR,NOT,LIKE,IN,IS NULL,IS NOT NULL,BETWEEN.
- String values in single quotes. No WHERE keyword. No UNION/EXEC/DML/DDL/semicolons/comments/subqueries.
- NEVER reference computed-metric names in ai_where (not physical columns). If filter needs one, set ai_where=null.
- null for simple queries with no filters.

### Base-table columns for ai_where:
{base_columns}

### Computed metrics (use as metric only, NOT in ai_where):
{computed_metrics}

### ai_where examples:
{ai_where_examples}

Note: response_mode is controlled by the client. Do not include it in the JSON output.
"""


# ── Question-aware section selection (schema_reduction="rules") ───────────────
# Triggers extracted from rules are matched against the question; rules whose
# subject never appears are dropped. Rules with no extractable trigger are
# always kept (fail-open) so guardrails can't be silently pruned.

_RULE_STOPWORDS = {
    "the", "user", "and", "for", "with", "use", "not", "all", "only", "when",
    "this", "that", "into", "from", "wise", "type", "types", "always", "never",
    "do", "does", "set", "must", "note", "also", "but", "same", "means",
}


def _rule_triggers(rule: str) -> set:
    """Extract matchable trigger terms from a rule line.

    Sources: "quoted phrases", arrow/equality left-hand sides
    (``material->grosswt``, ``CSAmt=ColorStoneAmount``), and colon headers
    (``Gold purity: ...``). Returns lowercase terms.
    """
    triggers = set()
    triggers.update(m.group(1).lower().strip() for m in re.finditer(r'"([^"]+)"', rule))
    for m in re.finditer(r"([A-Za-z_][A-Za-z_/ ]{0,40}?)\s*(?:->|→|=)", rule):
        lhs = m.group(1).strip().lower()
        for part in lhs.split("/"):
            part = part.strip()
            if part:
                triggers.add(part)
                triggers.update(w for w in part.split() if len(w) >= 3)
    header = re.match(r"\s*([A-Za-z][A-Za-z /_]{2,30}?)\s*:", rule)
    if header:
        triggers.update(w for w in header.group(1).lower().split("/") for w in w.split() if len(w) >= 3)
    return {t for t in triggers if t not in _RULE_STOPWORDS and len(t) >= 2}


def _trigger_matches(trigger: str, question_lower: str) -> bool:
    if " " in trigger:
        return trigger in question_lower
    return bool(re.search(rf"\b{re.escape(trigger)}\b", question_lower))


def _select_rules(rules: list, question_lower: str) -> list:
    """Keep rules that match the question; fail-open when no trigger extracted."""
    if not question_lower:
        return rules
    kept = []
    for rule in rules:
        triggers = _rule_triggers(rule)
        if not triggers or any(_trigger_matches(t, question_lower) for t in triggers):
            kept.append(rule)
    return kept


def _match_question_columns(columns: dict, question_lower: str) -> set:
    """Columns the question plausibly references (name/aliases/filter aliases).

    Columns with an id_pattern (Job#, Design#, Invoice#, SKU#...) and
    LIKE-match name columns are always included — record-ID and entity-name
    lookups are the highest-stakes filters and cheap to keep.
    """
    matched = set()
    for col_name, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        f = meta.get("filter")
        if isinstance(f, dict) and (f.get("id_pattern") or f.get("match") == "like"):
            matched.add(col_name)
            continue
        terms = {col_name.lower()}
        terms.update(a.lower().strip() for a in meta.get("aliases", []))
        if isinstance(f, dict):
            terms.update(a.lower().strip() for a in f.get("aliases", []))
        if any(len(t) >= 3 and _trigger_matches(t, question_lower) for t in terms):
            matched.add(col_name)
    return matched


def _get_column_filter_expr(col_name: str, col_meta: dict) -> str:
    """Get the SQL expression for a column to use in ai_where.

    For computed columns, use dimension_expr (already has DI. prefix).
    For regular columns, wrap with DI. prefix.
    """
    if col_meta.get("computed") and col_meta.get("dimension_expr"):
        return col_meta["dimension_expr"]
    sql = col_meta.get("sql", col_name)
    return f"DI.{sql}"


def _build_report_rules(report_key: str, question: str = "") -> str:
    """Build report-specific rule lines from column filter metadata + prompt_rules.

    Auto-generates synonyms, record ID patterns, and filter rules from the
    `filter` object on each column. Appends non-derivable special_rules and
    excluded_columns from prompt_rules.

    When schema_reduction="rules" and a question is given, synonyms are limited
    to columns the question references (id_pattern + LIKE columns always kept),
    and special_rules/negative_examples are trigger-selected.
    """
    cfg = _load_report_columns()
    report_cfg = cfg.get(report_key, {})
    columns = report_cfg.get("columns", {})
    rules = report_cfg.get("prompt_rules", {})

    slim = settings.schema_reduction == "rules" and question
    matched_cols = _match_question_columns(columns, question.lower()) if slim else set(columns.keys())

    lines = []

    # ── Auto-generate synonyms from column aliases + filter aliases ──
    synonyms = []
    id_patterns = []
    for col_name, col_meta in columns.items():
        # Metric/dimension aliases (top-level "aliases" key)
        f = col_meta.get("filter")
        if not slim or col_name in matched_cols:
            for alias in col_meta.get("aliases", []):
                synonyms.append(f"{alias}→{col_name}")
            # Filter aliases (inside "filter" object)
            if f:
                for alias in f.get("aliases", []):
                    synonyms.append(f"{alias}→{col_name}")
        if f and f.get("id_pattern"):
            id_patterns.append(f)

    if synonyms:
        lines.append(f"- Synonyms: {', '.join(synonyms)}.")

    # ── Auto-generate record ID patterns ──
    if id_patterns:
        id_lines = []
        for col_name, col_meta in columns.items():
            f = col_meta.get("filter")
            if not f or not f.get("id_pattern"):
                continue
            expr = _get_column_filter_expr(col_name, col_meta)
            pattern = f["id_pattern"]
            match_type = f.get("match", "exact")
            if match_type == "exact" and col_meta.get("sql") == "StockBarcode":
                id_lines.append(
                    f"{pattern} → {col_name}. For Job#, normalize underscores: "
                    f"use REPLACE({expr},'_','/')='value' to match both formats."
                )
            else:
                id_lines.append(f"{pattern} → {col_name}. Use exact match in ai_where.")
        lines.append(f"- Record ID patterns: {' '.join(id_lines)}")
        lines.append(
            '- Single-record lookup: when user asks about a specific ID (Job#/Invoice#/SKU#/Design#), '
            'set aggregation="max", dimension=null, limit=1, and ai_where with exact match. '
            'For multi-field lookups, use extra_metrics with metric=Amount, aggregation=max.'
        )

    # ── Auto-generate filter rule for LIKE-match columns ──
    like_cols = []
    for col_name, col_meta in columns.items():
        f = col_meta.get("filter")
        if not f or f.get("match") != "like":
            continue
        expr = _get_column_filter_expr(col_name, col_meta)
        aliases = ", ".join(f.get("aliases", []))
        like_cols.append(f"{aliases} → {expr} LIKE '%name%'")
    if like_cols:
        lines.append(
            '- Name/text filter: when user asks about a specific ' +
            ', '.join(c.split(" → ")[0] for c in like_cols) +
            ', ALWAYS generate ai_where. Never leave ai_where null for name-specific questions.'
        )

    # ── Append non-derivable special rules from prompt_rules ──
    # Trigger-selected when slimming; rules with no extractable trigger are kept.
    question_lower = question.lower() if slim else ""
    for rule in _select_rules(rules.get("special_rules", []), question_lower):
        lines.append(f"- {rule}")

    # ── Append negative examples from prompt_rules ──
    neg = _select_rules(rules.get("negative_examples", []), question_lower)
    if neg:
        lines.append("- DO NOT map (negative examples):")
        for ex in neg:
            lines.append(f"  {ex}")

    if rules.get("excluded_columns"):
        lines.append(f"- Excluded (hardcoded 0 or unavailable): {rules['excluded_columns']}")

    return "\n".join(lines)


def _build_ai_where_examples(report_key: str, question: str = "") -> str:
    """Build ai_where examples from column filter metadata.
    Auto-generates one example per column with a `filter` object:
    - LIKE match: shows the expression with LIKE '%value%'
    - Exact match: shows DI.<sql>='value'
    - ID pattern: shows the pattern with the correct expression

    For computed columns with complex dimension_expr, uses DI.<sql> instead
    to keep examples readable.

    When schema_reduction="rules" and a question is given, only examples for
    columns the question references are emitted — falling back to all columns
    when nothing matched so filtering guidance is never fully absent.
    """
    cfg = _load_report_columns()
    report_cfg = cfg.get(report_key, {})
    columns = report_cfg.get("columns", {})

    matched_cols = set()
    if settings.schema_reduction == "rules" and question:
        matched_cols = _match_question_columns(columns, question.lower())

    examples = []

    for col_name, col_meta in columns.items():
        f = col_meta.get("filter")
        if not f:
            continue
        if matched_cols and col_name not in matched_cols:
            continue

        match_type = f.get("match", "exact")
        aliases = f.get("aliases", [col_name])
        alias = aliases[0]
        id_pattern = f.get("id_pattern")
        sql = col_meta.get("sql", col_name)

        # For computed columns, use DI.<sql> for readability unless it's a name match
        if col_meta.get("computed") and col_meta.get("dimension_expr") and match_type != "like":
            expr = f"DI.{sql}"
        else:
            expr = _get_column_filter_expr(col_name, col_meta)

        if id_pattern:
            if sql == "StockBarcode":
                examples.append(f"{id_pattern} 1/2144→REPLACE({expr},'_','/')='1/2144'")
            else:
                examples.append(f"{id_pattern} VALUE→{expr}='VALUE'")
        elif match_type == "like":
            examples.append(f"{alias} John Smith→{expr} LIKE '%John Smith%'")
        else:
            examples.append(f"{alias}=value→{expr}='value'")

    if not examples:
        return "- (no report-specific examples available)"

    return "\n".join(f"- {ex}" for ex in examples)


def _build_system_prompt(report_key: str = "sales_report", question: str = "") -> str:
    """Build the system prompt with dynamic column sections from report_columns.json."""
    sections = _build_prompt_column_sections(report_key)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        report_key=report_key,
        base_columns=sections["base_columns"],
        computed_metrics=sections["computed_metrics"],
        filter_only=sections["filter_only"],
        dimensions=sections["dimensions"],
        report_rules=_build_report_rules(report_key, question),
        ai_where_examples=_build_ai_where_examples(report_key, question),
    )


def _build_user_prompt(question: str, catalog: str) -> str:
    return f"""## Catalog
{catalog}

## Question
{question}

Return ONLY the JSON object."""
