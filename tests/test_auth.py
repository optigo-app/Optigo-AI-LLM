"""Unit tests for auth middleware: session_id validation, cookie parsing."""

import pytest

from app.middleware.auth import validate_session_id, _extract_cookie_user


class TestValidateSessionId:
    def test_valid_hex_session(self):
        assert validate_session_id("abc123def456") == "abc123def456"

    def test_valid_with_dashes(self):
        assert validate_session_id("abc-123-def") == "abc-123-def"

    def test_valid_with_underscores(self):
        assert validate_session_id("session_123") == "session_123"

    def test_none_returns_none(self):
        assert validate_session_id(None) is None

    def test_empty_string_returns_none(self):
        assert validate_session_id("") is None

    def test_sql_injection_rejected(self):
        assert validate_session_id("drop table; --") is None

    def test_too_long_rejected(self):
        assert validate_session_id("a" * 65) is None

    def test_special_chars_rejected(self):
        assert validate_session_id("abc@123") is None


class TestExtractCookieUser:
    def test_valid_user_data_cookie(self):
        cookie = "other=123; userData={\"userid\":\"u123\",\"companycode\":\"DEMO\"}; foo=bar"
        result = _extract_cookie_user(cookie)
        assert result is not None
        assert result["userid"] == "u123"
        assert result["companycode"] == "DEMO"

    def test_no_user_data_cookie(self):
        assert _extract_cookie_user("foo=bar; baz=qux") is None

    def test_empty_header(self):
        assert _extract_cookie_user("") is None

    def test_malformed_json(self):
        cookie = "userData={invalid json}"
        assert _extract_cookie_user(cookie) is None
