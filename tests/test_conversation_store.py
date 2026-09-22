"""Unit tests for conversation_store: session management, messages, filters."""

import os
import tempfile

import pytest

from app.services import conversation_store


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch):
    """Use a temp directory for the conversation DB during tests."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        monkeypatch.setattr(conversation_store.settings, "dummy_db_path", os.path.join(tmpdir, "dummy.db"))
        yield


class TestSessionManagement:
    def test_new_session_id_is_16_chars(self):
        sid = conversation_store.new_session_id()
        assert len(sid) == 16

    def test_new_session_id_is_unique(self):
        ids = {conversation_store.new_session_id() for _ in range(100)}
        assert len(ids) == 100


class TestMessages:
    def test_add_and_retrieve_messages(self):
        sid = conversation_store.new_session_id()
        conversation_store.add_message(sid, "user", "Hello")
        conversation_store.add_message(sid, "assistant", "Hi there")
        msgs = conversation_store.get_messages(sid)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"

    def test_get_messages_respects_limit(self):
        sid = conversation_store.new_session_id()
        for i in range(15):
            conversation_store.add_message(sid, "user", f"msg {i}")
        msgs = conversation_store.get_messages(sid, limit=5)
        assert len(msgs) == 5
        assert msgs[0]["content"] == "msg 10"
        assert msgs[4]["content"] == "msg 14"

    def test_get_messages_none_session_returns_empty(self):
        assert conversation_store.get_messages(None) == []


class TestReportContext:
    def test_set_and_get_last_report(self):
        sid = conversation_store.new_session_id()
        conversation_store.set_last_report(sid, "sales_summary")
        assert conversation_store.get_last_report(sid) == "sales_summary"

    def test_get_last_report_none_session(self):
        assert conversation_store.get_last_report(None) is None

    def test_get_last_report_unset_session(self):
        sid = conversation_store.new_session_id()
        assert conversation_store.get_last_report(sid) is None


class TestFiltersContext:
    def test_set_and_get_last_filters(self):
        sid = conversation_store.new_session_id()
        filters = {"sales_rep": "Neha Joshi", "start_date": "2026-10-01"}
        conversation_store.set_last_filters(sid, filters)
        result = conversation_store.get_last_filters(sid)
        assert result == filters

    def test_get_last_filters_none_session(self):
        assert conversation_store.get_last_filters(None) is None

    def test_get_last_filters_unset_session(self):
        sid = conversation_store.new_session_id()
        assert conversation_store.get_last_filters(sid) is None

    def test_set_empty_filters(self):
        sid = conversation_store.new_session_id()
        conversation_store.set_last_filters(sid, {})
        assert conversation_store.get_last_filters(sid) == {}

    def test_overwrite_filters(self):
        sid = conversation_store.new_session_id()
        conversation_store.set_last_filters(sid, {"sales_rep": "Neha Joshi"})
        conversation_store.set_last_filters(sid, {"customer_code": "CUST0037"})
        result = conversation_store.get_last_filters(sid)
        assert result == {"customer_code": "CUST0037"}
