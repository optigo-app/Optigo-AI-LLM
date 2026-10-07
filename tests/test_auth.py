"""Unit tests for auth middleware: session_id validation, cookie parsing."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
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


def _make_request(headers=None, state=None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }
    req = Request(scope)
    for k, v in (state or {}).items():
        setattr(req.state, k, v)
    return req


def _body(**kw):
    return SimpleNamespace(
        company_code=kw.get("company_code"), user_id=kw.get("user_id"),
        appuserid=kw.get("appuserid"), yearcode=kw.get("yearcode"),
        sp=kw.get("sp"),
    )


class TestRequireAdmin:
    """app.main._require_admin — operational endpoints must not be public."""

    def test_no_admin_key_configured_authenticated_user_ok(self):
        from app.main import _require_admin
        req = _make_request(state={"user_id": "u1"})
        with mock.patch.object(settings, "admin_api_key", ""), \
             mock.patch.object(settings, "auth_required", True):
            _require_admin(req)  # should not raise

    def test_no_admin_key_unauthenticated_rejected(self):
        from app.main import _require_admin
        req = _make_request()
        with mock.patch.object(settings, "admin_api_key", ""), \
             mock.patch.object(settings, "auth_required", True):
            with pytest.raises(HTTPException) as exc:
                _require_admin(req)
            assert exc.value.status_code == 401

    def test_admin_key_configured_correct_key_ok(self):
        from app.main import _require_admin
        req = _make_request(headers={"x-admin-key": "s3cret"})
        with mock.patch.object(settings, "admin_api_key", "s3cret"):
            _require_admin(req)  # should not raise

    def test_admin_key_configured_wrong_key_rejected(self):
        from app.main import _require_admin
        req = _make_request(headers={"x-admin-key": "wrong"})
        with mock.patch.object(settings, "admin_api_key", "s3cret"):
            with pytest.raises(HTTPException) as exc:
                _require_admin(req)
            assert exc.value.status_code == 403

    def test_admin_key_configured_no_key_rejected(self):
        from app.main import _require_admin
        req = _make_request()
        with mock.patch.object(settings, "admin_api_key", "s3cret"):
            with pytest.raises(HTTPException) as exc:
                _require_admin(req)
            assert exc.value.status_code == 403


class TestResolveIdentity:
    """app.main._resolve_identity — cookie identity must override body fields."""

    def test_auth_required_ignores_body_identity(self):
        from app.services.chat_service import _resolve_identity
        req = _make_request(state={"company_code": "ACME", "user_id": "u9"})
        body = _body(company_code="EVIL", user_id="attacker")
        with mock.patch.object(settings, "auth_required", True):
            company, user = _resolve_identity(req, body)
        assert (company, user) == ("ACME", "u9")

    def test_dev_mode_allows_body_identity(self):
        from app.services.chat_service import _resolve_identity
        req = _make_request()
        body = _body(company_code="TENANT2", user_id="u2")
        with mock.patch.object(settings, "auth_required", False):
            company, user = _resolve_identity(req, body)
        assert (company, user) == ("TENANT2", "u2")


class TestResolveExecScope:
    """app.main._resolve_exec_scope — ERP execution identity/scope not client-asserted."""

    def test_auth_required_ignores_body_scope(self):
        from app.services.chat_service import _resolve_exec_scope
        req = _make_request(state={"yearcode": "Y2024", "appuserid": "erp.user@x"})
        body = _body(sp=999, yearcode="HACK", appuserid="sa@evil")
        with mock.patch.object(settings, "auth_required", True), \
             mock.patch.object(settings, "real_api_yearcode", "YC"), \
             mock.patch.object(settings, "real_api_sp", 7):
            sp, yearcode, appuserid = _resolve_exec_scope(req, body, {"sp": 42}, "u1")
        assert sp == 42          # report config wins, not body.sp
        assert yearcode == "Y2024"
        assert appuserid == "erp.user@x"

    def test_dev_mode_allows_body_scope(self):
        from app.services.chat_service import _resolve_exec_scope
        req = _make_request()
        body = _body(sp=999, yearcode="YDEV", appuserid="dev@x")
        with mock.patch.object(settings, "auth_required", False):
            sp, yearcode, appuserid = _resolve_exec_scope(req, body, {"sp": 42}, "u1")
        assert (sp, yearcode, appuserid) == (999, "YDEV", "dev@x")


class TestClaimSession:
    """conversation_store.claim_session — sessions bind to (company:user)."""

    @pytest.fixture(autouse=True)
    def temp_db(self, tmp_path, monkeypatch):
        import app.services.conversation_store as cs
        monkeypatch.setattr(cs, "_LOG_DIR", Path(tmp_path))
        monkeypatch.setattr(cs, "_table_ready", False)
        yield

    def test_new_session_binds_first_owner(self):
        from app.services import conversation_store as cs
        assert cs.claim_session("sess1", "ACME:u1") is True
        assert cs.claim_session("sess1", "ACME:u1") is True

    def test_foreign_owner_rejected(self):
        from app.services import conversation_store as cs
        assert cs.claim_session("sess2", "ACME:u1") is True
        assert cs.claim_session("sess2", "OTHER:u2") is False

    def test_same_user_different_company_rejected(self):
        from app.services import conversation_store as cs
        assert cs.claim_session("sess3", "ACME:u1") is True
        assert cs.claim_session("sess3", "TENANT2:u1") is False

    def test_empty_inputs_noop(self):
        from app.services import conversation_store as cs
        # Anonymous sessions can't be bound — treated as no-op, not rejected.
        assert cs.claim_session(None, "ACME:u1") is True
        assert cs.claim_session("sess4", "") is True
