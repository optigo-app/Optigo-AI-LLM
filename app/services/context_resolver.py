"""Context resolution for follow-up questions.

Two-phase approach:
1. Cheap lexical gate (regex) — detects if a question needs prior context
2. LLM rewrite (only if gate passes) — resolves pronouns/ellipsis into a
   standalone question using conversation history

Design goals:
- Zero latency for fresh (standalone) questions — the gate returns False
  and no LLM call is made
- Fail-soft everywhere — any error returns the original question unchanged
- Deterministic gate — same input always produces the same follow-up verdict
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.middleware.logging import get_logger
from app.services import llm_gateway

logger = get_logger(__name__)


# ── Phase 1: Lexical cues that signal a follow-up ──────────────────────────

# Word-boundary matched against the lowercased question. Deliberately broad —
# a false positive only costs one bounded LLM call (which returns the question
# unchanged when it is already self-contained); a false negative leaves a real
# follow-up unresolved, which is the bug we are fixing.
_FOLLOWUP_CUES: tuple[str, ...] = (
    r"\bwhat about\b", r"\bhow about\b", r"\band what\b", r"\band how\b",
    r"\bthose\b", r"\bthese\b", r"\bthem\b", r"\bthey\b", r"\btheir\b",
    r"\bthat one\b", r"\bthis one\b", r"\bsame\b", r"\bprevious\b",
    r"\bprior\b", r"\bearlier\b", r"\blast one\b", r"\bthe first\b",
    r"\bthe second\b", r"\bthe last\b", r"\bagain\b", r"\binstead\b",
    r"\bbreakdown\b", r"\bwise\b", r"\bcompare\b", r"\bversus\b",
    r"\bgrowth\b", r"\bnow\b", r"\bthis month\b", r"\blast month\b",
    r"\bthis year\b", r"\blast year\b", r"\btoday\b", r"\byesterday\b",
    r"\bby category\b", r"\bby customer\b", r"\bby month\b", r"\bby metal\b",
    r"\btop 5\b", r"\btop 10\b", r"\btop 3\b",
)

# Questions containing these terms are standalone — they name a concrete
# report/metric and do not need context resolution even if they also contain
# a cue word (e.g. "total tax this year" has "this year" but also "total"+"tax").
_STANDALONE_INDICATORS: tuple[str, ...] = (
    r"\bsales\b", r"\btax\b", r"\binvoice\b", r"\bcustomer\b", r"\bstock\b",
    r"\bgold\b", r"\bsilver\b", r"\bdiamond\b", r"\bmetal\b", r"\bplatinum\b",
    r"\blabour\b", r"\blabor\b", r"\bweight\b",
    r"\bgst\b", r"\bmaking\b", r"\bwastage\b", r"\bdiscount\b",
    r"\bdia\b", r"\bcolour stone\b", r"\bcolor stone\b",
)

# Very short questions (< this many chars) are likely follow-ups
# (e.g. "by category", "JS4", "top 5", "gold?").
_SHORT_QUESTION_THRESHOLD = 25

# Compile the cue + standalone regexes once at import time
_FOLLOWUP_RE = re.compile("|".join(_FOLLOWUP_CUES), re.IGNORECASE)
_STANDALONE_RE = re.compile("|".join(_STANDALONE_INDICATORS), re.IGNORECASE)


def looks_like_followup(question: str, history: Optional[List[Dict[str, str]]]) -> bool:
    """Cheap lexical gate — returns True if the question likely needs context.

    Returns False (no rewrite needed) when:
    - There is no conversation history
    - The question contains a standalone indicator (names a concrete metric/report)
      and is longer than the short-question threshold

    Returns True (rewrite needed) when:
    - The question is very short and has no standalone indicator
    - The question contains an explicit follow-up cue word
    """
    if not history or len(history) == 0:
        return False

    q = question.strip()
    if not q:
        return False

    q_lower = q.lower()

    # Very short questions are likely follow-ups (e.g. "by category", "JS4")
    # unless they name a concrete metric/report
    if len(q_lower) < _SHORT_QUESTION_THRESHOLD:
        if not _STANDALONE_RE.search(q_lower):
            return True

    # Check for explicit follow-up cue words
    if _FOLLOWUP_RE.search(q_lower):
        # But not if the question also has a strong standalone indicator
        # (names a concrete metric/report → self-contained)
        if _STANDALONE_RE.search(q_lower):
            return False
        return True

    return False


# ── Phase 2: LLM rewrite ────────────────────────────────────────────────────

_REWRITE_SYSTEM_PROMPT = """You rewrite follow-up questions into standalone questions using conversation history.

Rules:
- Resolve pronouns (it, they, them, those, these) to their referents from history
- Resolve ellipsis ("by category" -> "total sales this year by category")
- Resolve relative dates ("last month", "this year") if context implies a specific period
- Preserve the user's intent — do NOT change the question's meaning
- If the question is already standalone, return it unchanged
- Return ONLY the rewritten question, no explanation, no quotes

Examples:
History: user: total sales this year | assistant: Total sales: Rs 1324 crore
Question: by category
Output: total sales this year by category

History: user: top 5 customers | assistant: 1. Harrine Trivedi...
Question: what about their gold purchases?
Output: gold amount for top 5 customers

History: user: total tax this year | assistant: Tax: Rs 123 lakh
Question: how about last year?
Output: total tax last year

History: user: total sales | assistant: Total sales: Rs 1324 crore
Question: top 5
Output: top 5 customers by total sales
"""

_MAX_HISTORY_TURNS = 4
_MAX_HISTORY_MSG_CHARS = 300
_MAX_REWRITE_TOKENS = 100
_MAX_REWRITE_OUTPUT_CHARS = 500


async def resolve_context(
    question: str,
    history: Optional[List[Dict[str, str]]],
    token_usage: Optional[List[Dict[str, int]]] = None,
) -> str:
    """Rewrite a follow-up question into a standalone question using history.

    Returns the original question unchanged if:
    - The question is not a follow-up (gate returns False)
    - The LLM call fails (fail-soft)
    - The rewritten question is empty or too long
    - Any error occurs (full fail-soft)
    """
    try:
        if not question or not isinstance(question, str):
            return question or ""
        if not history or not isinstance(history, list):
            return question

        if not looks_like_followup(question, history):
            return question

        # Build a compact history string (last N turns)
        history_text = ""
        for msg in history[-_MAX_HISTORY_TURNS:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = str(msg.get("content", ""))[:_MAX_HISTORY_MSG_CHARS]
            history_text += f"{role}: {content}\n"

        # Cap question length to avoid prompt abuse
        safe_question = question[:500]

        messages = [
            {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": f"History:\n{history_text}\nQuestion: {safe_question}\nOutput:"},
        ]

        result = await llm_gateway.chat(
            tier="cheap",
            messages=messages,
            temperature=0.0,
            max_tokens=_MAX_REWRITE_TOKENS,
        )
        rewritten = result.text.strip().strip('"').strip("'").strip()

        # Fail-soft: reject empty or too-long rewrites
        if not rewritten or len(rewritten) > _MAX_REWRITE_OUTPUT_CHARS:
            logger.warning(
                "Context rewrite rejected (len=%d), returning original: %r",
                len(rewritten), question,
            )
            return question

        # Track token usage if caller requested it
        if token_usage is not None:
            token_usage.append(result.usage)

        logger.info("Context resolved: %r -> %r", question, rewritten)
        return rewritten
    except Exception as exc:
        logger.warning("Context resolve failed (fail-soft): %s", exc)
        return question
