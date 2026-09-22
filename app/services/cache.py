import hashlib
import time
from typing import Optional

import diskcache
import numpy as np

from app.config import settings
from app.models import ChatResponse
from app.services import llm_gateway


class ChatCache:
    """Local disk-backed semantic cache keyed by company_code + user_id.

    Matches questions by cosine similarity of embeddings. No external cache server
    is required: diskcache uses a local SQLite-backed file store.
    Entries auto-expire after cache_ttl_seconds. Manual invalidation is available
    via invalidate_report() and invalidate_all().
    """

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or settings.cache_dir
        self.cache = diskcache.Cache(self.cache_dir)

    async def lookup(
        self,
        question: str,
        company_code: str,
        user_id: str,
        response_mode: str = "normal",
        threshold: Optional[float] = None,
        report_key: Optional[str] = None,
    ) -> Optional[ChatResponse]:
        """Return a cached ChatResponse if a semantically similar question exists.

        First tries an exact-match lookup (instant, no API call).
        Falls back to semantic similarity (requires embedding API).

        When `report_key` is provided, the cache is scoped to that report —
        the same question asked on a different report will not return a
        cached entry from another report.
        """
        # Stage 0: Exact-match lookup (no API call needed)
        exact_key = _cache_key(question, company_code, user_id, response_mode, report_key)
        exact_entry = self.cache.get(exact_key)
        if exact_entry and isinstance(exact_entry, dict):
            cached_response = exact_entry.get("response")
            if cached_response:
                try:
                    return ChatResponse(**cached_response)
                except Exception:
                    # Stale schema (e.g. answer was a plain string) — treat as miss
                    return None

        # Stage 1: Semantic similarity lookup (requires embedding API)
        threshold = threshold or settings.cache_similarity_threshold
        try:
            emb_result = await llm_gateway.get_embedding(question)
            embedding = emb_result.embedding
        except llm_gateway.LLMGatewayError:
            # Embedding failure is non-fatal; treat as cache miss.
            return None

        prefix = _key_prefix(company_code, user_id, response_mode, report_key)
        best_score = threshold
        best_response: Optional[dict] = None

        for key in self.cache.iterkeys():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            entry = self.cache.get(key)
            if not entry or not isinstance(entry, dict):
                continue
            cached_embedding = entry.get("embedding")
            cached_response = entry.get("response")
            if not cached_embedding or not cached_response:
                continue
            score = _cosine_similarity(embedding, cached_embedding)
            if score > best_score:
                best_score = score
                best_response = cached_response

        if best_response:
            try:
                return ChatResponse(**best_response)
            except Exception:
                return None
        return None

    async def store(
        self,
        question: str,
        company_code: str,
        user_id: str,
        response: ChatResponse,
        response_mode: str = "normal",
        report_key: Optional[str] = None,
    ) -> None:
        """Store a response with its question embedding.

        Always stores an exact-match entry (no API call needed).
        Also stores an embedding for semantic similarity if the API is available.

        When `report_key` is provided, the entry is scoped to that report so
        it is only returned for the same report on a future lookup.
        """
        key = _cache_key(question, company_code, user_id, response_mode, report_key)

        # Try to compute embedding for semantic similarity
        embedding = None
        try:
            emb_result = await llm_gateway.get_embedding(question)
            embedding = emb_result.embedding
        except llm_gateway.LLMGatewayError:
            # If we can't compute an embedding, still store the exact-match entry.
            pass

        value = {
            "embedding": embedding,
            "response": response.model_dump(),
            "report_key": response.report_key,
            "created_at": time.time(),
        }
        self.cache.set(key, value, expire=settings.cache_ttl_seconds)

    def invalidate_report(self, report_key: str) -> int:
        """Remove all cache entries for a specific report. Returns count of removed entries."""
        removed = 0
        keys_to_delete = []
        for key in self.cache.iterkeys():
            if not isinstance(key, str):
                continue
            entry = self.cache.get(key)
            if not entry or not isinstance(entry, dict):
                continue
            if entry.get("report_key") == report_key:
                keys_to_delete.append(key)
        for key in keys_to_delete:
            self.cache.delete(key)
            removed += 1
        return removed

    def invalidate_all(self) -> int:
        """Clear all cache entries. Returns count of removed entries."""
        count = 0
        for key in list(self.cache.iterkeys()):
            self.cache.delete(key)
            count += 1
        return count

    def close(self) -> None:
        self.cache.close()


def _key_prefix(company_code: str, user_id: str, response_mode: str = "normal", report_key: Optional[str] = None) -> str:
    rk = report_key or "_any"
    return f"{company_code}:{user_id}:{response_mode}:{rk}:"


def _cache_key(question: str, company_code: str, user_id: str, response_mode: str = "normal", report_key: Optional[str] = None) -> str:
    normalized = question.strip().lower()
    question_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    rk = report_key or "_any"
    return f"{company_code}:{user_id}:{response_mode}:{rk}:{question_hash}"


def _cosine_similarity(a: list, b: list) -> float:
    a_vec = np.asarray(a, dtype=np.float32)
    b_vec = np.asarray(b, dtype=np.float32)
    norm = np.linalg.norm(a_vec) * np.linalg.norm(b_vec)
    if norm == 0:
        return 0.0
    return float(np.dot(a_vec, b_vec) / norm)
