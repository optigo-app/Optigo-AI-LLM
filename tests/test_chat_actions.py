"""Tests for /chat/action — structured widget actions resolving pending clarify
state deterministically instead of re-parsing display text."""

import asyncio
import types
from pathlib import Path

import pytest

from app.models import ChatActionRequest, ChatRequest, ChatResponse
from app.services import block_builder, chat_service, conversation_store


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(conversation_store, "_LOG_DIR", Path(tmp_path))
    monkeypatch.setattr(conversation_store, "_table_ready", False)
    yield


@pytest.fixture
def _dev_auth(monkeypatch):
    """Dev-mode auth so body identity fields are honored."""
    monkeypatch.setattr(chat_service.settings, "auth_required", False)


def _request() -> types.SimpleNamespace:
    return types.SimpleNamespace(state=types.SimpleNamespace())


def _action_body(action_type: str, payload: dict, session_id: str = "s1") -> ChatActionRequest:
    return ChatActionRequest(
        session_id=session_id,
        action={"type": action_type, "payload": payload, "display": "shown"},
        company_code="DEMO", user_id="u1", response_mode="wide",
    )


def _capture_run_chat(monkeypatch):
    """Replace run_chat with a recorder; returns the captured ChatRequest holder."""
    captured = {}

    async def fake(body: ChatRequest, request, registry, cache):
        captured["body"] = body
        return ChatResponse(answer_text="ok", session_id=body.session_id)

    monkeypatch.setattr(chat_service, "run_chat", fake)
    return captured


class TestPendingStore:
    def test_roundtrip(self):
        sid = conversation_store.new_session_id()
        conversation_store.set_pending(sid, {"kind": "generic", "question": "q"})
        pending = conversation_store.get_pending(sid)
        assert pending["kind"] == "generic"
        assert pending["question"] == "q"
        assert "created_at" in pending

    def test_clear(self):
        sid = conversation_store.new_session_id()
        conversation_store.set_pending(sid, {"kind": "generic"})
        conversation_store.clear_pending(sid)
        assert conversation_store.get_pending(sid) is None

    def test_expired_pending_returns_none(self, monkeypatch):
        sid = conversation_store.new_session_id()
        conversation_store.set_pending(sid, {"kind": "generic"})
        # Backdate created_at beyond the TTL
        monkeypatch.setattr(conversation_store, "_PENDING_TTL_SECONDS", 0)
        assert conversation_store.get_pending(sid) is None

    def test_no_session_is_noop(self):
        conversation_store.set_pending(None, {"kind": "x"})
        assert conversation_store.get_pending(None) is None


class TestBlockActions:
    def test_entity_choice_options_carry_actions(self):
        block = block_builder.build_entity_choice_block("VIDSY", "which field?")
        assert block["blocking"] is True
        # design/SKU/customer/invoice/salesperson/brand/category/branch —
        # codes like TR62 are overwhelmingly design numbers, so it leads.
        assert len(block["options"]) == 8
        assert block["options"][0]["action"]["payload"]["option_id"] == "design"
        assert "sku" in block_builder.ENTITY_OPTION_INJECT
        opt = block["options"][0]
        assert opt["action"]["type"] == "select_option"
        assert opt["action"]["handler"] == "server"
        assert opt["action"]["payload"]["value"] == "VIDSY"
        assert opt["action"]["payload"]["option_id"] in block_builder.ENTITY_OPTION_INJECT

    def test_date_range_block_has_submit_action(self):
        block = block_builder.build_date_range_input_block("pick dates")
        sa = block["submit_action"]
        assert sa["type"] == "set_date_range"
        assert sa["handler"] == "server"
        assert block["blocking"] is True

    def test_clarify_block_options(self):
        blocks = block_builder.build_clarify_blocks("sales_report")
        clarify = blocks[0]
        assert clarify["blocking"] is False
        assert clarify["options"], "clarify should emit structured options"
        assert clarify["options"][0]["action"]["type"] == "send_message"

    def test_chip_options_shape(self):
        opts = block_builder._chip_options(["total sales"])
        assert opts[0]["label"] == "total sales"
        assert opts[0]["action"]["payload"]["message"] == "total sales"


class TestInboundFilters:
    def test_accepts_iso_dates(self):
        out = chat_service._validate_inbound_filters(
            {"start_date": "2026-01-01", "end_date": "2026-01-31"}
        )
        assert out == {"start_date": "2026-01-01", "end_date": "2026-01-31"}

    def test_swaps_reversed_range(self):
        out = chat_service._validate_inbound_filters(
            {"start_date": "2026-01-31", "end_date": "2026-01-01"}
        )
        assert out["start_date"] == "2026-01-01"

    def test_rejects_non_iso_and_unknown_fields(self):
        out = chat_service._validate_inbound_filters(
            {"start_date": "tomorrow", "categoryname": "x'; DROP--"}
        )
        assert out == {}


@pytest.mark.usefixtures("_dev_auth")
class TestRunAction:
    def test_send_message_needs_no_pending(self, monkeypatch):
        captured = _capture_run_chat(monkeypatch)
        body = _action_body("send_message", {"message": "total sales"})
        resp = asyncio.run(
            chat_service.run_action(body, _request(), {}, None)
        )
        assert resp.answer_text == "ok"
        assert captured["body"].question == "total sales"
        assert captured["body"].from_action is True

    def test_no_pending_clarifies(self, monkeypatch):
        _capture_run_chat(monkeypatch)
        body = _action_body("select_option", {"option_id": "customer"})
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.status == "clarify"
        assert "expired" in (resp.answer_text or "")

    def test_select_option_reconstructs_question(self, monkeypatch):
        captured = _capture_run_chat(monkeypatch)
        sid = "sess-entity"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {
            "kind": "entity_disambiguation",
            "question": "VIDSY total sales",
            "entity_value": "VIDSY",
            "report_key": "sales_report",
            "options": {
                "customer": {"label": "Customer", "inject": "customer"},
                "brand": {"label": "Brand", "inject": "brand"},
            },
        })
        body = _action_body("select_option", {"option_id": "customer"}, session_id=sid)
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.answer_text == "ok"
        assert captured["body"].question == "customer VIDSY total sales"
        assert conversation_store.get_pending(sid) is None  # consumed

    def test_select_option_rejects_unknown_option(self, monkeypatch):
        captured = _capture_run_chat(monkeypatch)
        sid = "sess-badopt"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {
            "kind": "entity_disambiguation",
            "question": "X total sales", "entity_value": "X",
            "options": {"customer": {"label": "Customer", "inject": "customer"}},
        })
        body = _action_body("select_option", {"option_id": "evil_field"}, session_id=sid)
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.status == "clarify"
        assert "body" not in captured

    def test_select_option_kind_mismatch(self, monkeypatch):
        captured = _capture_run_chat(monkeypatch)
        sid = "sess-kind"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {"kind": "date_range", "question": "sales"})
        body = _action_body("select_option", {"option_id": "customer"}, session_id=sid)
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.status == "clarify"
        assert "body" not in captured

    def test_set_date_range_explicit(self, monkeypatch):
        captured = _capture_run_chat(monkeypatch)
        sid = "sess-dr"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {
            "kind": "date_range", "question": "top customers by sales",
        })
        body = _action_body(
            "set_date_range",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
            session_id=sid,
        )
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.answer_text == "ok"
        assert captured["body"].question == "top customers by sales"
        assert captured["body"].filters == {
            "start_date": "2026-01-01", "end_date": "2026-01-31",
        }

    def test_set_date_range_preset(self, monkeypatch):
        captured = _capture_run_chat(monkeypatch)
        sid = "sess-drp"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {
            "kind": "date_range", "question": "total sales",
        })
        body = _action_body("set_date_range", {"preset": "last_month"}, session_id=sid)
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.answer_text == "ok"
        f = captured["body"].filters
        assert f["start_date"].endswith("-01")
        assert f["start_date"] < f["end_date"]

    def test_set_date_range_invalid_dates(self, monkeypatch):
        _capture_run_chat(monkeypatch)
        sid = "sess-drbad"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {
            "kind": "date_range", "question": "total sales",
        })
        body = _action_body(
            "set_date_range", {"start_date": "nope", "end_date": "x"}, session_id=sid
        )
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.status == "clarify"

    def test_set_date_range_empty_base_question(self, monkeypatch):
        _capture_run_chat(monkeypatch)
        sid = "sess-dr-empty"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {"kind": "date_range", "question": ""})
        body = _action_body(
            "set_date_range",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
            session_id=sid,
        )
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.status == "clarify"

    def test_unknown_action_type(self, monkeypatch):
        _capture_run_chat(monkeypatch)
        sid = "sess-unknown"
        conversation_store.claim_session(sid, "DEMO:u1")
        conversation_store.set_pending(sid, {"kind": "generic", "question": "x"})
        body = _action_body("delete_everything", {}, session_id=sid)
        resp = asyncio.run(chat_service.run_action(body, _request(), {}, None))
        assert resp.status == "clarify"
