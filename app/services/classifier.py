import json
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.config import settings
from app.models import ReportRegistryEntry
from app.services import llm_gateway
from app.services.intent import classify_question_by_intent
from app.services.column_registry import get_report_keywords, get_generic_routing_terms


def _load_report_keywords() -> Dict[str, List[str]]:
    """Build a fresh report-keywords map from the column registry.

    Returns a new dict each time. Callers that need a reload should import and
    call `reload_config` from app.services.intent first.
    """
    from app.services.column_registry import _REGISTRY
    keywords: Dict[str, List[str]] = {}
    for report_key, cfg in _REGISTRY.items():
        rk = cfg.get("report_keywords", [])
        if rk:
            keywords[report_key] = list(rk)
    return keywords

# Questions that are clearly out of scope for an ERP business-report chatbot.
# Matches common non-business queries (greetings, small talk, general knowledge, etc.)
_GREETING_PATTERNS = [
    re.compile(r"^\s*(hi|hello|hey|good morning|good evening|good afternoon|greetings)\s*$", re.I),
    re.compile(r"^\s*(hi+|hello+|hey+)\s+(there|everyone|all|guys|sir|madam|team)\s*$", re.I),
]

_HELP_PATTERNS = [
    re.compile(r"\b(who are you|what are you|what can you do|help me|how do you work|what can you help with)\b", re.I),
]

_THANKS_PATTERNS = [
    re.compile(r"\b(thank you|thanks|bye|goodbye|see you|that's all|that is all)\b", re.I),
]

_OUT_OF_SCOPE_PATTERNS = [
    re.compile(r"\b(what is the weather|tell me a joke|write a poem|write a story)\b", re.I),
    re.compile(r"\b(who won|who is the president|who is the prime minister|what is the capital)\b", re.I),
    re.compile(r"\b(translate|recipe|cook|workout|exercise)\b", re.I),
    re.compile(r"\b(do you love|are you human|are you ai|what is your name)\b", re.I),
]

def _all_report_keywords(registry: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return the union of all configured report keywords.

    Used to distinguish genuine business questions from greetings/help/out-of-scope
    queries. A minimal built-in fallback is used when no registry is provided.
    """
    if registry:
        keywords: List[str] = []
        for report_key, cfg in _load_report_keywords().items():
            if report_key in registry:
                keywords.extend(cfg)
        # Also include report key names themselves as signals
        keywords.extend(registry.keys())
        return [k.lower() for k in keywords]
    # Fallback for tests/standalone usage before registry is loaded
    return ["sales", "purchase", "stock", "order", "outstanding", "invoice",
            "discount", "report", "branch", "customer"]


def _build_greeting_response(
    registry: Optional[Dict[str, Any]] = None,
    report_key: Optional[str] = None,
) -> str:
    return "Hello! How can I help you today?"


def _build_help_response(registry: Optional[Dict[str, Any]] = None) -> str:
    bullets = []
    examples = []
    if registry:
        for report_key in list(registry.keys())[:5]:
            desc = getattr(registry[report_key], "description", report_key.replace("_", " "))
            bullets.append(f"- {desc}")
        # Pull example phrases from configured keywords
        for report_key, cfg in _load_report_keywords().items():
            if report_key in registry and cfg:
                phrase = cfg[0]
                if phrase:
                    examples.append(f"- \"{phrase}\"")
    if not bullets:
        bullets = ["- Sales and revenue reports", "- Stock and purchase reports"]
    if not examples:
        examples = ["- \"Total sales today\"", "- \"Top 5 customers by revenue\""]
    return (
        "I'm OptigoAI, your business reporting assistant. I can help you with:\n"
        + "\n".join(bullets)
        + "\n\nTry asking things like:\n"
        + "\n".join(examples[:4])
    )


def _build_thanks_response(registry: Optional[Dict[str, Any]] = None) -> str:
    return (
        "You're welcome! Feel free to ask OptigoAI if you have any more questions "
        "about your business reports."
    )


def _build_out_of_scope_response(registry: Optional[Dict[str, Any]] = None) -> str:
    names = []
    if registry:
        names = [k.replace("_", " ") for k in list(registry.keys())[:5]]
    scope = ", ".join(names) if names else "sales, purchases, stock, and more"
    return (
        "I'm OptigoAI, designed to help with your business reports — "
        f"{scope}. Could you ask me something about your business data?"
    )


def get_greeting_response(
    question: str,
    registry: Optional[Dict[str, Any]] = None,
    report_key: Optional[str] = None,
) -> Optional[str]:
    """Return a friendly static message for greetings and out-of-scope questions.

    The response text is generated from the supplied report registry so it stays
    accurate as reports are added or removed. Returns None if the question is
    in-scope and should proceed normally.
    """
    q = question.strip().lower()
    if not q:
        return None

    erp_keywords = _all_report_keywords(registry)

    def _has_erp_keyword(text: str) -> bool:
        return any(kw in text for kw in erp_keywords)

    # Short greetings (under 20 chars)
    if len(q) < 20 and any(q.startswith(g) for g in ("hi", "hello", "hey", "thanks", "thank", "bye")):
        if any(p.search(question) for p in _GREETING_PATTERNS):
            return _build_greeting_response(registry, report_key=report_key)
        if any(p.search(question) for p in _THANKS_PATTERNS):
            return _build_thanks_response(registry)

    # Help/identity questions
    if any(p.search(question) for p in _HELP_PATTERNS):
        if not _has_erp_keyword(q):
            return _build_help_response(registry)

    # Thanks/goodbye
    if any(p.search(question) for p in _THANKS_PATTERNS):
        if not _has_erp_keyword(q):
            return _build_thanks_response(registry)

    # Clearly out-of-scope (weather, jokes, general knowledge, etc.)
    if any(p.search(question) for p in _OUT_OF_SCOPE_PATTERNS):
        if not _has_erp_keyword(q):
            return _build_out_of_scope_response(registry)

    return None


def is_out_of_scope(question: str) -> bool:
    """Return True if the question is clearly outside the chatbot's business scope."""
    return get_greeting_response(question) is not None


class ReportClassifier:
    """Multi-stage classifier: embeddings → LLM → keyword fallback.

    Architecture (production-grade, based on industry best practices):
      1. Embedding similarity (fast, <50ms) — pre-computed at startup
      2. LLM-based classification with structured output (~200ms) — when embedding confidence is low
      3. Keyword fallback (instant, 0ms) — when LLM is unavailable
      4. Follow-up context detection — short/pronoun questions reuse last report
    """

    def __init__(self):
        self.description_embeddings: Dict[str, List[float]] = {}
        self.use_embeddings = False

    async def load_embeddings(self, registry: Dict[str, ReportRegistryEntry]) -> None:
        """Load report descriptions and compute their embeddings in one batch."""
        if not registry:
            self.use_embeddings = False
            return

        keys = list(registry.keys())
        descriptions = [registry[k].description for k in keys]
        try:
            embeddings = await llm_gateway.get_embeddings(descriptions)
            if len(embeddings) != len(keys):
                raise llm_gateway.LLMGatewayError("Embedding batch returned wrong number of vectors")
            self.description_embeddings = dict(zip(keys, embeddings))
            self.use_embeddings = True
        except llm_gateway.LLMGatewayError:
            self.description_embeddings = {}
            self.use_embeddings = False

    def _build_llm_classify_prompt(
        self, question: str, registry: Dict[str, ReportRegistryEntry]
    ) -> str:
        """Build a few-shot classification prompt with report descriptions."""
        report_list = []
        for key, entry in registry.items():
            report_list.append(f"- {key}: {entry.description}")
        reports_text = "\n".join(report_list)

        return f"""You are an ERP report classifier. Given a user's question, determine which report it belongs to.

Available reports:
{reports_text}

Rules:
- Return ONLY the report_key as a JSON object: {{"report_key": "<key>"}}
- If the question is ambiguous or doesn't match any report, return {{"report_key": null}}
- "discount", "labour", "metal amount", "diamond amount", "colorstone", "gross weight", "net weight", "top customer", "best customer" → sales_report
- "wip", "work in progress", "jobs", "production", "job cost", "karigar", "department status", "pending jobs" → wip_report

Question: "{question}"

Respond with JSON only:"""

    async def _llm_classify(
        self, question: str, registry: Dict[str, ReportRegistryEntry]
    ) -> Optional[str]:
        """Use a cheap LLM call to classify the question. Returns report_key or None."""
        prompt = self._build_llm_classify_prompt(question, registry)
        messages = [
            {"role": "system", "content": "You are a precise classification engine. Output only valid JSON."},
            {"role": "user", "content": prompt},
        ]
        try:
            result = await llm_gateway.chat(
                tier="cheap",
                messages=messages,
                temperature=0.0,
                max_tokens=256,
            )
            # Parse JSON from response — model may wrap in markdown or add text
            text = result.text.strip()
            # Try direct parse first
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Try to extract JSON from text
                import re
                match = re.search(r'\{[^}]+\}', text)
                if match:
                    parsed = json.loads(match.group())
                else:
                    return None
            key = parsed.get("report_key")
            if key and key in registry:
                return key
            return None
        except Exception as exc:
            logger.debug("LLM report classification failed: %s", exc)
            return None

    async def classify(
        self,
        question: str,
        registry: Dict[str, ReportRegistryEntry],
        token_usage: Optional[List[Dict[str, int]]] = None,
        default_report_key: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[str]:
        """Return the report key best matching the question.

        Multi-stage:
        0. Deterministic intent mapping (rules before any LLM guesswork)
        1. Embedding similarity (fast)
        2. If embedding score is below threshold, try LLM classification
        3. If LLM fails, fall back to keyword matching
        4. If keywords fail, use default_report_key for follow-up context
        """
        # Stage 0: Deterministic intent mapping
        result = classify_question_by_intent(question, registry)
        if result:
            return result

        # Stage 1: Embedding similarity
        if self.use_embeddings and self.description_embeddings:
            try:
                emb_result = await llm_gateway.get_embedding(question)
                question_embedding = emb_result.embedding
                if token_usage is not None:
                    token_usage.append(emb_result.usage)
            except llm_gateway.LLMGatewayError:
                result = _keyword_classify(question, registry)
            else:
                best_key: Optional[str] = None
                best_score = settings.classifier_similarity_threshold

                for report_key, desc_embedding in self.description_embeddings.items():
                    if report_key not in registry:
                        continue
                    score = _cosine_similarity(question_embedding, desc_embedding)
                    if score > best_score:
                        best_score = score
                        best_key = report_key

                result = best_key

        # Stage 2: LLM classification (when embedding was uncertain or unavailable)
        if result is None:
            if history and default_report_key and _is_followup(question, history):
                result = default_report_key
            else:
                result = await self._llm_classify(question, registry)

        # Stage 3: Keyword fallback
        if result is None:
            result = _keyword_classify(question, registry)

        # Stage 4: Default for follow-up context
        if result is None and default_report_key and default_report_key in registry:
            if history and _is_followup(question, history):
                return default_report_key

        return result

    def list_available_reports(self, registry: Dict[str, ReportRegistryEntry]) -> List[Dict[str, str]]:
        """Return a list of available report keys and descriptions."""
        return [
            {"report_key": key, "pid": getattr(entry, "pid", None), "description": entry.description}
            for key, entry in registry.items()
        ]


def keyword_classify(
    question: str, registry: Dict[str, ReportRegistryEntry]
) -> Optional[str]:
    """Fallback keyword-based classifier (public wrapper)."""
    return _keyword_classify(question, registry)


def _keyword_classify(
    question: str, registry: Dict[str, ReportRegistryEntry]
) -> Optional[str]:
    """Fallback keyword-based classifier."""
    lowered = question.lower()
    best_key: Optional[str] = None
    best_score = 0.0
    generic_terms = get_generic_routing_terms()

    for report_key, keywords in _load_report_keywords().items():
        if report_key not in registry:
            continue
        score = 0.0
        for keyword in keywords:
            if re.search(r'\b' + re.escape(keyword) + r'\b', lowered):
                # Longer keyword matches are weighted slightly higher to avoid
                # generic words; generic entity nouns count even less so a
                # distinctive term ("quote") beats a bare "jobs".
                if keyword.lower() in generic_terms:
                    score += 0.5
                else:
                    score += len(keyword.split())
        if score > best_score:
            best_score = score
            best_key = report_key

    return best_key


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    a_vec = np.asarray(a, dtype=np.float32)
    b_vec = np.asarray(b, dtype=np.float32)
    norm = np.linalg.norm(a_vec) * np.linalg.norm(b_vec)
    if norm == 0:
        return 0.0
    return float(np.dot(a_vec, b_vec) / norm)


# Follow-up detection: short questions or questions with pronouns/references
_FOLLOWUP_MARKERS = [
    "it", "this", "that", "these", "those", "the same", "what about",
    "how about", "and", "also", "show me more", "filter", "narrow",
    "drill down", "breakdown", "by branch", "by category", "by brand",
    "for that", "from that", "in that", "of that",
]

_FOLLOWUP_MAX_WORDS = 12


def _is_followup(question: str, history: List[Dict[str, str]]) -> bool:
    """Heuristic: detect if a question is a follow-up to the previous conversation.

    Indicators:
    - Very short question (<= 3 words)
    - Contains pronouns/references (it, this, that, the same, what about)
    - Contains filter-narrowing language (by branch, breakdown, drill down)
    - No explicit report keyword and short (<= 12 words)
    """
    if not history:
        return False

    lowered = question.lower().strip()
    word_count = len(lowered.split())

    if word_count <= 3:
        return True

    for marker in _FOLLOWUP_MARKERS:
        if marker in lowered:
            return True

    has_report_keyword = False
    for keywords in _load_report_keywords().values():
        for kw in keywords:
            if kw in lowered:
                has_report_keyword = True
                break
        if has_report_keyword:
            break

    if not has_report_keyword and word_count <= _FOLLOWUP_MAX_WORDS:
        return True

    return False


# Backward-compatible module-level helpers
_classifier = ReportClassifier()


async def load_classifier_embeddings(registry: Dict[str, ReportRegistryEntry]) -> None:
    await _classifier.load_embeddings(registry)


async def classify_question(
    question: str,
    registry: Dict[str, ReportRegistryEntry],
    token_usage: Optional[List[Dict[str, int]]] = None,
    default_report_key: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> Optional[str]:
    return await _classifier.classify(
        question, registry, token_usage=token_usage,
        default_report_key=default_report_key, history=history,
    )


def list_available_reports(registry: Dict[str, ReportRegistryEntry]) -> List[Dict[str, str]]:
    return _classifier.list_available_reports(registry)
