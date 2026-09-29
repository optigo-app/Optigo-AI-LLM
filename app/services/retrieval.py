import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from app.config import settings
from app.services import llm_gateway
from app.services.verified_queries import load_examples

_INDEX_PATH = Path(settings.cache_dir) / "retrieval.db"
_EMBEDDING_CACHE: OrderedDict[str, List[float]] = OrderedDict()
_EMBEDDING_CACHE_LIMIT = 1000


def _tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9#/_-]+", text.lower())


def _keyword_rank(question: str, examples: List[Dict[str, Any]]) -> List[int]:
    conn = None
    try:
        _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_INDEX_PATH, timeout=10)
        fingerprint = hashlib.sha256(json.dumps([e["question"] for e in examples]).encode()).hexdigest()
        table = f"retrieval_{fingerprint[:16]}"
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(question)")
        indexed_tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'retrieval_[0-9a-f]*'") if not any(row[0].endswith(suffix) for suffix in ("_data", "_idx", "_content", "_docsize", "_config"))]
        if len(indexed_tables) > 20:
            for stale in indexed_tables:
                if stale != table:
                    conn.execute(f"DROP TABLE IF EXISTS {stale}")
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count != len(examples):
            conn.execute(f"DELETE FROM {table}")
            conn.executemany(f"INSERT INTO {table}(rowid, question) VALUES (?, ?)", [(i + 1, e["question"]) for i, e in enumerate(examples)])
            conn.commit()
        query = " OR ".join(f'"{token}"' for token in _tokens(question))
        rows = conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY bm25({table})", (query,)).fetchall() if query else []
        return [row[0] - 1 for row in rows]
    except sqlite3.Error:
        wanted = set(_tokens(question))
        return sorted(range(len(examples)), key=lambda i: len(wanted & set(_tokens(examples[i]["question"]))), reverse=True)
    finally:
        if conn is not None:
            conn.close()


async def retrieve_examples(question: str, report_key: str = "", top_k: int = 0) -> List[Dict[str, Any]]:
    examples = [e for e in load_examples() if not report_key or e.get("report_key") == report_key]
    if not examples:
        return []
    keyword = _keyword_rank(question, examples)
    vector: List[int] = []
    try:
        def cache_key(text: str) -> str:
            return f"{settings.embedding_model}:{text}"

        missing = [e["question"] for e in examples if cache_key(e["question"]) not in _EMBEDDING_CACHE]
        if missing:
            values = await llm_gateway.get_embeddings(missing)
            for text, value in zip(missing, values):
                _EMBEDDING_CACHE[cache_key(text)] = value
            while len(_EMBEDDING_CACHE) > _EMBEDDING_CACHE_LIMIT:
                _EMBEDDING_CACHE.popitem(last=False)
        query_result = await llm_gateway.get_embedding(question)
        query = np.asarray(query_result.embedding, dtype=np.float32)
        scores = []
        for i, item in enumerate(examples):
            key = cache_key(item["question"])
            value = _EMBEDDING_CACHE[key]
            _EMBEDDING_CACHE.move_to_end(key)
            candidate = np.asarray(value, dtype=np.float32)
            norm = np.linalg.norm(query) * np.linalg.norm(candidate)
            scores.append((float(np.dot(query, candidate) / norm) if norm else 0.0, i))
        vector = [i for _, i in sorted(scores, reverse=True)]
    except Exception:
        vector = []
    scores: Dict[int, float] = {}
    for ranking in (keyword, vector):
        for rank, index in enumerate(ranking):
            scores[index] = scores.get(index, 0) + 1 / (60 + rank)
    limit = top_k or settings.retrieval_top_k
    merged = [examples[index] for index, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:max(limit, 10)]]
    if settings.retrieval_rerank and len(merged) > 1:
        try:
            prompt = "Rank these verified ERP examples for the query. Return JSON {\"indexes\":[...]}.\nQuery: " + question + "\n" + "\n".join(f"{i}: {item['question']}" for i, item in enumerate(merged))
            result = await llm_gateway.chat(tier="cheap", messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=100, response_format={"type": "json_object"})
            order = json.loads(result.text).get("indexes", [])
            ranked = [merged[i] for i in order if isinstance(i, int) and 0 <= i < len(merged)]
            ranked.extend(item for item in merged if item not in ranked)
            merged = ranked
        except Exception:
            pass
    return merged[:limit]


def format_examples(examples: List[Dict[str, Any]]) -> str:
    if not examples:
        return ""
    lines = ["Verified examples (follow when relevant):"]
    for item in examples:
        lines.append(f"- {item['question']} -> {json.dumps(item.get('query_plan', {}), separators=(',', ':'))}")
    return "\n".join(lines)
