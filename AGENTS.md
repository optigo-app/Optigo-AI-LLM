# Project Notes

## Environment
- Python 3.13 with venv at `.venv/`. Use `.\.venv\Scripts\python.exe` to run scripts/tests.
- `pytest` is not installed; use `python -m unittest <module>` for tests.
- Config is env-driven via `pydantic_settings` (`.env` file).

## Key Commands
- Run unit tests: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`
- Validate report configs: `.\.venv\Scripts\python.exe scripts\validate_report_configs.py`
- Syntax check Python: `python -c "import ast; ast.parse(open('path').read())"`

## Architecture: LLM Chatbot Mode (GetLLMChatSummary)
- **Shared SP**: `Sample_SQL_Sp/llm_chat_sp.sql` -> `[dbo].[DynamicReport_LLMChatbeta]` — one metadata-driven SP for all reports.
- Report table list + base filter come from `app/report_columns/<report_key>.json` (`tables` + `base_filter` keys), sent in the `p` JSON payload by `_build_p` in `app/services/real_api_client.py`.
- Routing: when `settings.real_api_llm_chat_sp > 0`, `call_real_report_api` routes chat-mode requests to the shared SP. Set via `REAL_API_LLM_CHAT_SP` env var. `0` = legacy (each report SP has its own inlined block).
- **Adding a new report**: create `app/report_columns/<report_key>.json` with `description`, `sp`, `report_id`, `default_metric`, `tables`, `base_filter`, `columns`, `special_metrics`, `filter_key_map`, `metric_catalog`, plus optional `report_keywords`, `intents`, `canonical_values`, `fallback_intent`, `location_aliases`, and `prompt_rules`. Then add the report to `app/registry.json`. No Python or SQL changes are needed for intent routing, filter schema, metric labels, or keyword classification.
- `Sample_SQL_Sp/llm_chat_mode_sp.sql` is DEPRECATED — kept only as historical reference. Do not copy its block into report SPs.
- `app/intent_config.json` is DEPRECATED — kept as a read-only fallback for reports that do not yet define `intents` / `fallback_intent` in their per-report JSON. New intent metadata should go into `app/report_columns/<report>.json`.

## Report Key Aliases (intent-config <-> registry)
- `sales_summary` (legacy intent-config name) <-> `sales_report` (registry + column-registry in `report_columns/sales_report.json`).
- `wip_summary` (legacy intent-config name) <-> `wip_report` (registry + column-registry in `report_columns/wip_report.json`).
- Aliases are bridged in `column_registry.py` (registry -> intent-config lookup) and `intent.py` (`_REPORT_KEY_ALIAS` map). `intent.py` normalizes to the canonical registry key before returning from `classify_question_by_intent`. New reports should use the same key everywhere.

## Per-Report JSON Source of Truth
Each `app/report_columns/<report>.json` now owns:
- `intents` — regex patterns, target metric, aggregation, dimension, sort, limit, and display `label`.
- `fallback_intent` — default intent when no regex matches.
- `report_keywords` — fast keyword classifier routing.
- `canonical_values` — normalization vocabulary for filter values; also drives cross-field value remapping when a value belongs to another field's vocabulary.
- `metric_catalog` — governed metrics with labels and aliases.
- `filter_key_map` — alias -> column mapping for SP filters. Targets may also point to `name_filter_map` keys for name-based LIKE filters.
- `name_filter_map` — SQL expressions for customer/name-style filters (e.g. `customername`, `customerfullname`, `salesrep`).
- `location_aliases` — branch/location normalization.
- `prompt_rules` — report-specific LLM prompt rules.
- Column-level flags: `not_in_where: true` / `computed_only: true` marks a computed column as unsafe for LLM-generated `ai_where` clauses (used by `sql_guard.py`).

Shared cross-report rules are merged from `app/report_columns/_shared/jewelry_knowledge.json` automatically by `column_registry.py`.

## Config-Driven Helpers (`app/services/column_registry.py`)
- `get_report_keywords(report_key)` — keyword list for classifier.
- `get_report_intents(report_key)` / `get_intent_patterns(report_key)` — regex rules.
- `get_canonical_values(report_key, field)` — normalized vocabulary.
- `get_fallback_intent(report_key)` — default intent.
- `get_location_aliases(report_key)` — branch normalization.
- `get_computed_only_names(report_key)` — columns marked `not_in_where: true` / `computed_only: true` that the SQL guard rejects in LLM-generated `ai_where` clauses.
- `derive_filter_schema(report_key)` — auto-derived filter schema for `validator.py`.

## WIP Report (wip_report / wip_summary)
- Main table: `ProductionManagement_SerialNoBook` (alias `PS` in the SP).
- `sp`: 245 (shared LLM-chat SP), `report_id`: 21.
- Base filter: `MasterManagement_productionstatusid NOT IN (28) AND iStore_IsJobClosed = 0 AND ismerged = 0 AND isETARejected = 0` (jobs in progress, not closed, not merged, not outsource-status-28, not ETA-rejected).
- 60 columns extracted from `Sample_SQL_Sp/wip_report.sql` (DynamicWIPReportDatabeta). Computed dimensions: `department` (production status CASE), `jobtype` (Regular/Sample/Repair/Recast), `size` (varies by category), `workername`, `JobLocation`, `withpip`.
- Default metric: `JobCost`, default dimension: `department`.
- Intent patterns, fallback, keywords, and canonical values now live in `report_columns/wip_report.json`.

## Test Notes
- `tests/test_unit_core.py` tests use `sales_summary` as report_key; `sales_report` is the column registry key. An alias `sales_summary -> sales_report` is added in `column_registry.py`, `real_api_client.py`, and `intent.py` so both keys resolve to the same config.
- `sales_summary` is the legacy intent-config name; `sales_report` is the column-registry / registry name.
- All 102 tests (test_unit_core, test_sql_guard, test_registry_sync) pass with `python -m unittest discover -s tests -v`.
- `scripts/validate_report_configs.py` checks required keys, type correctness, intent metric/dimension references, canonical-value references, and duplicate aliases.
