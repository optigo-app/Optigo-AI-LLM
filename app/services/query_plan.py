from datetime import date, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.services.column_registry import _REGISTRY, derive_filter_schema


class QueryComplexity(str, Enum):
    simple = "SIMPLE"
    moderate = "MODERATE"
    complex = "COMPLEX"


class IntentAlternative(BaseModel):
    intent: str
    confidence: float = Field(ge=0, le=1)


class DateRange(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    preset: Optional[str] = None

    def resolved(self) -> Dict[str, str]:
        today = date.today()
        if self.preset == "today":
            start = end = today
        elif self.preset == "yesterday":
            start = end = today - timedelta(days=1)
        elif self.preset == "this_month":
            start, end = today.replace(day=1), today
        elif self.preset == "last_month":
            end = today.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        elif self.preset == "this_year":
            start, end = today.replace(month=1, day=1), today
        elif self.preset == "last_year":
            start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        elif self.preset == "this_week":
            start, end = today - timedelta(days=today.weekday()), today
        elif self.preset == "last_week":
            start = today - timedelta(days=today.weekday() + 7)
            end = start + timedelta(days=6)
        else:
            values = {"start_date": self.start or "", "end_date": self.end or ""}
            if values["start_date"] and values["end_date"] and values["start_date"] > values["end_date"]:
                values["start_date"], values["end_date"] = values["end_date"], values["start_date"]
            return {k: v for k, v in values.items() if v}
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}


class QueryFilter(BaseModel):
    field: str
    operator: str = "="
    value: Any

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, value: str) -> str:
        allowed = {"=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "IN", "BETWEEN", "IS NULL", "IS NOT NULL"}
        normalized = value.upper()
        if normalized not in allowed:
            raise ValueError(f"Unsupported filter operator: {value}")
        return normalized


class QueryStep(BaseModel):
    type: str
    metric: Optional[str] = None
    date_range: Optional[DateRange] = None
    limit: Optional[int] = None


class QueryPlan(BaseModel):
    intent: str
    report_key: str
    metric: str
    confidence: float = Field(default=0.6, ge=0, le=1)
    alternatives: List[IntentAlternative] = Field(default_factory=list)
    date_range: Optional[DateRange] = None
    filters: List[QueryFilter] = Field(default_factory=list)
    aggregation: str = "sum"
    dimension: Optional[str] = None
    group_by: List[str] = Field(default_factory=list)
    sort_by: Optional[str] = "desc"
    limit: int = Field(default=1, ge=1)
    extra_metrics: List[str] = Field(default_factory=list)
    ai_where: Optional[str] = None
    clarify: Optional[str] = None
    complexity: QueryComplexity = QueryComplexity.simple
    steps: List[QueryStep] = Field(default_factory=list)
    results_limited: bool = False

    @classmethod
    def from_parse_result(cls, parsed: Any, confidence: Optional[float] = None) -> "QueryPlan":
        raw_filters = parsed.filters or {}
        date_filter = parsed.date_filter or None
        plan = cls(
            intent=getattr(parsed, "intent", None) or f"semantic_{parsed.metric}", report_key=parsed.report_key,
            metric=parsed.metric, confidence=confidence if confidence is not None else getattr(parsed, "confidence", 0.6),
            alternatives=[IntentAlternative(**item) for item in getattr(parsed, "alternatives", [])],
            date_range=DateRange(**date_filter) if date_filter else None,
            filters=[QueryFilter(field=k, value=v) for k, v in raw_filters.items()],
            aggregation=parsed.aggregation, dimension=parsed.dimension, group_by=[parsed.dimension] if parsed.dimension else [],
            sort_by=parsed.sort, limit=max(1, parsed.limit), extra_metrics=parsed.extra_metrics,
            ai_where=parsed.ai_where, clarify=parsed.clarify,
        )
        plan.classify_complexity("")
        return plan

    def classify_complexity(self, question: str) -> QueryComplexity:
        lowered = question.lower()
        if self.steps or self.extra_metrics or any(k in lowered for k in ("compare", "compared", "versus", " vs ", "growth", "trend")):
            self.complexity = QueryComplexity.complex
        elif self.dimension or len(self.filters) > 1 or self.extra_metrics:
            self.complexity = QueryComplexity.moderate
        else:
            self.complexity = QueryComplexity.simple
        return self.complexity

    def validate_against_registry(self) -> "QueryPlan":
        cfg = _REGISTRY.get(self.report_key)
        if not cfg:
            raise ValueError(f"Unknown report: {self.report_key}")
        columns = cfg.get("columns", {})
        metrics = cfg.get("metric_catalog", {})
        special = cfg.get("special_metrics", {})
        valid_metrics = set(metrics) | set(columns) | set(special)
        if self.metric not in valid_metrics:
            raise ValueError(f"Unknown metric {self.metric!r} for {self.report_key}")
        invalid_metrics = [metric for metric in self.extra_metrics if metric not in valid_metrics]
        if invalid_metrics:
            raise ValueError(f"Unknown extra metrics for {self.report_key}: {', '.join(invalid_metrics)}")
        self.alternatives = [
            item for item in self.alternatives
            if item.intent in _REGISTRY and item.intent != self.report_key
        ][:2]
        if self.dimension:
            from app.services.column_registry import get_dimension_aliases
            resolved_dim = get_dimension_aliases(self.report_key).get(
                self.dimension.strip().lower()
            )
            if resolved_dim:
                self.dimension = resolved_dim
            meta = columns.get(self.dimension)
            if not meta or meta.get("filter_only") or meta.get("not_available"):
                raise ValueError(f"Invalid dimension {self.dimension!r} for {self.report_key}")
        allowed_filters = set(derive_filter_schema(self.report_key))
        invalid = [item.field for item in self.filters if item.field not in allowed_filters]
        if invalid:
            raise ValueError(f"Invalid filters for {self.report_key}: {', '.join(invalid)}")
        requested = self.limit
        if not self.dimension:
            self.limit = 1
        elif requested <= 50:
            self.limit = requested
        else:
            self.limit = min(requested, settings.breakdown_row_limit)
            self.results_limited = self.limit < requested
        return self

    def validated_filters(self) -> Dict[str, Any]:
        from app.services.column_registry import get_filter_invalid_values

        def _is_invalid(field: str, value: Any) -> bool:
            invalid_l = get_filter_invalid_values(self.report_key, field)
            if not invalid_l:
                return False
            if isinstance(value, list):
                return all(str(v).lower().strip() in invalid_l for v in value)
            return str(value).lower().strip() in invalid_l

        result = {}
        for item in self.filters:
            if _is_invalid(item.field, item.value):
                continue
            result[item.field] = item.value
        if self.date_range:
            result.update(self.date_range.resolved())
        return result
