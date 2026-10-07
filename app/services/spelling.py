"""Conservative misspelling normalisation for user questions.

Real users type fast and loosely — Indian-English spellings like "totel
selas" or "custmer wise amout" are common. The LLM parser handles mild
typos, but heavily mangled tokens drop its confidence and trigger the
clarify path. This module rewrites only tokens from an explicit
whitelist of known misspellings; it never fuzzy-matches, so real words
(customer names, design codes, SKUs) are never altered.
"""

import re
from typing import Dict

# token -> correction. Extend this list as new misspellings are observed
# in feedback logs (scripts/analyze_feedback.py).
_MISSPELLINGS: Dict[str, str] = {
    # report/domain words
    "totel": "total", "selas": "sales", "seles": "sales", "sel": "sale",
    "ordr": "order", "ordrs": "orders", "ordes": "orders",
    "wip": "wip", "invice": "invoice", "invoise": "invoice",
    "bil": "bill", "billls": "bills",
    "qutation": "quotation", "quotaton": "quotation", "quot": "quote",
    "etajobs": "eta jobs", "remaning": "remaining", "remainig": "remaining",
    "pendng": "pending", "pendingg": "pending", "pendin": "pending",
    "reguler": "regular", "reglar": "regular",
    "custmer": "customer", "custmor": "customer", "costmer": "customer",
    "custmrs": "customers", "custmers": "customers",
    "departmnt": "department", "departmet": "department", "dept": "department",
    "werk": "work", "werker": "worker", "workr": "worker",
    "disign": "design", "desing": "design", "dizain": "design",
    "catgory": "category", "categry": "category",
    "progres": "progress", "progess": "progress", "stuk": "stuck",
    "manufaturing": "manufacturing", "prodution": "production",
    "purcase": "purchase", "purhcase": "purchase",
    "delivary": "delivery", "delvery": "delivery",
    "rep": "rep", "employe": "employee",
    "busines": "business", "bussiness": "business",
    "comper": "compare", "comapr": "compare", "groth": "growth",
    # metrics
    "wieght": "weight", "weigth": "weight", "wight": "weight",
    "gros": "gross", "grs": "gross",
    "nett": "net",
    "amout": "amount", "amont": "amount", "amt": "amount",
    "dimond": "diamond", "diamnd": "diamond", "daimond": "diamond",
    "colourstone": "colourstone", "colorston": "colourstone",
    "quentity": "quantity", "quantiy": "quantity", "qty": "qty",
    "peices": "pieces", "piecs": "pieces", "peice": "piece",
    "cont": "count", "cout": "count",
    "valu": "value", "valeu": "value",
    "wastge": "wastage", "wastag": "wastage",
    "labr": "labour", "labur": "labour",
    "matel": "metal", "metl": "metal",
    "pur": "pure",
    "req": "req",
    # time words
    "todya": "today", "tody": "today", "tday": "today",
    "yestrday": "yesterday", "yestrady": "yesterday",
    "mnth": "month", "mont": "month", "mnt": "month", "montly": "monthly",
    "yer": "year", "yr": "year",
    "wek": "week", "weak": "week",
    "lastweek": "last week", "lastmonth": "last month", "lastyear": "last year",
    "thismonth": "this month", "thisyear": "this year", "thisweek": "this week",
    # common question words
    "wich": "which", "whch": "which", "wat": "what", "wht": "what",
    "higest": "highest", "highst": "highest", "lowst": "lowest",
    "bigest": "biggest", "mst": "most", "meny": "many", "meni": "many",
    "meny": "many", "hw": "how", "howm": "how",
    "colect": "collect", "colected": "collected", "colectd": "collected",
    "sumary": "summary", "summery": "summary", "summry": "summary",
    "sho": "show", "shw": "show", "tel": "tell",
    "perfomance": "performance", "perfrmance": "performance",
    "histry": "history", "detals": "details",
    "trendng": "trending", "populer": "popular",
}

_TOKEN = re.compile(r"[A-Za-z]+")


def normalize_spelling(question: str) -> str:
    """Replace known misspelled tokens; leave everything else untouched.

    Token-level and case-insensitive. Multi-word corrections (e.g.
    ``etajobs`` -> ``eta jobs``) are applied as plain string replacement
    of that token.
    """
    if not question:
        return question

    def _fix(match: "re.Match[str]") -> str:
        tok = match.group(0)
        return _MISSPELLINGS.get(tok.lower(), tok)

    return _TOKEN.sub(_fix, question)


# ── Hinglish (Roman Hindi) normalisation ────────────────────────────────────
# Real users mix Hindi grammar words with English domain terms: "pichle mahine
# ka total sales dikhao". The LLM parser understands this natively, but the
# deterministic layers (date extraction, ranking, field filters) are
# English-only. Rewriting common function words to English keeps those layers
# working and feeds the parser cleaner input.
#
# Two tiers, same whitelist philosophy as _MISSPELLINGS — exact-token matching
# only, never fuzzy, so entity names, design codes and SKUs pass through:
#   Tier 1 — unambiguous Hindi words that are never English or entity names.
#   Tier 2 — particles/short tokens (ka, se, ne...) that can legitimately be
#            English or entity codes (a sales rep "KA"). Applied only when the
#            question already contains a Tier-1 word or Devanagari script, so
#            "customer KA sales" in English is left untouched.
# "kal" is deliberately unmapped — it means both yesterday and tomorrow.

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# Multi-word phrases — applied first, longest match wins.
_HINGLISH_PHRASES: Dict[str, str] = {
    "ke hisaab se": "by", "ke hisab se": "by", "hisaab se": "by",
    "sabse zyada": "top", "sabse jyada": "top", "sabse jada": "top",
    "sabse kam": "lowest", "sabse accha": "best", "sabse achha": "best",
    "dikha do": "show", "bata do": "tell",
    "ke liye": "for",
}

# Tier 1 — always safe.
_HINGLISH_WORDS: Dict[str, str] = {
    # question words
    "kitna": "how much", "kitni": "how much", "kitne": "how many",
    "kaunsa": "which", "konsa": "which", "kaunsi": "which", "kaunse": "which",
    "kaun": "who", "kon": "who", "kisne": "who",
    "kiska": "whose", "kiski": "whose",
    "kya": "what", "kyu": "why", "kyun": "why", "kyon": "why",
    "kab": "when", "kahan": "where", "kaha": "where",
    # commands
    "dikhao": "show", "dikha": "show", "dikhavo": "show", "dikhana": "show",
    "batao": "tell", "bata": "tell", "batayen": "tell", "bataiye": "tell",
    "nikalo": "show", "nikal": "show",
    # ranking / comparison
    "sabse": "top", "zyada": "most", "jyada": "most", "jada": "most",
    "kam": "less",
    # time
    "aaj": "today", "abhi": "now",
    "pichla": "last", "pichle": "last", "pichli": "last",
    "agla": "next", "agli": "next", "agle": "next",
    "mahina": "month", "mahine": "month", "maheena": "month",
    "saal": "year", "sal": "year",
    "hafta": "week", "hafte": "week",
    # domain words users often swap to Hindi
    "kul": "total",
    "baki": "remaining", "baaki": "remaining",
    "bacha": "remaining", "bache": "remaining", "bachi": "remaining",
    "bikri": "sales", "bikree": "sales", "becha": "sold",
    "kharidi": "purchase", "khareed": "purchase",
    "udhaar": "outstanding", "udhar": "outstanding",
    "maal": "stock", "paisa": "amount", "paise": "amount",
    # logical / structural
    "aur": "and", "bhi": "also", "nahi": "not", "nahin": "not",
    "har": "each", "sab": "all", "saare": "all", "sare": "all",
    "phir": "again", "chahiye": "want",
}

# Tier 2 — guarded particles, applied only when Hinglish is already detected.
# They map onto English stop words that the deterministic filter extractor and
# date parser already trim on ("of", "in", "from", "to" are all in
# query_planner._FIELD_VALUE_STOP_WORDS).
_HINGLISH_PARTICLES: Dict[str, str] = {
    "ka": "of", "ki": "of", "ke": "of",
    "mein": "in", "se": "from", "tak": "to", "ko": "to",
    "par": "on", "ne": "by", "liye": "for", "ya": "or",
    "hai": "is", "hain": "are", "tha": "was", "thi": "was",
    "din": "day",
    "mera": "my", "mere": "my", "meri": "my",
    "mujhe": "me", "hamara": "our", "humara": "our", "apna": "our",
    "yeh": "this", "woh": "that", "isse": "this", "usse": "that",
    "karo": "do", "karna": "do", "wala": "", "wali": "", "wale": "",
}


def _has_hinglish_cue(question: str) -> bool:
    """True when the question contains Tier-1 Hinglish words or Devanagari."""
    if _DEVANAGARI_RE.search(question):
        return True
    lowered = question.lower()
    if any(phrase in lowered for phrase in _HINGLISH_PHRASES):
        return True
    return any(m.group(0).lower() in _HINGLISH_WORDS for m in _TOKEN.finditer(question))


def normalize_hinglish(question: str) -> str:
    """Rewrite romanised-Hindi function words to English equivalents.

    Phrases are replaced first (longest first), then Tier-1 tokens, then —
    only when a Hinglish cue exists — Tier-2 particles. Tokens absent from
    the maps are returned verbatim. Collapses the whitespace that empty
    replacements (``wala`` -> "") leave behind.
    """
    if not question:
        return question

    cue = _has_hinglish_cue(question)
    out = question
    for phrase, repl in sorted(_HINGLISH_PHRASES.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(phrase)}\b", repl, out, flags=re.IGNORECASE)

    out = _TOKEN.sub(lambda m: _HINGLISH_WORDS.get(m.group(0).lower(), m.group(0)), out)
    if cue:
        out = _TOKEN.sub(lambda m: _HINGLISH_PARTICLES.get(m.group(0).lower(), m.group(0)), out)

    return re.sub(r"\s{2,}", " ", out).strip()
