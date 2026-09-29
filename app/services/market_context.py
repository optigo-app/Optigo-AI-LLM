"""External market-context detection and governed market-data loading.

Two responsibilities:

1. ``detect_market_context(question)`` — recognises when a user is asking about
   something *outside* the ERP data (market trends, industry demand, live gold
   rates, competitor comparison). The normal governed pipeline still answers
   the internal side; this only flags that an external boundary was crossed so
   the answer can be labelled honestly.

2. ``load_market_snapshot()`` — loads *curated* external observations from
   ``app/market_data/trends.json`` if the deployment provides one. Every entry
   must carry a source and retrieval timestamp so external claims are always
   attributed. When no file exists the system says so instead of fabricating
   market facts.

Deliberately NOT implemented here: live web scraping / search API calls. A
production provider (approved API + credentials) can later fill the same
snapshot schema without changing this module's contract.
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MARKET_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "market_data", "trends.json"
)

_EXTERNAL_WORDS = re.compile(
    r"\b(markets?|industry|industries|competitors?|competition|external|"
    r"globale?|worldwide|international|online|internet|mcx|economy|"
    r"economic|bullion)\b",
    re.IGNORECASE,
)
_TREND_WORDS = re.compile(
    r"\b(trends?|trending|trendy|demand|popular|fashion|fashionable|"
    r"hot[-\s]?selling|in[-\s]?demand|latest)\b",
    re.IGNORECASE,
)
_RATE_WORDS = re.compile(r"\b(rate|rates|price|prices|spot)\b", re.IGNORECASE)
_METAL_WORDS = re.compile(r"\b(gold|silver|platinum|diamond|diamonds)\b", re.IGNORECASE)
_COMPARE_WORDS = re.compile(
    r"\b(compare|comparison|vs\.?|versus|against|benchmark|relative to)\b",
    re.IGNORECASE,
)


def detect_market_context(question: str) -> Optional[Dict[str, Any]]:
    """Detect references to external (non-ERP) market information.

    Returns ``None`` for ordinary internal questions, otherwise::

        {
            "is_external": True,
            "wants_comparison": True/False,
            "topic": "gold_rate" | "trends" | "market",
        }
    """
    if not question or not question.strip():
        return None

    has_external = bool(_EXTERNAL_WORDS.search(question))
    wants_compare = bool(_COMPARE_WORDS.search(question))
    has_metal_rate = bool(_RATE_WORDS.search(question)) and bool(
        _METAL_WORDS.search(question)
    )
    has_trend = bool(_TREND_WORDS.search(question))

    # "gold rate"/"metal price" asks about a live market price, not ERP data —
    # but only when phrased like a market query (current/live/spot/today/market
    # wording or an explicit external word).
    q_low = question.lower()
    marketish_rate = has_metal_rate and (
        has_external or "current" in q_low or "live" in q_low
        or "today" in q_low or "now" in q_low
    )

    if not (has_external or marketish_rate):
        return None

    if marketish_rate:
        topic = "gold_rate"
    elif has_trend:
        topic = "trends"
    else:
        topic = "market"

    return {
        "is_external": True,
        "wants_comparison": wants_compare,
        "topic": topic,
    }


def load_market_snapshot(topic: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load curated market observations from app/market_data/trends.json.

    Schema::

        {
          "updated_at": "ISO timestamp",
          "entries": [
            {
              "topic": "trends" | "gold_rate" | "market",
              "observation": "free text claim",
              "period": "e.g. 2026-Q3",
              "source_name": "who published it",
              "source_url": "where it came from",
              "retrieved_at": "ISO timestamp"
            }
          ]
        }

    Only entries that carry ``source_name`` + ``retrieved_at`` are returned —
    unsourced observations are dropped so nothing unattributed reaches the user.
    """
    if not os.path.isfile(_MARKET_DATA_PATH):
        return []
    try:
        with open(_MARKET_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001 — defensive: bad file must not break chat
        logger.warning("Cannot load market snapshot %s: %s", _MARKET_DATA_PATH, exc)
        return []

    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return []
    sourced = [
        e for e in entries
        if isinstance(e, dict) and e.get("observation")
        and e.get("source_name") and e.get("retrieved_at")
    ]
    if topic:
        topic_entries = [e for e in sourced if e.get("topic") == topic]
        return topic_entries or sourced  # fall back to all sourced entries
    return sourced


def build_market_note(
    ctx: Dict[str, Any], snapshot: List[Dict[str, Any]]
) -> str:
    """Human-readable boundary note prepended to the internal answer."""
    if ctx.get("topic") == "gold_rate":
        internal_note = (
            "the value below is your *billed* metal rate from invoices — "
            "not the live market/MCX rate. "
        )
    else:
        internal_note = "the answer below is from *your own* sales data. "

    if snapshot:
        lines = ["External market context (not from your ERP):"]
        for e in snapshot[:5]:
            src = e.get("source_name", "source")
            when = str(e.get("retrieved_at", ""))[:10]
            url = e.get("source_url") or ""
            tail = f" ({src}, {when})" if when else f" ({src})"
            lines.append(f"• {e['observation']}{tail}" + (f" — {url}" if url else ""))
        lines.append(f"For comparison, {internal_note}")
        return "\n".join(lines)

    base = "I don't have a live market-data source connected right now, so "
    if ctx.get("wants_comparison"):
        return base + (
            "I can't show the external side of that comparison — "
            f"{internal_note}"
        )
    if ctx.get("topic") == "gold_rate":
        return base + f"I can't report a live market rate — {internal_note}"
    return base + f"I can't report current market trends — {internal_note}"
