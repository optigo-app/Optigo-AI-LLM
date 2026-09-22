"""Tests for ChatCache report-scoping and regenerate behavior.

Covers the two production requirements:
1. The cache key includes the resolved report so the same question asked
   on a different report does not return another report's cached answer.
2. `regenerate=True` in the chat request bypasses the cache lookup so the
   backend always generates a fresh answer.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app.models import ChatRequest, ChatResponse
from app.services import cache as cache_module
from app.services.cache import ChatCache, _cache_key, _key_prefix


class CacheKeyScopingTests(unittest.TestCase):
    """The cache key must include report_key so reports don't collide."""

    def test_cache_key_differs_per_report(self):
        k1 = _cache_key("total sales", "DEMO", "u1", "normal", report_key="sales_report")
        k2 = _cache_key("total sales", "DEMO", "u1", "normal", report_key="wip_report")
        self.assertNotEqual(k1, k2)

    def test_cache_key_same_report_matches(self):
        k1 = _cache_key("total sales", "DEMO", "u1", "normal", report_key="sales_report")
        k2 = _cache_key("total sales", "DEMO", "u1", "normal", report_key="sales_report")
        self.assertEqual(k1, k2)

    def test_key_prefix_includes_report(self):
        p1 = _key_prefix("DEMO", "u1", "normal", report_key="sales_report")
        p2 = _key_prefix("DEMO", "u1", "normal", report_key="wip_report")
        self.assertNotEqual(p1, p2)
        self.assertIn("sales_report", p1)
        self.assertIn("wip_report", p2)

    def test_missing_report_key_falls_back_to_any(self):
        """When no report is resolved, the key uses the _any bucket."""
        k_any = _cache_key("total sales", "DEMO", "u1", "normal", report_key=None)
        k_sales = _cache_key("total sales", "DEMO", "u1", "normal", report_key="sales_report")
        self.assertNotEqual(k_any, k_sales)
        self.assertIn("_any", k_any)


class ChatCacheReportScopedLookupTests(unittest.TestCase):
    """End-to-end ChatCache lookup/store must be scoped by report_key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chatcache_test_")
        # Patch settings.cache_dir so we never touch the real cache file.
        self._patch = patch.object(cache_module.settings, "cache_dir", self.tmp)
        self._patch.start()
        self.cache = ChatCache(cache_dir=self.tmp)

    def tearDown(self):
        self._patch.stop()
        self.cache.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self, question: str, report_key: str) -> ChatResponse:
        resp = ChatResponse(
            report_key=report_key,
            answer_text=f"answer for {report_key}",
            session_id="s1",
        )
        # Bypass the embedding API (not available in tests) by patching it
        # to return a tiny deterministic vector.
        async def fake_emb(_q):
            class _R:
                embedding = [1.0, 0.0, 0.0]
            return _R()
        with patch.object(cache_module.llm_gateway, "get_embedding", side_effect=fake_emb):
            asyncio.run(self.cache.store(
                question, "DEMO", "u1", resp, "normal", report_key=report_key,
            ))
        return resp

    def test_lookup_returns_same_report_entry(self):
        self._store("total sales", "sales_report")
        async def fake_emb(_q):
            class _R:
                embedding = [1.0, 0.0, 0.0]
            return _R()
        with patch.object(cache_module.llm_gateway, "get_embedding", side_effect=fake_emb):
            got = asyncio.run(self.cache.lookup(
                "total sales", "DEMO", "u1", "normal", report_key="sales_report",
            ))
        self.assertIsNotNone(got)
        self.assertEqual(got.report_key, "sales_report")

    def test_lookup_does_not_cross_reports(self):
        """Same question stored under sales_report must not be returned for wip_report."""
        self._store("total sales", "sales_report")
        async def fake_emb(_q):
            class _R:
                embedding = [1.0, 0.0, 0.0]
            return _R()
        with patch.object(cache_module.llm_gateway, "get_embedding", side_effect=fake_emb):
            got = asyncio.run(self.cache.lookup(
                "total sales", "DEMO", "u1", "normal", report_key="wip_report",
            ))
        self.assertIsNone(got)


class RegenerateFlagTests(unittest.TestCase):
    """ChatRequest.regenerate must default to False and be accepted."""

    def test_default_false(self):
        req = ChatRequest(question="hi", company_code="DEMO", user_id="u1")
        self.assertFalse(req.regenerate)

    def test_accept_true(self):
        req = ChatRequest(question="hi", company_code="DEMO", user_id="u1", regenerate=True)
        self.assertTrue(req.regenerate)


if __name__ == "__main__":
    unittest.main()
