# Project Notes

## Environment
- Python 3.13 with venv at `.venv/`. Use `.\.venv\Scripts\python.exe` to run scripts/tests.
- `pytest` is installed (9.x) and required: many test files use plain classes/fixtures that `unittest discover` silently skips. Always run `python -m pytest tests` for the full suite.
- Config is env-driven via `pydantic_settings` (`.env` file).

## Key Commands
- Run unit tests: `.\.venv\Scripts\python.exe -m pytest tests -q` (full suite incl. pytest-style files)
- Validate report configs: `.\.venv\Scripts\python.exe scripts\validate_report_configs.py`
- Syntax check Python: `python -c "import ast; ast.parse(open('path').read())"`

## Architecture: LLM Chatbot Mode (GetLLMChatSummary)
- **Shared SP**: `Sample_SQL_Sp/llm_chat_sp.sql` -> `[dbo].[DynamicReport_LLMChatbeta]` — one metadata-driven SP for all reports.
- Report table list + base filter come from `app/report_columns/<report_key>.json` (`tables` + `base_filter` keys), sent in the `p` JSON payload by `_build_p` in `app/services/real_api_client.py`.
- Optionally, a report can define `source_query_file` (a generated `.sql` file under `app/report_queries/`). When present, `_build_p` sends the query text as `SourceQuery` in the `p` payload. The shared SP wraps it as `FROM (<SourceQuery>) AS DI` instead of querying base tables directly. This avoids creating per-report views or tables in each tenant database.
- `count_distinct` aggregation is only supported by the SP **without** a dimension. `QueryPlan.validate_against_registry()` coerces `count_distinct` + dimension -> `count` (per-group row counts). The SP source has a `count_distinct` grouped branch for future use, but the app never sends that combination today.
- "details/list/breakup" on `unique_customers`/`unique_designs`/`total_count`: `main.py` injects the entity dimension via `column_registry.get_detail_dimension()` and forces `aggregation=count` so the answer is a per-entity count table.
- Table-specific metrics can define `columns.<metric>.table_metric_exprs` keyed by every configured base table. `_build_p` sends this as `TableMetricExprs`; the shared SP chooses the matching expression for each table and fails closed if a non-empty map is incomplete. Computed dimensions can similarly use `table_dimension_exprs`, and computed filters can be emitted as `TableDimensionFilters`. A column that uses these features must provide an entry for every `tables` entry. SQL expression/filter payload fields are XML-escaped because the real API transport treats `p` as XML.
- Routing: when `settings.real_api_llm_chat_sp > 0`, `call_real_report_api` routes chat-mode requests to the shared SP. Set via `REAL_API_LLM_CHAT_SP` env var. `0` = legacy (each report SP has its own inlined block).
- **Adding a new report**: create `app/report_columns/<report_key>.json` with `description`, `sp`, `report_id`, `default_metric`, `tables`, `base_filter`, `columns`, `special_metrics`, `filter_key_map`, `metric_catalog`, plus optional `report_keywords`, `intents`, `canonical_values`, `fallback_intent`, `location_aliases`, and `prompt_rules`. Then add the report to `app/registry.json`. No Python or SQL changes are needed for intent routing, filter schema, metric labels, or keyword classification.
- After editing `Sample_SQL_Sp/llm_chat_sp.sql`, deploy `[dbo].[DynamicReport_LLMChatbeta]` to SQL Server before expecting production behavior; the local `.sql` file is only the source artifact.
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
- `filter_invalid_values` — per-filter-key literals that must never be accepted as filter values (e.g. metric words like `diamond`/`net` leaked into `customername`/`categoryname`). Dropped in `QueryPlan.validated_filters`, name-filter WHERE clauses, and `ai_where` clause cleanup (`parse_result.validate_ai_where`). Column-level `filter.invalid_values` works the same for real columns.
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

## External Market Context (market_context.py)
- `app/services/market_context.py` detects questions that reference non-ERP information (market/industry trends, live gold rates, competitor comparison) via `detect_market_context()`.
- The governed pipeline still answers the internal side; `main.py` prepends `build_market_note()` to the answer and appends a `market_context` block with sources (wide mode). Works in both `/chat` and `/chat_stream`.
- Curated external observations load from `app/market_data/trends.json` (see `trends.example.json` for schema). Only entries with `observation` + `source_name` + `retrieved_at` are used — unsourced claims are dropped.
- No live web/scraping provider is wired; an approved market API can later fill the same snapshot schema.

## WIP Report (wip_report / wip_summary)
- Main table: `ProductionManagement_SerialNoBook` (alias `PS` in the SP).
- `sp`: 245 (shared LLM-chat SP), `report_id`: 21.
- Base filter (must mirror the legacy SP — `ISNULL` wrappers are required because most rows have NULL flags): `ISNULL(MasterManagement_productionstatusid,0) NOT IN (28) AND ISNULL(iStore_IsJobClosed,0)=0 AND ISNULL(ismerged,0)=0 AND ISNULL(isAllSplitedJobsNotMerged,0)<>1 AND ISNULL(IsSplitProcess_NotCompleted,0)=0 AND ISNULL(isETARejected,0)=0 AND ISNULL(Mastermanagement_MFG_JobLastLocationid,0) IN (1,2,3,4,5) AND (ISNULL(isReverseEngage,0)<>1 OR (ISNULL(isReverseEngage,0)=1 AND ISNULL(REcnt,0)=0 AND ISNULL(ReverseEngage_parent_serialjobno,'')<>''))`. The legacy SP also had a `MasterManagement_productionstatus` master-table subquery (excludes deleted statuses) — omitted because a `base_filter` subquery cannot be tenant-DB-qualified; status 28 exclusion covers the practical case.
- 60 columns extracted from `Sample_SQL_Sp/wip_report.sql` (DynamicWIPReportDatabeta). Computed dimensions: `department` (production status CASE), `jobtype` (Regular/Sample/Repair/Recast), `size` (varies by category), `workername`, `JobLocation`, `withpip`.
- Default metric: `JobCost`, default dimension: `department`.
- Intent patterns, fallback, keywords, and canonical values now live in `report_columns/wip_report.json`.

## Order Report (order_report)
- `sp`: 245 (shared LLM-chat SP), grouped grain — the real `DynamicOrderReportbeta` counts join-rows (job x quotation line) inside groups that pass a `HAVING` (`SUM(Job_Quantity)>0` AND at least one non-`advfgpurchaseneworder`/`advfgpurchasereorder` row, grouped by 22 keys).
- `source_query_file`: `app/report_queries/order_report.sql` reproduces that pipeline as a derived table — JobData union (`JobManagement_JobMaster` + `_closed` with `IsQuotationJobBillArchived=3`), LEFT JOIN `QuotationAggregates`, and the group-HAVING via `SUM(...) OVER (PARTITION BY <same keys>)` so row grain is preserved for filters/dimensions.
- The 15 quotation metrics (`GrossWt`…`Amount`) use `me: ISNULL(DI.Q*,0)` pointing at per-line columns exposed by the source query — NOT correlated `job_JobNo` subqueries (those double-count at join-row grain).
- `QuotationJob`/`total_count` = join-row count (matches the real report's "Quote Jobs" total). `ETAJobs`/`RemJobs` split by `JobManagement_ProgressStatusMasterid NOT IN / IN (1,12)`.
- `tables`/`base_filter`/`table_filters` stay configured as a graceful fallback — if the source-query file is missing, the SP falls back to flat table mode (base_filter is idempotent over the DI columns; `table_filters` is ignored in SourceQuery mode).
- SourceQuery rules: must start with SELECT, no `;`/`--`/`/*`/`*/`, no CTE at top level (a derived table cannot contain `WITH` — use nested subqueries), `[dbo].` prefixes are auto-rewritten to the tenant DB. See `app/report_queries/sales_report.sql` for the convention.

## Chat Pipeline (post-refactor)
- `app/main.py` is routes/middleware only (~550 lines). All orchestration lives in `app/services/chat_service.py`.
- `chat_service.run_chat(body, request, registry, cache) -> ChatResponse` is the single pipeline used by BOTH `/chat` and `/chat/stream`. `stream_chat()`/`stream_chat_events()` translate the same ChatResponse into SSE events — no divergent logic.
- SSE contract: `{"session_id"}` first → `{"error"}|{"status":"clarify",...,"done":true}|metadata + {"chunk"}* + {"done":true}`.
- `prepare_chat()` (identity + session binding + response mode) is shared; `run_chat` stamps `request.state.session_id`.
- Do NOT add bookkeeping after a `done` yield in SSE paths — clients disconnect on `done` and post-done code silently never runs.

## Widget Actions (`/chat/action`)
- Interactive blocks carry a canonical `action` object `{type, handler, payload, display}` — clicks POST to `/chat/action` (+`/v1/`, +`/chat/action/stream`) instead of re-sending the label as text. `format_version: "blocks-v1"` on ChatResponse.
- `chat_service.run_action` validates the payload against **server-side pending state** (`conversation_store.set_pending/get_pending`, `session_context.pending_json`, 30-min TTL) recorded when the clarify was emitted — a click can only pick among offered options, never inject fields.
- Action types: `send_message` (verbatim question, no pending needed), `select_option` (entity disambiguation — deterministic rewrite "VIDSY sales" → "design VIDSY sales" via `ENTITY_OPTION_INJECT`), `set_date_range` (explicit `start_date`/`end_date` or `preset` → injected as `body.filters`, which `run_chat` merges into `validated_filters` via `_validate_inbound_filters` — dates only).
- `ChatRequest.from_action=True` keeps pending alive during the re-run; a fresh user-typed question clears it. `display` text is written to history so follow-ups resolve against the choice.
- Entity options come from `block_builder.ENTITY_OPTION_INJECT` (design, sku, customer, invoice, salesperson, brand, category, branch — ordered by jewellery-ERP likelihood) + `ENTITY_OPTION_LABELS`.
- `blocking: true` on clarify/choice blocks means the frontend should steer to the offered options (Dialogflow-style blocking chips).
- `static/chat_demo.html` is the reference block renderer + action client (served at `/static/`, public path).

## Pipeline Tracing (observability)
- `app/services/pipeline_trace.py` — `StageTracer` records per-stage latency/outcome for each chat request: `context`, `semantic_parse`, `execute`, `answer`.
- Emitted as `{"event": "pipeline_trace", ...}` JSONL entries in `logs/audit.log`; also feeds `stage_latencies` + `failure_stage` in the existing `request_trace` entries.
- Wired into both `/chat` and `/chat/stream` (including the multi-period early-return paths). Filter `audit.log` for `pipeline_trace` to replay a request's stage timings and find which stage failed.
- `scripts/real_scenario_test.py` — `EXPECTED_OVERRIDE` maps intentionally cross-report questions (e.g. tax-by-customer → sales) so route-checks measure real mistakes; the summary categorizes failures as clarify / route_miss / http / exception / backend.

## Test Notes
- `tests/test_unit_core.py` tests use `sales_summary` as report_key; `sales_report` is the column registry key. An alias `sales_summary -> sales_report` is added in `column_registry.py`, `real_api_client.py`, and `intent.py` so both keys resolve to the same config.
- `sales_summary` is the legacy intent-config name; `sales_report` is the column-registry / registry name.
- The full suite has ~400 tests; run it with `python -m pytest tests -q`.
- `scripts/validate_report_configs.py` checks required keys, type correctness, intent metric/dimension references, canonical-value references, and duplicate aliases.
