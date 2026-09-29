from typing import Any, Dict, List, Optional, Union, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str
    company_code: str
    user_id: str
    filters: Optional[Dict[str, Any]] = None
    export: bool = False
    session_id: Optional[str] = None
    yearcode: Optional[str] = None 
    sp: Optional[int] = None 
    appuserid: Optional[str] = None 
    ip_address: Optional[str] = None 
    report_name: Optional[str] = None  
    pid: Optional[int] = None
    response_mode: str = "normal"
    regenerate: bool = False  # when True, bypass cache and generate a fresh answer


class ReportInfo(BaseModel):
    key: str
    name: str


class AnswerData(BaseModel):
    """Structured answer for the frontend — replaces plain text answer."""
    type: str = "metric"  # metric | table | list | text
    title: str = ""
    value: str = ""
    raw_value: Optional[float] = None
    unit: str = ""
    unit_label: str = ""
    currency: Optional[str] = None
    subtext: str = ""


class PeriodInfo(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    label: str = ""


class Metadata(BaseModel):
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    record_count: int = 0
    token_usage: Dict[str, Any] = Field(default_factory=dict)
    intent_confidence: Optional[float] = None
    complexity: Optional[str] = None
    results_limited: bool = False
    cache_hit: bool = False


class Actions(BaseModel):
    download_url: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    status: str = "success"  # success | error | clarify
    report: Optional[ReportInfo] = None
    question: Optional[str] = None
    answer: Optional[AnswerData] = None
    period: Optional[PeriodInfo] = None
    blocks: Optional[List[Dict[str, Any]]] = None
    filters: Optional[Dict[str, Any]] = None
    metadata: Optional[Metadata] = None
    actions: Optional[Actions] = None
    error: Optional[str] = None
    # Legacy fields for backward compatibility
    report_key: Optional[str] = None
    answer_text: Optional[str] = None
    assumptions: List[str] = Field(default_factory=list)
    download_url: Optional[str] = None
    token_usage: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None

    def model_post_init(self, __context: Any) -> None:
        """Auto-set status based on error field and blocks content."""
        if self.error:
            self.status = "error"
        elif self.blocks:
            for b in self.blocks:
                if b.get("type") == "clarify":
                    self.status = "clarify"
                    break
                if b.get("type") == "error":
                    self.status = "error"
                    break


# ── Block schema for wide response mode ───────────────────────────────────────

class TextBlock(BaseModel):
    type: Literal["text"]
    content: str

class HeadingBlock(BaseModel):
    type: Literal["heading"]
    content: str

class TableBlock(BaseModel):
    type: Literal["table"]
    columns: List[str]
    rows: List[List[str]]

class ListBlock(BaseModel):
    type: Literal["list"]
    style: Literal["bullet", "number"]
    items: List[str]

class ChartPoint(BaseModel):
    x: str
    y: float

class ChartSeries(BaseModel):
    name: str
    data: List[ChartPoint]

class ChartBlock(BaseModel):
    type: Literal["chart"]
    chart_type: Literal["line", "bar", "pie"]
    x_key: str
    series: List[ChartSeries] = Field(..., min_length=1)

class AssumptionBlock(BaseModel):
    type: Literal["assumption"]
    content: str

class ErrorBlock(BaseModel):
    type: Literal["error"]
    content: str

class SuggestionsBlock(BaseModel):
    type: Literal["suggestions"]
    items: List[str]


# ── Richer block types for jewelry-industry answers ────────────────────────────

class MetricCardBlock(BaseModel):
    """A prominent single-metric card (big number + label + optional subtext).

    Renders as a hero metric in the frontend. Used for the primary answer
    of simple aggregate questions (total sales, total gold weight, etc.).
    Carries raw_value + unit + currency + unit_label so the frontend can
    apply its own formatting if it prefers the raw number.
    """
    type: Literal["metric_card"]
    label: str
    value: str  # pre-formatted display value (e.g. "₹2.47 crore (₹24,75,56,602)")
    raw_value: Optional[float] = None
    unit: str = ""  # currency | weight | count | rate | text
    currency: Optional[str] = None  # only set when unit == "currency"
    unit_label: str = ""  # g, ct, pcs, %
    subtext: str = ""  # e.g. "Transactions: 2,123" or "Date: 2026-09-15"


class BreakdownBlock(BaseModel):
    """A component-breakdown table for jewelry material splits.

    Shows the metal / diamond / colourstone / labour / other breakup that
    jewellery users expect. Columns: Component | Amount | Weight | Pieces.
    Any column may be empty ("—") when the source does not provide it.
    """
    type: Literal["breakdown"]
    title: str = "Component Breakdown"
    columns: List[str] = Field(default_factory=lambda: ["Component", "Amount", "Weight", "Pieces"])
    rows: List[List[str]]
    raw_data: Optional[List[Dict[str, Any]]] = None


class PeriodBlock(BaseModel):
    """A chip/badge showing the date range (and any applied filters) for the answer.

    Renders as a small pill in the frontend so the user always knows which
    period the numbers belong to.
    """
    type: Literal["period"]
    label: str = ""  # e.g. "Date Range"
    value: str  # e.g. "2026-09-01 to 2026-09-15" or "Today"
    filters: Optional[Dict[str, Any]] = None


class GlossaryBlock(BaseModel):
    """A small legend explaining jewelry-industry abbreviations used in the answer.

    Renders as a collapsible/tooltip section. e.g. {"ctw": "Carat Total Weight",
    "Tunch": "Gold purity percentage"}.
    """
    type: Literal["glossary"]
    title: str = "Glossary"
    terms: Dict[str, str]  # abbreviation -> full form


class ClarifyBlock(BaseModel):
    """A clarification prompt asking the user to be more specific.

    Renders as a question + clickable suggestion chips in the frontend.
    """
    type: Literal["clarify"]
    content: str
    suggestions: List[str] = Field(default_factory=list)


class DateRangeInputBlock(BaseModel):
    type: Literal["date_range_input"]
    title: str = "Select date range"
    content: str
    start_field: str = "start_date"
    end_field: str = "end_date"
    presets: List[Dict[str, str]] = Field(default_factory=list)
    submit_label: str = "Apply date range"
    submit_message_template: str = "Use date range {start_date} to {end_date}"


class ChoiceOption(BaseModel):
    label: str
    value: str
    message: str


class ChoiceInputBlock(BaseModel):
    type: Literal["choice_input"]
    title: str
    content: str
    field: str
    value: str
    options: List[ChoiceOption] = Field(..., min_length=1)
    allow_custom: bool = False


class SourcesBlock(BaseModel):
    """A list of data sources used to generate the answer."""
    type: Literal["sources"]
    items: List[str]


Block = Union[
    TextBlock, HeadingBlock, TableBlock, ListBlock,
    ChartBlock, AssumptionBlock, ErrorBlock, SuggestionsBlock,
    MetricCardBlock, BreakdownBlock, PeriodBlock, GlossaryBlock,
    ClarifyBlock, DateRangeInputBlock, ChoiceInputBlock, SourcesBlock,
]


class ExportResponse(BaseModel):
    report_key: Optional[str] = None
    download_url: Optional[str] = None
    assumptions: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    token_usage: Dict[str, Any] = Field(default_factory=dict)


class FilterSchemaField(BaseModel):
    type: str  # 'string', 'number', 'integer', 'date', 'boolean', 'enum'
    required: bool = False
    default: Optional[Any] = None
    allowed: Optional[List[Any]] = None
    description: Optional[str] = None


class ReportRegistryEntry(BaseModel):
    report_key: str
    pid: Optional[int] = None  # unique report ID from frontend
    api_endpoint: str
    method: str = "GET"
    description: str
    filter_schema: Dict[str, FilterSchemaField]
    response_mode: str  # 'aggregate' or 'list'


class ValidationResult(BaseModel):
    cleaned: Dict[str, Any]
    assumptions: List[str]
    errors: List[str] = []


# ── Feedback (thumbs up/down) ─────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    session_id: str
    question: str
    answer: str = ""
    report_key: str = ""
    metric: str = ""
    rating: str  # "up" or "down"
    comment: str = ""  # optional user comment
    company_code: str = ""
    user_id: str = ""
    corrected_metric: str = ""  # user suggests correct metric when down-voting
    failure_reason: str = ""
    query_plan: Dict[str, Any] = Field(default_factory=dict)
    confidence: Optional[float] = None
    latency_ms: Optional[float] = None


class FeedbackResponse(BaseModel):
    status: str  # "ok" or "error"
    message: str = ""
    feedback_id: Optional[int] = None
