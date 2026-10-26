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
    r"\babove\b", r"\bthis\b", r"\bthat\b", r"\bits\b",
    r"\bthat one\b", r"\bthis one\b", r"\bsame\b", r"\bprevious\b",
    r"\bprior\b", r"\bearlier\b", r"\blast one\b", r"\bthe first\b",
    r"\bthe second\b", r"\bthe last\b", r"\bagain\b", r"\binstead\b",
    r"\bbreakdown\b", r"\bwise\b", r"\bcompare\b", r"\bversus\b",
    r"\bgrowth\b", r"\bnow\b", r"\bthis month\b", r"\blast month\b",
    r"\bthis year\b", r"\blast year\b", r"\btoday\b", r"\byesterday\b",
    r"\bby category\b", r"\bby customer\b", r"\bby month\b", r"\bby metal\b",
    r"\btop 5\b", r"\btop 10\b", r"\btop 3\b",
    # Hinglish follow-up cues ("aur category wise?", "uska gold?", "aur batao")
    r"\baur\b", r"\biska\b", r"\buska\b", r"\binka\b", r"\binke\b", r"\buske\b",
    r"\bbhi\b", r"\bphir\b", r"\bab\b", r"\bwala\b", r"\bwali\b",
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
# Referential cues (pronouns/demonstratives) point back at a specific earlier
# turn — 'above design total diamond weight' still needs rewriting even though
# it names a metric. These bypass the standalone-indicator guard.
_REFERENTIAL_RE = re.compile(
    r"\b(above|same|its|their|them|they|those|these|that one|this one|"
    r"last one|the first|the second|the last|previous|prior|earlier)\b",
    re.IGNORECASE,
)
_DATE_CHANGE_RE = re.compile(r"\b(change|different|another|other|modify|update|switch|try)\b.*\b(date|period|range)\b|\b(date|period|range)\b.*\b(change|different|another|other|modify|update|switch)\b", re.IGNORECASE)
_DATE_VALUE_RE = re.compile(
    r"\b(today|yesterday|tomorrow|this\s+(week|month|year|quarter)|"
    r"last\s+(week|month|year|quarter)|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"q[1-4]|fy\s*'?\d{0,4}|financial\s+year|fiscal\s+year|fortnight|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4})\b",
    re.IGNORECASE,
)


def needs_date_clarification(question: str) -> bool:
    return bool(_DATE_CHANGE_RE.search(question) and not _DATE_VALUE_RE.search(question))


def ambiguous_entity_value(question: str) -> Optional[str]:
    lowered = question.lower()
    field_words = (
        "customer", "client", "buyer", "customer type", "customer segment",
        "business class", "salesperson", "sales rep", "employee", "brand",
        "branch", "category", "manufacturer", "manufacture", "mfg", "maker",
        "supplier", "collection",
        "product type", "metal", "wastage", "invoice", "bill",
        "job", "design", "sku",
    )
    if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in field_words):
        return None
    match = re.match(
        r"^\s*(?:(?:what|show|give|tell)\s+(?:me\s+)?(?:(?:is|was|are|were)\s+)?(?:the\s+)?|how\s+much\s+)?"
        r"(?!(?:what|show|give|tell|how|the|a|an|is|was|are|were)\s)"
        r"([A-Za-z][A-Za-z0-9._&-]*(?:\s+[A-Za-z][A-Za-z0-9._&-]*){0,2}?)\s+"
        r"(?:total\s+)?(?:sales?|revenue|amount|value)\b",
        question,
        re.IGNORECASE,
    )
    if not match:
        # 'why did 440 perform well' — the entity sits in the 'did <X> perform'
        # slot with no metric word after it, so the pattern above can't see
        # it. Bare codes land here for the same design/SKU/customer clarify.
        why_m = re.search(
            r"\bdid\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9._&-]*(?:\s+[A-Za-z][A-Za-z0-9._&-]*){0,3}?)\s+perform\w*\b",
            question, re.IGNORECASE,
        )
        if not why_m:
            return None
        value = why_m.group(1).strip()
    else:
        value = match.group(1).strip()
        # Anything after the metric word must be empty, punctuation, a date word,
        # or an explicit "no filters" directive. Trailing business words mean the
        # matched phrase was never an entity ("overall total sales for brand").
        tail = question[match.end():].strip()
        if tail:
            tail_clean = tail.strip(" ?.!").lower()
            if tail_clean not in ("", "please", "without filters", "no filters",
                                  "without filter") and not _DATE_VALUE_RE.fullmatch(tail_clean):
                return None
    excluded = {
        "what", "show", "give", "tell", "how much", "total",
        "sale", "sales", "revenue", "amount", "value",
        "tax", "total tax", "gst", "cgst", "sgst", "igst", "tcs", "tds",
        "order", "orders", "wip", "work in progress",
        "today", "yesterday", "this month", "last month", "this year", "last year",
        "compare", "comparison", "growth", "monthly", "daily", "weekly", "yearly",
        "this", "last", "previous", "current",
        "top", "best", "worst", "bottom", "average", "avg", "my", "our", "all",
        "highest", "lowest", "biggest", "smallest", "most", "least",
        "me", "us", "we", "you", "something", "everything", "anything",
        "trending", "trend", "interesting", "summary", "details", "it", "its",
        "overall", "overall total", "grand total", "without filters",
        "net", "gross", "tomorrow", "next month", "next week",
        # Hinglish question words — 'aaj kitna sale hua' asks 'how much sale
        # today', not an entity named 'aaj kitna'.
        "aaj", "aj", "aaj ka", "aj ka", "kal", "parso", "kitna", "kitne",
        "kitni", "kya", "kya hai", "batao", "dikhao", "bataye", "hua",
        "hai", "hain", "tha", "tha kya", "is mahine", "pichle mahine",
        "is saal", "pichle saal", "agle", "agla",
    }
    # "compare our diamond sales" — the phrase before 'sales' can be multi-word;
    # if it starts with a comparison/period keyword it is not an entity.
    first_word = value.lower().split()[0] if value.split() else ""
    if value.lower() in excluded or first_word in excluded:
        return None
    # Period expressions ('august', 'q3', 'fy26', 'last quarter') are date
    # filters, not entity values — don't clarify them.
    if _DATE_VALUE_RE.search(value):
        return None
    return value


def is_probable_person_name(value: str) -> bool:
    """Heuristic: does an ambiguous entity look like a person's name?

    Multi-word alphabetic values ('Deepak Pareek', 'Harrine Trivedi') and
    single ALL-CAPS alphabetic tokens ('VIDSY') are overwhelmingly customer
    names in this domain — safe to try the customer filter directly instead
    of forcing a clarification round-trip. Codes with digits/underscores
    ('TR62', 'ANE_143') stay ambiguous.
    """
    words = value.split()
    if len(words) >= 2:
        return all(w[:1].isalpha() for w in words)
    return (
        len(words) == 1
        and words[0].isalpha()
        and words[0].isupper()
        and len(words[0]) >= 3
    )


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

    # Referential cues always resolve against context — a metric word in the
    # same sentence doesn't make 'above design' standalone.
    if _REFERENTIAL_RE.search(q_lower):
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
- PRESERVE the attribute being asked: "its type" / "its brand" -> "<entity> customer type" / "<entity> brand", never replace it with a sales/amount question
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

History: user: customer code vidsy total sales | assistant: Total sales: Rs 37 lakh
Question: tell me its customer type
Output: customer code vidsy customer type
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


# ── Compound question splitting ─────────────────────────────────────────────
# "last month highest selling category and most valuable sold tag" asks TWO
# independent questions. Split only on top-level joins when every clause is
# an independent information request — never inside a metric phrase
# ("total diamond wt and pcs" is one ask served by extra_metrics) or an
# entity name ("Tushar and Sons").
_COMPOUND_JOIN_RE = re.compile(
    r"\s*(?:;\s*|,\s*also\s+| and also | as well as | along with | and )",
    re.IGNORECASE,
)

# A clause qualifies as an independent request when it asks for a ranked/top
# entity, a "by X" breakdown, or a named metric.
_COMPOUND_REQUEST_RE = re.compile(
    r"\b(?:top|bottom|best|worst|highest|lowest|least|most|largest|smallest|"
    r"average|avg|total|count|number of|how many|how much|revenue|sales|"
    r"sale|amount|weight|tax|discount|collection|orders|jobs|value)\b"
    r"|\bby\s+\w+|\b\w+\s+wise\b",
    re.IGNORECASE,
)

# The split only pays off when at least one clause asks for an entity —
# "total sales and net weight" stays single so extra_metrics renders one
# combined metric table instead of two calls.
_COMPOUND_ENTITY_RE = re.compile(
    r"\b(?:top|bottom|best|worst|highest|lowest|least|most|largest|smallest)\s+\w+"
    r"|\bby\s+\w+|\b\w+\s+wise\b",
    re.IGNORECASE,
)

# Metric abbreviations are intra-question joins, not clause boundaries:
# "diamond wt and pcs" asks one thing (weight + pieces).
# A "by X" / "per X" / "X wise" modifier shared by the whole compound —
# "Revenue and tax breakdown per customer group" applies the grouping to
# both clauses. Stops before date/function words.
_DIM_PHRASE_RE = re.compile(
    r"\b(?:per|by)\s+(?:(?!\b(?:last|this|next|past|previous|in|on|at|for|from|to|and|or|during|between|the)\b)[a-z0-9]+\s*){1,3}"
    r"|\b[a-z]+(?:\s+[a-z]+)?\s+wise\b",
    re.IGNORECASE,
)

_COMPOUND_UNIT_ONLY_RE = re.compile(
    r"^(?:total\s+|net\s+|gross\s+)?\w{0,12}\s*(?:wt|wts|weight|pcs|pieces|ctw|"
    r"carat|amount|count|qty|quantity|rate|value)s?\.?$",
    re.IGNORECASE,
)


def _clause_report_keys(parts: List[str]) -> List[str]:
    """Best-match report key per clause, via report_keywords scoring.

    Same scoring as the keyword classifier: word-boundary keyword hits,
    multi-word keywords weighted by word count, generic entity nouns
    ("jobs", "records") at half weight. Returns "" for a clause with no
    keyword signal at all.
    """
    from app.services.column_registry import _REGISTRY, get_generic_routing_terms

    generic = get_generic_routing_terms()
    winners: List[str] = []
    for part in parts:
        lowered = part.lower()
        best_key, best_score, best_pos = "", 0.0, 10**9
        for report_key, cfg in _REGISTRY.items():
            score, first_pos = 0.0, 10**9
            for kw in cfg.get("report_keywords", []) or []:
                m = re.search(r"\b" + re.escape(kw.lower()) + r"\b", lowered)
                if not m:
                    continue
                first_pos = min(first_pos, m.start())
                if kw.lower() in generic:
                    score += 0.5
                else:
                    score += len(kw.split())
            # Score ties go to the report whose keyword appears earliest —
            # the leading noun is usually the clause's subject ('tax
            # breakdown per customer group' is a tax ask, not customer).
            if score > best_score or (score == best_score and score > 0 and first_pos < best_pos):
                best_key, best_score, best_pos = report_key, score, first_pos
        winners.append(best_key)
    return winners


def split_compound_question(question: str, max_parts: int = 3) -> List[str]:
    """Split a compound question into independent sub-questions.

    Conservative: returns ``[question]`` unless every clause is an
    independent request AND at least one asks for an entity/breakdown —
    OR every clause resolves to a different report (cross-report compounds
    like "total sales and total GST" can't be served by extra_metrics,
    which only exists within one report).
    A shared date phrase ("last month ...") is copied onto clauses that
    have none, so "last month X and Y" keeps both parts in-period.
    """
    if not question or not isinstance(question, str):
        return [question]
    q = question.strip().strip("?.!")
    if len(q) > 200:
        return [question]
    parts = [p.strip(" ,;?.!") for p in _COMPOUND_JOIN_RE.split(q)]
    parts = [p for p in parts if p]
    if not (2 <= len(parts) <= max_parts):
        return [question]
    if not any(_COMPOUND_ENTITY_RE.search(p) for p in parts):
        # No entity ask — still split when the clauses clearly target
        # different reports; each must reach its own governed pipeline.
        clause_reports = _clause_report_keys(parts)
        if any(not r for r in clause_reports) or len(set(clause_reports)) < 2:
            return [question]
    for p in parts:
        # One-word clauses are fine when they're a request word themselves —
        # 'Revenue and tax breakdown...' splits into 'Revenue' + 'tax ...'.
        # Entity names ('Tushar and Sons') still bail on the REQUEST check.
        if len(p.split()) < 2 and not _COMPOUND_REQUEST_RE.search(p):
            return [question]
        if _COMPOUND_UNIT_ONLY_RE.match(p):
            return [question]
        # A bare "by X" fragment is a modifier, not a question by itself
        if re.match(r"^(?:by|per)\s+\w+\s*$", p, re.IGNORECASE):
            return [question]
        if not _COMPOUND_REQUEST_RE.search(p):
            return [question]
    # Inherit the first shared date phrase onto parts that lack one
    date_phrase = ""
    for p in parts:
        m = _DATE_VALUE_RE.search(p)
        if m:
            date_phrase = m.group(0)
            break
    if date_phrase:
        parts = [p if _DATE_VALUE_RE.search(p) else f"{p} {date_phrase}" for p in parts]
    # Inherit a shared grouping modifier ("per customer group", "by branch",
    # "category wise") onto clauses that don't carry their own — the phrase
    # belongs to the whole compound, not just the clause it appears in.
    dim_phrase = ""
    for p in parts:
        m = _DIM_PHRASE_RE.search(p)
        if m:
            dim_phrase = m.group(0).strip()
            break
    if dim_phrase:
        parts = [
            p if _DIM_PHRASE_RE.search(p) or _COMPOUND_ENTITY_RE.search(p)
            else f"{p} {dim_phrase}"
            for p in parts
        ]
    return parts


def contextual_entity_inject(
    ambiguous_value: str,
    last_dimension: str,
    prev_answer: str,
    dimension_aliases: Dict[str, str],
) -> str:
    """Word to inject before an ambiguous entity value, resolved from the
    previous turn's breakdown dimension.

    After "top 10 designs" (dimension=designno) the follow-up "why did 440
    perform well?" refers to design 440 — the value appears in the previous
    answer's ranking rows, so the dimension's shortest alias ("design") is
    injected as the entity type. Returns "" when context can't resolve it.
    """
    if not (ambiguous_value and last_dimension and prev_answer):
        return ""
    if not re.search(
        rf"(?<![\w]){re.escape(ambiguous_value)}(?![\w])",
        prev_answer, re.IGNORECASE,
    ):
        return ""
    return next(
        (a for a, col in dimension_aliases.items()
         if col == last_dimension and " " not in a),
        "",
    )
