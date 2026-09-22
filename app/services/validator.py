import json
import logging
from datetime import date
from typing import Any, Dict, List

from app.models import FilterSchemaField, ReportRegistryEntry, ValidationResult

logger = logging.getLogger(__name__)


def _resolve_default(value: Any) -> Any:
    """Resolve dynamic default tokens into concrete values."""
    if value == "today":
        return date.today().isoformat()
    if value == "first_day_of_month":
        return date.today().replace(day=1).isoformat()
    return value


def _coerce_value(field: FilterSchemaField, raw: Any) -> Any:
    """Validate and coerce a raw value to the schema type."""
    if raw is None:
        return None

    ftype = field.type

    if ftype == "string":
        return str(raw)

    if ftype == "integer":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected integer, got {raw!r}") from exc

    if ftype == "number":
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected number, got {raw!r}") from exc

    if ftype == "boolean":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "yes", "y"):
            return True
        if str(raw).lower() in ("false", "0", "no", "n"):
            return False
        raise ValueError(f"Expected boolean, got {raw!r}")

    if ftype in ("date", "enum"):
        return str(raw)

    return raw


def validate_and_fill(
    schema: Dict[str, FilterSchemaField], raw_filters: Dict[str, Any]
) -> ValidationResult:
    """Generic schema-driven validator. Returns cleaned filters and assumptions."""
    cleaned: Dict[str, Any] = {}
    assumptions: List[str] = []
    errors: List[str] = []

    for field_name, field in schema.items():
        raw = raw_filters.get(field_name)
        defaulted = False

        if raw is None or raw == "":
            raw = _resolve_default(field.default)
            defaulted = True

        try:
            coerced = _coerce_value(field, raw)
        except ValueError as exc:
            errors.append(f"{field_name}: {exc}")
            continue

        if coerced is None and field.required:
            errors.append(f"{field_name}: required field is missing")
            continue

        if coerced is not None and field.allowed is not None and coerced not in field.allowed:
            errors.append(
                f"{field_name}: value {coerced!r} not in allowed list {field.allowed}"
            )
            continue

        cleaned[field_name] = coerced
        if defaulted:
            assumptions.append(f"{field_name} defaulted to {_display(coerced)}")

    # Flag user-provided filters for transparency.
    for field_name, value in raw_filters.items():
        if field_name in schema and value not in (None, ""):
            assumptions.append(f"{field_name} provided as {_display(value)}")

    return ValidationResult(cleaned=cleaned, assumptions=assumptions, errors=errors)


def _display(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def load_registry(path: str) -> Dict[str, ReportRegistryEntry]:
    """Load the report registry from JSON and return a dict keyed by report_key.

    The filter_schema is AUTO-DERIVED from report_columns.json (the single
    source of truth for columns and filters). If registry.json still contains
    a manual filter_schema, it is ignored and the derived one is used.
    """
    from app.services.column_registry import derive_filter_schema

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries: Dict[str, ReportRegistryEntry] = {}
    for entry in data.get("reports", []):
        rk = entry["report_key"]
        # Auto-derive filter_schema from report_columns.json
        derived_schema = derive_filter_schema(rk)
        if derived_schema:
            entry["filter_schema"] = derived_schema
            logger.info("Auto-derived filter_schema for %s (%d filters)",
                        rk, len(derived_schema))
        entries[rk] = ReportRegistryEntry(**entry)

    return entries
