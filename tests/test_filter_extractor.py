"""Unit tests for filter_extractor: _sanitize_dates, merge_with_previous, _parse_json."""

import json
import pytest

from app.services.filter_extractor import (
    FilterExtractError,
    _parse_json,
    _sanitize_dates,
    merge_with_previous,
)


class TestSanitizeDates:
    def test_strips_dates_when_no_date_mentioned(self):
        filters = {"customer_code": "CUST0037", "start_date": "2026-09-01", "end_date": "2026-09-07"}
        mentioned = ["customer_code", "start_date", "end_date"]
        _sanitize_dates(filters, mentioned, "What is the name of customer CUST0037?")
        assert "start_date" not in filters
        assert "end_date" not in filters
        assert "start_date" not in mentioned
        assert "end_date" not in mentioned
        assert filters["customer_code"] == "CUST0037"

    def test_keeps_dates_when_month_mentioned(self):
        filters = {"start_date": "2026-10-01", "end_date": "2026-10-31", "sales_rep": "Neha Joshi"}
        mentioned = ["start_date", "end_date", "sales_rep"]
        _sanitize_dates(filters, mentioned, "Show me sales by Neha Joshi in October 2026")
        assert filters["start_date"] == "2026-10-01"
        assert filters["end_date"] == "2026-10-31"

    def test_keeps_dates_when_year_mentioned(self):
        filters = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
        mentioned = ["start_date", "end_date"]
        _sanitize_dates(filters, mentioned, "Show me all sales in 2026")
        assert "start_date" in filters
        assert "end_date" in filters

    def test_keeps_dates_when_relative_date_mentioned(self):
        filters = {"start_date": "2026-09-01", "end_date": "2026-09-07"}
        mentioned = ["start_date", "end_date"]
        _sanitize_dates(filters, mentioned, "Show me sales this week")
        assert "start_date" in filters
        assert "end_date" in filters

    def test_strips_dates_for_pronoun_question(self):
        filters = {"sales_rep": "Neha Joshi", "start_date": "2026-09-01", "end_date": "2026-09-07"}
        mentioned = ["sales_rep", "start_date", "end_date"]
        _sanitize_dates(filters, mentioned, "Which customers did she sell to?")
        assert "start_date" not in filters
        assert "end_date" not in filters
        assert filters["sales_rep"] == "Neha Joshi"

    def test_no_dates_in_filters_no_crash(self):
        filters = {"customer_code": "CUST0037"}
        mentioned = ["customer_code"]
        _sanitize_dates(filters, mentioned, "Who is customer CUST0037?")
        assert filters == {"customer_code": "CUST0037"}


class TestMergeWithPrevious:
    def test_customer_code_drops_sales_rep_and_dates(self):
        previous = {"sales_rep": "Neha Joshi", "start_date": "2026-10-01", "end_date": "2026-10-31"}
        new = {"customer_code": "CUST0037"}
        mentioned = ["customer_code"]
        result = merge_with_previous(previous, new, mentioned)
        assert result["customer_code"] == "CUST0037"
        assert "sales_rep" not in result
        assert "start_date" not in result
        assert "end_date" not in result

    def test_sales_rep_drops_customer_code(self):
        previous = {"customer_code": "CUST0037"}
        new = {"sales_rep": "Arun Kumar"}
        mentioned = ["sales_rep"]
        result = merge_with_previous(previous, new, mentioned)
        assert result["sales_rep"] == "Arun Kumar"
        assert "customer_code" not in result

    def test_sales_rep_keeps_dates_from_previous(self):
        previous = {"start_date": "2026-10-01", "end_date": "2026-10-31", "sales_rep": "Neha Joshi"}
        new = {"sales_rep": "Arun Kumar"}
        mentioned = ["sales_rep"]
        result = merge_with_previous(previous, new, mentioned)
        assert result["sales_rep"] == "Arun Kumar"
        assert result["start_date"] == "2026-10-01"
        assert result["end_date"] == "2026-10-31"

    def test_dates_overwrite_previous(self):
        previous = {"start_date": "2026-10-01", "end_date": "2026-10-31", "sales_rep": "Neha Joshi"}
        new = {"start_date": "2026-11-01", "end_date": "2026-11-30"}
        mentioned = ["start_date", "end_date"]
        result = merge_with_previous(previous, new, mentioned)
        assert result["start_date"] == "2026-11-01"
        assert result["end_date"] == "2026-11-30"
        assert result["sales_rep"] == "Neha Joshi"

    def test_empty_previous(self):
        new = {"sales_rep": "Neha Joshi", "start_date": "2026-10-01"}
        mentioned = ["sales_rep", "start_date"]
        result = merge_with_previous(None, new, mentioned)
        assert result == {"sales_rep": "Neha Joshi", "start_date": "2026-10-01"}

    def test_no_mentioned_fields_keeps_previous(self):
        previous = {"sales_rep": "Neha Joshi"}
        new = {}
        mentioned = []
        result = merge_with_previous(previous, new, mentioned)
        assert result == {"sales_rep": "Neha Joshi"}


class TestParseJson:
    def test_plain_json(self):
        result = _parse_json('{"filters": {"sales_rep": "Neha"}, "mentioned_fields": ["sales_rep"]}')
        assert result["filters"]["sales_rep"] == "Neha"

    def test_json_with_markdown_fences(self):
        result = _parse_json('```json\n{"filters": {}, "mentioned_fields": []}\n```')
        assert result == {"filters": {}, "mentioned_fields": []}

    def test_json_embedded_in_text(self):
        result = _parse_json('Here are the filters: {"sales_rep": "Neha Joshi"}')
        assert result["sales_rep"] == "Neha Joshi"

    def test_invalid_json_raises(self):
        with pytest.raises(FilterExtractError):
            _parse_json("not json at all")

    def test_array_raises(self):
        with pytest.raises(FilterExtractError):
            _parse_json("[1, 2, 3]")
