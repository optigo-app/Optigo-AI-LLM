"""Unit tests for validator: validate_and_fill, _coerce_value, _resolve_default."""

import pytest
from datetime import date

from app.models import FilterSchemaField
from app.services.validator import validate_and_fill


def _schema():
    return {
        "start_date": FilterSchemaField(type="date", required=False, default=None),
        "end_date": FilterSchemaField(type="date", required=False, default=None),
        "sales_rep": FilterSchemaField(type="string", required=False, default=None),
        "customer_code": FilterSchemaField(type="string", required=False, default=None),
        "branch": FilterSchemaField(type="string", required=False, default=None),
        "limit": FilterSchemaField(type="integer", required=False, default=10),
    }


class TestValidateAndFill:
    def test_passes_through_provided_values(self):
        result = validate_and_fill(_schema(), {"sales_rep": "Neha Joshi", "start_date": "2026-10-01"})
        assert result.cleaned["sales_rep"] == "Neha Joshi"
        assert result.cleaned["start_date"] == "2026-10-01"
        assert result.errors == []

    def test_fills_defaults(self):
        result = validate_and_fill(_schema(), {})
        assert result.cleaned["limit"] == 10
        assert "limit defaulted to 10" in " ".join(result.assumptions)

    def test_coerces_integer(self):
        result = validate_and_fill(_schema(), {"limit": "25"})
        assert result.cleaned["limit"] == 25

    def test_invalid_integer_raises_error(self):
        result = validate_and_fill(_schema(), {"limit": "abc"})
        assert any("limit" in e for e in result.errors)

    def test_none_values_get_default(self):
        result = validate_and_fill(_schema(), {"sales_rep": None})
        assert result.cleaned["sales_rep"] is None

    def test_provided_filters_flagged_in_assumptions(self):
        result = validate_and_fill(_schema(), {"sales_rep": "Neha Joshi"})
        assert any("sales_rep provided" in a for a in result.assumptions)

    def test_empty_string_treated_as_missing(self):
        result = validate_and_fill(_schema(), {"sales_rep": ""})
        assert result.cleaned["sales_rep"] is None
