import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.services.column_registry import _load_registry
from app.services.intent import _detect_explicit_intent
from app.services.query_plan import QueryPlan
from app.services.semantic_query_parser import ParseResult, parse_query

logger = logging.getLogger(__name__)


@dataclass
class PlanningResult:
    parsed: ParseResult
    plan: QueryPlan
    intent_spec: Any
    validated_filters: Dict[str, Any]
    ai_where: str
    routing_source: str = "llm"


_FIELD_VALUE_STOP_WORDS = {
    "a", "an", "and", "amount", "average", "by", "category", "compared",
    "count", "customer", "date", "discount", "for", "generates", "growth",
    "highest", "how", "invoice", "items", "last", "lowest", "many", "month",
    "most", "pieces", "pcs", "per", "range", "revenue", "sale", "sales",
    "segment", "the", "this", "today", "top", "total", "type", "units",
    "value", "wastage", "week", "what", "which", "who", "wise", "year", "yesterday",
    # Function words / verbs that can appear between an entity value and the
    # metric anchor in rewritten questions ("design H_FR41 perform well in
    # sales") — the capture trims at the first of these.
    "in", "on", "at", "of", "is", "are", "was", "were", "do", "does", "did",
    "has", "have", "had", "it", "its", "not", "very", "really", "good", "bad",
    "better", "best", "worse", "worst", "perform", "performed", "performing",
    "well", "more", "less", "vs", "versus", "with", "without", "from",
}

# Column names that are time buckets, not attributes — '_apply_attribute_lookup'
# must never adopt these as the metric ("...in this month" is a date qualifier).
_TIME_DIMENSION_WORDS = {
    "month", "year", "week", "date", "entrydate", "jobdate", "day", "quarter",
}


def _apply_explicit_field_filters(parsed: ParseResult, question: str) -> None:
    """Extract explicit '<field> <value> <metric>' phrases into governed filters.

    This keeps values such as ``customer ThGems total sales`` and
    ``customer type Retailer total sales`` out of the ambiguous-entity path and
    sends them through the configured filter_key_map/SP filter contract instead.
    """
    report_cfg = _load_registry().get(parsed.report_key, {})
    filter_map = report_cfg.get("filter_key_map", {}) or {}
    candidates = []
    for alias, target in filter_map.items():
        if target in ("_date", "_name_filter"):
            continue
        phrase = alias.replace("_", " ").strip()
        if phrase:
            candidates.append((phrase, alias))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    metric_start = (
        r"(?:total\s+)?(?:sales?|revenue|amount|value|tax|discount|"
        r"pieces|pcs|units|weight|wt)\b"
    )
    occupied: List[tuple[int, int]] = []
    extracted = False
    for phrase, alias in candidates:
        match = re.search(
            rf"\b{re.escape(phrase)}\s+(?:of\s+|is\s+)?"
            rf"([A-Za-z0-9._&/-]+(?:\s+[A-Za-z0-9._&/-]+){{0,3}}?)\s+{metric_start}",
            question,
            re.IGNORECASE,
        )
        if not match:
            continue
        value = match.group(1).strip(" ,.?")
        # Trailing junk can be captured between the value and the metric anchor
        # ("design H_FR41 perform well in sales") — trim at the first stop word
        # so the real entity value survives instead of rejecting or polluting.
        tokens = value.split()
        cut = next(
            (i for i, tok in enumerate(tokens)
             if re.fullmatch(r"[A-Za-z0-9._&/-]+", tok) and tok.lower() in _FIELD_VALUE_STOP_WORDS),
            len(tokens),
        )
        value = " ".join(tokens[:cut]).strip(" ,.?")
        words = {word.lower() for word in re.findall(r"[A-Za-z0-9._&/-]+", value)}
        if not value or len(value) > 100 or words & _FIELD_VALUE_STOP_WORDS:
            continue
        if any(match.start(1) < end and match.end(1) > start for start, end in occupied):
            continue
        occupied.append(match.span(1))
        parsed.filters.setdefault(alias, value)
        extracted = True

    if extracted and not re.search(r"\b(?:by|wise|per|top|highest|lowest|most)\b", question, re.IGNORECASE):
        parsed.dimension = None
        parsed.intent = None


_WHY_RE = re.compile(r"\b(why|reasons?|because)\b", re.IGNORECASE)
_WHY_PERF_RE = re.compile(
    r"\b(perform\w*|well|best|top|selling|sold|success(?:ful)?|high(?:est)?|"
    r"increas\w*|growth|drop(?:ped)?|low(?:est)?|poor|worst|better|most|least)\b",
    re.IGNORECASE,
)

# ai_where column tokens -> entity kind, for "why" questions where the entity
# came from the LLM's ai_where rather than the regex filter extractor.
_WHY_ENTITY_SQL_TOKENS = {
    "design": ("designno", "skuno", "stockbarcode", "designcode"),
    "customer": ("customerfullname", "customeridentity", "customername"),
}


def _why_filtered_entity(parsed: ParseResult, extra_filters: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Which entity did the question filter on (design, customer, brand...)?

    ``extra_filters`` covers entity scope inherited from the previous turn —
    the context resolver rewrites 'this design' to 'design 794' but the
    previous-filters merge happens after the deterministic overrides run.
    """
    keys = {str(k).lower() for k in (parsed.filters or {})}
    keys |= {str(k).lower() for k in (extra_filters or {})}
    for e in ("design", "designno", "designcode", "sku", "barcode", "brand",
              "category", "customer", "sales rep", "branch", "metal", "job"):
        if e in keys or any(k.startswith(e) for k in keys):
            return "design" if e in ("designno", "designcode") else e
    ai = (parsed.ai_where or "").lower()
    for entity, tokens in _WHY_ENTITY_SQL_TOKENS.items():
        if any(t in ai for t in tokens):
            return entity
    return None


# ── Deterministic date extraction ────────────────────────────────────────────
# Every enumerable date form users type is extracted here — the LLM frequently
# drops them or guesses 'today'. Ordered most-specific → least-specific;
# first detector that fires wins. LLM presets (this/last month etc.) still
# handle the purely-relative forms, which they get right reliably.

_MONTH_WORDS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAME_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember|t)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

# Day/period relatives that would collide with named months ("compare this
# month vs august") — those stay with the LLM parser. "this/last/next year"
# is fine: it only picks which year the months land in.
_MONTH_COLLISION_RE = re.compile(
    r"\b(this|last|next)\s+(month|week)\b|\btoday\b|\byesterday\b|\btomorrow\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _month_end(y: int, m: int) -> date:
    return date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)


def _shift_month(y: int, m: int, delta: int) -> tuple:
    total = y * 12 + (m - 1) + delta
    return total // 12, total % 12 + 1


def _mkdate(y, m, d) -> Optional[date]:
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def _day_year(mo: int, d: int, today: date) -> int:
    """Most recent occurrence of a bare month-day."""
    return today.year if (mo, d) <= (today.month, today.day) else today.year - 1


def _parse_day_dates(q: str, today: date) -> List[date]:
    """Explicit calendar dates: '15 aug', '15th august 2026', 'aug 15',
    '15/08/2026', '2026-08-15'. Returns sorted unique dates."""
    found = set()
    for y, mo, d in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q):
        v = _mkdate(y, mo, d)
        if v:
            found.add(v)
    # d/m/yyyy or d.m.yyyy (Indian convention — day first)
    for d, mo, y in re.findall(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", q):
        v = _mkdate(y, mo, d)
        if v:
            found.add(v)
    # '15 aug', '15th august 2026'
    for d, mon, y in re.findall(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAME_RE})\b(?:\s*,?\s*(20\d{{2}}))?", q):
        mo = _MONTH_WORDS.get(mon)
        yr = int(y) if y else _day_year(mo, int(d), today)
        v = _mkdate(yr, mo, d)
        if v:
            found.add(v)
    # 'august 15', 'aug 15, 2026'
    for mon, d, y in re.findall(
        rf"\b({_MONTH_NAME_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(20\d{{2}}))?", q):
        mo = _MONTH_WORDS.get(mon)
        yr = int(y) if y else _day_year(mo, int(d), today)
        v = _mkdate(yr, mo, d)
        if v:
            found.add(v)
    return sorted(found)


def _set_month_dimension(parsed: ParseResult, span_count: int) -> None:
    """Multi-period spans get a month breakdown so each period shows as a row."""
    if parsed.dimension or span_count < 2:
        return
    columns = _load_registry().get(parsed.report_key, {}).get("columns", {})
    if "month" in columns:
        parsed.dimension = "month"
        parsed.limit = max(parsed.limit or 1, span_count)


def _apply_explicit_dates(parsed: ParseResult, question: str) -> None:
    """Extract every enumerable date form deterministically.

    Coverage (first match wins):
      explicit dates  '15 aug', 'aug 15 2026', '15/08/2026', '2026-08-15'
      relative N      'last 3 months', 'past 6 days', 'last 2 years'
      since           'since january', 'since aug 2025', 'since 2024'
      quarters        'q3', 'second quarter', 'this/last quarter' (calendar)
      financial year  'fy26', 'fy 2025-26', 'this financial year' (Apr–Mar)
      year(s)         'sales in 2025', 'compare 2024 and 2026'
      named months    'august', 'compare aug and sep', 'jan 2025 to mar 2026'
    """
    q = question.lower()
    today = date.today()

    def _finish(start: date, end: date) -> None:
        parsed.date_filter = {"start": start.isoformat(), "end": end.isoformat()}

    # ── 1. Explicit calendar dates (single date → that day; several → span) ──
    day_dates = _parse_day_dates(q, today)
    if day_dates:
        _finish(day_dates[0], day_dates[-1])
        return

    # ── 1b. Future/relative presets the LLM cannot be trusted with ──
    # 'tomorrow'/'next week'/'next month' resolve via resolve_preset_dates();
    # without this the LLM guessed dates (observed: 'tomorrow' -> 14 May 2025)
    # or silently fell back to today. Deliberately excludes this/last
    # month/year/week — those stay with the LLM and the period-detector so
    # multi-period phrasing ("this month vs august") still works.
    _RELATIVE_PRESET_RE = (
        (r"\bday\s+after\s+tomorrow\b|\bafter\s+tomorrow\b", "day_after_tomorrow"),
        (r"\btomorrow\b", "tomorrow"),
        (r"\bnext\s+month\b|\bagl[ae]\s+mahina\b|\bagle\s+mahine\b", "next_month"),
        (r"\bnext\s+week\b", "next_week"),
        # Hinglish date words — common in Indian ERP usage. 'kal' maps to
        # yesterday (dominant meaning in sales-review phrasing 'kal ka sale').
        (r"\baaj\b|\baj\b", "today"),
        (r"\bkal\b", "yesterday"),
        (r"\bis\s+mahin[ae]\b|\biss\s+mahin[ae]\b", "this_month"),
        (r"\b(?:pichl[ae]|pichhl[ae]|guzr[ae])\s+mahin[ae]\b", "last_month"),
        (r"\bis\s+saal\b|\biss\s+saal\b", "this_year"),
        (r"\b(?:pichl[ae]|pichhl[ae]|guzr[ae])\s+saal\b", "last_year"),
    )
    for _pat, _preset in _RELATIVE_PRESET_RE:
        if re.search(_pat, q):
            parsed.date_filter = {"preset": _preset}
            return
    if re.search(r"\bparso\w*\b|\bday\s+before\s+yesterday\b", q):
        _finish(today - timedelta(days=2), today - timedelta(days=2))
        return

    # ── 2. 'last/past/previous N days|weeks|months|years' ──
    m = re.search(r"\b(last|past|previous)\s+(\d{1,3})\s+(days?|weeks?|months?|years?)\b", q)
    if m:
        kind, n, unitw = m.group(1), int(m.group(2)), m.group(3)
        if unitw.startswith("day"):
            rng = (today - timedelta(days=n - 1), today)
        elif unitw.startswith("week"):
            rng = (today - timedelta(days=7 * n - 1), today)
        elif unitw.startswith("month"):
            if kind == "past":  # rolling, including the current partial month
                sy, sm = _shift_month(today.year, today.month, -(n - 1))
                rng = (date(sy, sm, 1), today)
            else:  # last/previous N *completed* months
                ey, em = _shift_month(today.year, today.month, -1)
                sy, sm = _shift_month(ey, em, -(n - 1))
                rng = (date(sy, sm, 1), _month_end(ey, em))
        else:  # years
            if kind == "past":
                rng = (date(today.year - n + 1, 1, 1), today)
            else:
                rng = (date(today.year - n, 1, 1), date(today.year - 1, 12, 31))
        _finish(*rng)
        return

    # ── 3. 'since X' → X's start through today ──
    ms = re.search(r"\bsince\s+(.+)$", q)
    if ms:
        tail = ms.group(1)
        dts = _parse_day_dates(tail, today)
        if dts:
            _finish(dts[0], today)
            return
        ym = re.search(rf"\b({_MONTH_NAME_RE})\b(?:\s*,?\s*(20\d{{2}}))?", tail)
        if ym:
            mo = _MONTH_WORDS[ym.group(1)]
            yr = int(ym.group(2)) if ym.group(2) else (
                today.year if mo <= today.month else today.year - 1)
            _finish(date(yr, mo, 1), today)
            return
        yy = _YEAR_RE.search(tail)
        if yy:
            _finish(date(int(yy.group(1)), 1, 1), today)
            return
        if re.search(r"\blast\s+year\b", tail):
            _finish(date(today.year - 1, 1, 1), today)
            return

    # ── 4. Quarters (calendar: Q1 Jan–Mar … Q4 Oct–Dec) ──
    qm_rel = re.search(r"\b(this|last)\s+quarter\b", q)
    q_names = {"first": 1, "second": 2, "third": 3, "fourth": 4,
               "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
    q_nums = [int(g) for g in re.findall(r"\bq([1-4])\b", q)]
    q_nums += [q_names[w] for w in re.findall(
        r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b", q)]
    if qm_rel or q_nums:
        today_q = (today.month - 1) // 3 + 1
        if qm_rel and not q_nums:
            if qm_rel.group(1) == "this":
                qn, yr = today_q, today.year
                rng = (date(yr, 3 * qn - 2, 1), today)
            else:
                qn, yr = (4, today.year - 1) if today_q == 1 else (today_q - 1, today.year)
                rng = (date(yr, 3 * qn - 2, 1), _month_end(yr, 3 * qn))
        else:
            ym = _YEAR_RE.search(q)
            spans = []
            for qn in set(q_nums):
                if ym:
                    yr = int(ym.group(1))
                else:
                    # most recent *completed* occurrence of that quarter
                    yr = today.year if _month_end(today.year, 3 * qn) <= today else today.year - 1
                spans.append((date(yr, 3 * qn - 2, 1), _month_end(yr, 3 * qn)))
            rng = (min(s for s, _ in spans), max(e for _, e in spans))
        _finish(*rng)
        if len(set(q_nums)) > 1 or re.search(r"\b(compare|vs|versus)\b", q):
            _set_month_dimension(parsed, 3)
        return

    # ── 5. Financial year (Apr–Mar; 'fy26'/'fy 2025-26' → Apr 2025–Mar 2026) ──
    fy = re.search(r"\b(?:fy|fiscal\s+year|financial\s+year)\s*'?\s*(\d{2,4})?\b", q)
    if fy:
        n = fy.group(1)
        if n and len(n) == 4:
            sy = int(n)                       # fy 2025 / fy 2025-26 → starts Apr 2025
        elif n:
            sy = 2000 + int(n) - 1            # fy26 → Apr 2025–Mar 2026
        elif "last" in q or "previous" in q:
            sy = (today.year - 1) if today.month >= 4 else (today.year - 2)
        else:                                 # this/current/bare → current FY
            sy = today.year if today.month >= 4 else today.year - 1
        _finish(date(sy, 4, 1), min(today, date(sy + 1, 3, 31)))
        return

    # ── 6. Named months (pair each with its nearest explicit year) ──
    if not _MONTH_COLLISION_RE.search(q):
        year_positions = [(m.start(), int(m.group(1))) for m in _YEAR_RE.finditer(q)]
        occurrences = []
        for m in re.finditer(rf"\b({_MONTH_NAME_RE})\b", q):
            mo = _MONTH_WORDS[m.group(1)]
            if year_positions:
                yr = min(year_positions, key=lambda p: abs(p[0] - m.start()))[1]
            elif "last year" in q or "previous year" in q:
                yr = today.year - 1
            elif "this year" in q:
                yr = today.year
            elif "next year" in q:
                yr = today.year + 1
            else:
                yr = today.year if mo <= today.month else today.year - 1
            occurrences.append((yr, mo))
        if occurrences:
            occ = sorted(set(occurrences))
            (y0, m0), (y1, m1) = occ[0], occ[-1]
            _finish(date(y0, m0, 1), _month_end(y1, m1))
            _set_month_dimension(parsed, len(occ))
            return

    # ── 7. Bare years (only when they can't be confused with entity IDs) ──
    years = [int(y) for y in _YEAR_RE.findall(q)]
    if len(years) >= 2 and re.search(r"\b(compare|vs|versus|between|and|to)\b", q):
        columns = _load_registry().get(parsed.report_key, {}).get("columns", {})
        _finish(date(min(years), 1, 1), date(max(years), 12, 31))
        if "year" in columns and not parsed.dimension:
            parsed.dimension = "year"
            parsed.limit = max(parsed.limit or 1, len(set(years)))
        return
    single_year = re.search(
        r"\b(?:in|for|of|during)\s+(20\d{2})\b|\b(20\d{2})\s+(?:sales|data|revenue|performance|orders)\b", q)
    if single_year:
        y = int(single_year.group(1) or single_year.group(2))
        _finish(date(y, 1, 1), min(today, date(y, 12, 31)))
        return


def _apply_metric_override(parsed: ParseResult, question: str, locked_metric: Optional[str] = None) -> None:
    """Question names a non-default metric by its catalog alias
    ('gold weight', 'discount', 'making charges') but the parse left the
    default metric — switch deterministically. Config-driven via
    metric_catalog aliases, so it works for every report."""
    report_cfg = _load_registry().get(parsed.report_key, {})
    default_metric = report_cfg.get("default_metric", "Amount")
    catalog = report_cfg.get("metric_catalog", {}) or {}
    q = question.lower()
    best_key, best_len = None, 0
    for mkey, meta in catalog.items():
        for alias in meta.get("aliases") or []:
            a = str(alias).strip().lower()
            if len(a) < 2:  # \b-anchored — 2-char abbreviations (gw, nw) are safe
                continue
            if re.search(rf"\b{re.escape(a)}\b", q):
                if mkey != parsed.metric and len(a) > best_len:
                    best_key, best_len = mkey, len(a)
    if best_key:
        # Always switch off the default metric. For a non-default pick, switch
        # only when the model's choice isn't grounded in the question — e.g.
        # 'total wastage' where the model chose WastageAmount but no 'wastage
        # amount' alias appears in the text while 'wastage' matches Wastage.
        # A metric set by a deterministic intent spec is never overridden.
        if locked_metric and parsed.metric == locked_metric:
            return
        switch = parsed.metric == default_metric
        if not switch:
            meta = catalog.get(parsed.metric, {}) or {}
            aliases = [str(a).strip().lower() for a in meta.get("aliases") or []]
            aliases.append(parsed.metric.lower())
            switch = not any(
                len(a) >= 2 and re.search(rf"\b{re.escape(a)}\b", q)
                for a in aliases
            )
        if switch:
            parsed.metric = best_key
            parsed.intent = None


def _apply_attribute_lookup(parsed: ParseResult, question: str) -> None:
    """'tell me its type', 'customer code X tell its type', 'brand of design X'
    — the user wants an attribute VALUE, not a metric. Map the attribute word
    to the matching string column and let validation force a MAX() lookup.
    Only fires when the parse still holds the report's default metric (i.e.
    the attribute word was ignored), never overrides a real metric choice."""
    report_cfg = _load_registry().get(parsed.report_key, {})
    if parsed.metric != report_cfg.get("default_metric"):
        return
    q = question.lower()
    attr = None
    m = re.search(r"\b(?:its|their|his|her)\s+([a-z][a-z\s]{0,30}?)\s*(?:\?|$)", q)
    if m:
        attr = m.group(1).strip()
    if not attr:
        m2 = re.search(r"\b([a-z][a-z\s]{0,30}?)\s+of\s+(?:this|that|the|same|above)\b", q)
        if m2:
            attr = m2.group(1).strip()
    columns = report_cfg.get("columns", {}) or {}
    q_words = set(re.findall(r"[a-z]+", q))

    def _col_aliases(cname: str, meta: dict) -> set:
        aliases = {cname.lower()}
        aliases |= {str(a).strip().lower() for a in (meta.get("filter") or {}).get("aliases", [])}
        aliases |= {str(a).strip().lower() for a in meta.get("aliases", [])}
        return aliases

    if not attr:
        # Rewritten phrasing: 'customer code vidsy customer type' — the
        # question ends with the attribute's own alias. Date-grouping columns
        # are excluded: a trailing 'this month'/'in 2025' is a time qualifier,
        # not an attribute lookup ('top 5 design ... in this month' would
        # otherwise corrupt metric=Amount -> metric='month').
        q_clean = q.rstrip(" ?.!")
        for cname, meta in columns.items():
            if str(meta.get("type", "")).lower() not in ("string", "text"):
                continue
            if str(meta.get("grp", "")).lower() == "date" or cname.lower() in _TIME_DIMENSION_WORDS:
                continue
            if any(len(a) > 3 and q_clean.endswith(a) for a in _col_aliases(cname, meta)):
                parsed.metric = cname
                parsed.aggregation = "max"
                parsed.dimension = None
                parsed.intent = None
                return
        return
    best, best_score = None, 0
    for cname, meta in columns.items():
        if str(meta.get("type", "")).lower() not in ("string", "text"):
            continue
        for alias in _col_aliases(cname, meta):
            words = alias.split()
            if attr == alias:
                score = 3
            elif len(words) > 1 and attr == words[-1]:
                score = 1 + len(q_words & set(words[:-1]))  # 'its type' + 'customer' in q → customer type
            else:
                continue
            if score > best_score:
                best, best_score = cname, score
    if best:
        parsed.metric = best
        parsed.aggregation = "max"
        parsed.dimension = None
        parsed.intent = None


def _apply_metric_companions(parsed: ParseResult, question: str) -> None:
    """Count-style metrics pair with the report's value metric — 'today total
    orders' means 'how many orders AND how much', not just a bare count.
    Config-driven: a metric_catalog entry may declare ``companions`` (e.g.
    total_count -> Amount / JobCost / TotalTax). Skipped for dimension
    breakdowns (companions are per-total, not per-row) and explicit 'only'."""
    if parsed.dimension:
        return
    if re.search(r"\bonly\b", question, re.IGNORECASE):
        return
    catalog = _load_registry().get(parsed.report_key, {}).get("metric_catalog", {}) or {}
    companions = (catalog.get(parsed.metric) or {}).get("companions") or []
    for comp in companions:
        if comp in catalog and comp != parsed.metric and comp not in (parsed.extra_metrics or []):
            parsed.extra_metrics = list(parsed.extra_metrics or []) + [comp]


_DIMENSION_WORD_RE = re.compile(
    r"\b([a-z]+)\s*wise\b|\bby\s+([a-z]+)\b|\bper\s+([a-z]+)\b|\beach\s+([a-z]+)\b",
    re.IGNORECASE,
)


def _apply_dimension_words(parsed: ParseResult, question: str) -> None:
    """'category wise', 'by branch', 'per salesperson', 'each design' —
    deterministic dimension via the report's dimension_aliases. Only fires
    when the parse left dimension empty."""
    if parsed.dimension:
        return
    from app.services.column_registry import get_dimension_aliases
    aliases = get_dimension_aliases(parsed.report_key)
    for m in _DIMENSION_WORD_RE.finditer(question.lower()):
        word = next(g for g in m.groups() if g)
        dim = aliases.get(word) or aliases.get(word.rstrip("s"))
        if dim:
            parsed.dimension = dim
            return


_DATE_PREDICATE_RE = re.compile(
    r"\b(entrydate|jobdate|month|year|week)\b|'(?:20\d{2}|\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)'",
    re.IGNORECASE,
)


def _strip_date_predicates(parsed: ParseResult) -> None:
    """Drop ai_where conjuncts that restate the deterministic date range.

    The LLM often writes 'month=''August''' / 'year=''2026''' alongside the
    explicit range — redundant AND wrong (the computed month column holds
    '2026-08', not 'August'), and it errors out SP-side (stat_code=1001).
    """
    ai = parsed.ai_where
    if not ai or not parsed.date_filter:
        return
    # Split on top-level AND only — a naive regex split breaks inside quoted
    # literals ("LIKE '%A AND B%'") and nested parens, mangling name filters.
    from app.services.parse_result import _split_top_level_and
    kept = [c for c in _split_top_level_and(ai)
            if not _DATE_PREDICATE_RE.search(c)]
    parsed.ai_where = " AND ".join(kept) or None


_RANKING_WORD_RE = re.compile(
    r"\b(top|bottom|best|worst|first|leading|highest|lowest|least|most)\s+(\d{1,3})\b"
    r"(?!\s*(?:days?|weeks?|months?|years?|times?\b))"
)
_ASC_RANK_WORDS = {"bottom", "worst", "lowest", "least"}
_DESC_RANK_WORDS = {"top", "best", "highest", "most", "leading", "first"}


def _apply_ranking_overrides(parsed: ParseResult, question: str) -> None:
    """'top N'/'bottom N' are enumerable — take N and direction from the
    question, not the LLM's guess. Also catches the entity after N
    ('top 5 customers' → customer dimension) when the LLM missed it."""
    q = question.lower()
    m = _RANKING_WORD_RE.search(q)
    if m:
        word, n = m.group(1), int(m.group(2))
        if not parsed.dimension:
            ent = re.match(r"\s*([a-z]+)", q[m.end():])
            if ent:
                from app.services.column_registry import get_dimension_aliases
                aliases = get_dimension_aliases(parsed.report_key)
                parsed.dimension = aliases.get(ent.group(1)) or aliases.get(ent.group(1).rstrip("s"))
        if parsed.dimension:
            parsed.limit = n
            if word in _ASC_RANK_WORDS:
                parsed.sort = "asc"
            elif word in _DESC_RANK_WORDS:
                parsed.sort = "desc"
    elif parsed.dimension:
        if re.search(r"\b(bottom|worst|lowest|least|poorest|weakest|ascending)\b", q):
            parsed.sort = "asc"
        elif re.search(r"\b(descending|highest|topmost)\b", q):
            parsed.sort = "desc"


def _apply_why_breakdown(
    parsed: ParseResult,
    question: str,
    previous_filters: Optional[Dict[str, Any]] = None,
) -> None:
    """Turn 'why did <entity> perform well' into a governed driver breakdown.

    The governed pipeline cannot answer causal questions, but it CAN show what
    drives the number: a design's sales -> top customers of that design; a
    customer's spend -> top designs they bought. This converts the follow-up
    into a metric+dimension ranking (structurally complete -> skips the
    low-confidence clarify) instead of returning a dead-end clarify.
    """
    if not parsed.metric:
        return
    if not (_WHY_RE.search(question) and _WHY_PERF_RE.search(question)):
        return
    entity = _why_filtered_entity(parsed, previous_filters)
    if not entity:
        return
    from app.services.column_registry import get_dimension_aliases
    aliases = get_dimension_aliases(parsed.report_key)
    if parsed.dimension:
        # A dimension equal to the filtered entity's own dimension is just
        # the entity restated ('reasons for design 794' -> dim=designno) —
        # replace it with the driver dimension. Any other dimension is real
        # user intent ('why sales dropped month wise') — leave it alone.
        cur = aliases.get(parsed.dimension.strip().lower(), parsed.dimension.strip())
        entity_dim = aliases.get(entity) or aliases.get(entity + " identity")
        if not entity_dim or cur.lower() != entity_dim.lower():
            return
    # Explain with the *other* side of the sale: customer-filtered -> what they
    # bought (design); anything else -> who bought it (customer).
    pref = "design" if entity in ("customer", "client", "customer name") else "customer"
    dim = aliases.get(pref) or aliases.get("customer") or aliases.get("design")
    if not dim:
        return
    parsed.dimension = dim
    parsed.aggregation = "sum"
    parsed.sort = "desc"
    parsed.limit = parsed.limit if parsed.limit and parsed.limit > 1 else 10


def _apply_deterministic_override(
    parsed: ParseResult,
    question: str,
    previous_filters: Optional[Dict[str, Any]] = None,
) -> None:
    det_spec = _detect_explicit_intent(question, parsed.report_key)
    report_cfg = _load_registry().get(parsed.report_key, {})
    default_metric = report_cfg.get("default_metric", "Amount")
    if det_spec is not None:
        parsed.confidence = max(parsed.confidence, 0.95)
    if det_spec is not None and det_spec.metric_key and (parsed.metric == default_metric or det_spec.override_metric):
        parsed.metric = det_spec.metric_key
        parsed.aggregation = det_spec.aggregation or parsed.aggregation
        if det_spec.dimension:
            parsed.dimension = det_spec.dimension
            parsed.sort = det_spec.sort or parsed.sort
            parsed.limit = det_spec.limit or parsed.limit
    if det_spec is not None and parsed.metric == det_spec.metric_key:
        parsed.intent = det_spec.intent
    _apply_explicit_field_filters(parsed, question)
    _apply_metric_override(
        parsed, question,
        locked_metric=det_spec.metric_key if det_spec else None,
    )
    _apply_attribute_lookup(parsed, question)
    _apply_metric_companions(parsed, question)
    _apply_dimension_words(parsed, question)
    _apply_explicit_dates(parsed, question)
    _strip_date_predicates(parsed)
    _apply_ranking_overrides(parsed, question)
    _apply_why_breakdown(parsed, question, previous_filters)
    # Count words are enumerable — 'how many' is never a sum of Amount.
    # Skip when an intent rule already set the aggregation (e.g. units_sold
    # is a conditional SUM, not a row count). 'How many <unit>' ('how many
    # grams of gold') is a weight question — never a row count.
    from app.services.metric_validator import _COUNT_OF_UNIT_RE
    if re.search(r"\b(how many|number of|count of)\b", question, re.IGNORECASE) \
            and parsed.aggregation == "sum" \
            and not _COUNT_OF_UNIT_RE.search(question) \
            and not (det_spec and det_spec.aggregation):
        parsed.aggregation = "count"
    if re.search(r"\b(unique|distinct|different)\b", question, re.IGNORECASE) \
            and parsed.aggregation == "count":
        parsed.aggregation = "count_distinct"
    elif re.search(r"\b(avg|average|mean)\b", question, re.IGNORECASE) \
            and parsed.aggregation == "sum":
        parsed.aggregation = "avg"
    # Scalar min/max: 'highest bill value' with no dimension is an aggregate,
    # not a ranking (ranking paths already have a dimension by now).
    if not parsed.dimension:
        if re.search(r"\b(highest|maximum|max|peak)\b", question, re.IGNORECASE):
            parsed.aggregation = "max"
        elif re.search(r"\b(lowest|minimum|min)\b", question, re.IGNORECASE):
            parsed.aggregation = "min"
    # 'list all / show all X' wants the full table, not the LLM's default N.
    if parsed.dimension and re.search(
            r"\b(list|show|give(?:\s+me)?)\s+(?:all|every)\b", question, re.IGNORECASE):
        parsed.limit = max(parsed.limit or 1, 50)


def _coerce_pinned_metric(plan: "QueryPlan") -> None:
    """A frontend-pinned report must not hard-fail on an incompatible metric.

    When the UI pins sales_report but the question semantics picked another
    report's metric ('sales order jobs' -> QuotationJob), degrade to the
    pinned report's count metric (count questions) or default metric instead
    of 'Unknown metric' — the pin reflects where the user is looking.
    """
    cfg = _load_registry().get(plan.report_key, {})
    cols = cfg.get("columns", {})
    valid = set(cols) | set(cfg.get("metric_catalog", {})) | set(cfg.get("special_metrics", {}))
    if plan.metric in valid:
        return
    from app.services.column_registry import resolve_metric_alias
    if resolve_metric_alias(plan.report_key, plan.metric):
        return
    catalog = cfg.get("metric_catalog", {}) or {}
    if plan.aggregation in ("count", "count_distinct"):
        count_key = next(
            (k for k, m in catalog.items()
             if str((m or {}).get("type", "")).lower() == "count"),
            "total_count" if "total_count" in valid else "",
        )
        if count_key:
            plan.metric = count_key
            return
    plan.metric = cfg.get("default_metric", "Amount")


def finalize_query(
    parsed: ParseResult,
    question: str,
    previous_filters: Optional[Dict[str, Any]] = None,
    routing_source: str = "llm",
) -> PlanningResult:
    # Routing-source confidence represents how sure we are of the report
    # route. For non-LLM routes it is the only signal, so it wins; for
    # LLM-routed parses the model's self-reported confidence is the signal —
    # overwriting it with a flat 0.60 made every scalar query fail the 0.75
    # clarify gate ('total wastage', 'tomorrow sale' -> "not confident").
    if routing_source == "llm":
        parsed.confidence = parsed.confidence or 0.60
    else:
        parsed.confidence = {
            "frontend": 1.0, "intent": 0.95, "keyword": 0.75, "context": 0.80,
        }.get(routing_source, 0.60)
    _apply_deterministic_override(parsed, question, previous_filters)
    from app.services.parse_result import retarget_misplaced_filters
    retarget_misplaced_filters(question, parsed)
    lowered = question.lower()
    if not parsed.date_filter and any(term in lowered for term in ("growth", "compared with the previous", "compare with the previous")):
        parsed.date_filter = {"preset": "this_month"}
    if parsed.dimension and parsed.aggregation in ("min", "max"):
        # String-typed metrics (designno, CustomerName...) can't be summed —
        # MAX() is the only valid aggregate for text, keep it.
        from app.services.column_registry import _REGISTRY as _COL_REG
        col_meta = _COL_REG.get(parsed.report_key, {}).get("columns", {}).get(parsed.metric, {})
        if str(col_meta.get("type", "")).lower() not in ("string", "text"):
            parsed.aggregation = "sum"
    plan = parsed.to_query_plan(question)
    if routing_source == "frontend":
        _coerce_pinned_metric(plan)
    plan.validate_against_registry()
    parsed.metric = plan.metric
    parsed.dimension = plan.dimension
    parsed.limit = plan.limit
    parsed.aggregation = plan.aggregation
    parsed.sort = plan.sort_by or "desc"
    validated_filters = plan.validated_filters()
    if previous_filters:
        # Follow-up questions inherit the previous turn's filters — entity
        # filters too ('above design', 'same for last month'). New dates in
        # the question always win over inherited ones.
        for key, val in previous_filters.items():
            if not val or key in validated_filters:
                continue
            if key in ("start_date", "end_date") and plan.date_range:
                continue
            validated_filters[key] = val
    return PlanningResult(
        parsed=parsed,
        plan=plan,
        intent_spec=parsed.to_intent_spec(),
        validated_filters=validated_filters,
        ai_where=parsed.generate_where_clause(),
        routing_source=routing_source,
    )


def _report_for_metric_shape(parsed: ParseResult, registry: Optional[Dict[str, Any]], question: str = "") -> str:
    """Pick the report owning the parsed metric/dimension when the LLM left
    report_key empty. Scores metric ownership (2) + dimension ownership (1);
    ties fall through to keyword hits on the question, then registry order."""
    from app.services.column_registry import (
        get_dimension_aliases, get_report_keywords, resolve_metric_alias,
    )
    reg = _load_registry()
    keys = [k for k in (registry or reg) if k in reg]
    scored = []
    for rk in keys:
        cfg = reg.get(rk, {})
        cols = cfg.get("columns", {})
        valid = set(cols) | set(cfg.get("metric_catalog", {})) | set(cfg.get("special_metrics", {}))
        score = 0
        m = (parsed.metric or "")
        if m and (m in valid or resolve_metric_alias(rk, m)):
            score += 2
        dim = (parsed.dimension or "").strip().lower()
        if dim and (dim in {c.lower() for c in cols}
                    or dim in get_dimension_aliases(rk)):
            score += 1
        scored.append((score, rk))
    top = max((s for s, _ in scored), default=0)
    if top <= 0:
        return ""
    tied = [rk for s, rk in scored if s == top]
    if len(tied) == 1:
        return tied[0]
    # Tie-break on keyword hits in the question, then registry order.
    ql = question.lower()
    best_kw, best_rk = 0, tied[0]
    for rk in tied:
        kw = sum(1 for k in get_report_keywords(rk) if k and k.lower() in ql)
        if kw > best_kw:
            best_kw, best_rk = kw, rk
    return best_rk


async def plan_query(
    question: str,
    history: Optional[List[Dict[str, str]]] = None,
    token_usage: Optional[List[Dict[str, int]]] = None,
    report_name: str = "",
    previous_filters: Optional[Dict[str, Any]] = None,
    routing_source: str = "llm",
    registry: Optional[Dict[str, Any]] = None,
    fallback_report: str = "",
) -> PlanningResult:
    selected_report = report_name
    if selected_report:
        routing_source = "frontend"
    elif registry and len(registry) > 1:
        from app.services.intent import classify_question_by_intent
        from app.services.classifier import keyword_classify
        selected_report = classify_question_by_intent(question, registry) or ""
        if selected_report:
            routing_source = "intent"
        else:
            selected_report = keyword_classify(question, registry) or ""
            if selected_report:
                routing_source = "keyword"
    # Keyword routing is a weak hint — single-word ties are common
    # ('customer order total' hits both order and sales keywords). Parse
    # against the FULL catalog so the LLM can pick a better report; only the
    # high-precision routes (frontend pin, deterministic intent rules) lock
    # the catalog to a single report.
    parse_report = selected_report if routing_source in ("frontend", "intent") else ""
    parsed = await parse_query(question, history=history, token_usage=token_usage, report_name=parse_report)
    report_aliases = {"sales_summary": "sales_report", "wip_summary": "wip_report"}
    parsed.report_key = report_aliases.get(parsed.report_key, parsed.report_key)
    if selected_report and routing_source == "keyword":
        # The keyword pick never saw the question's semantics — a valid
        # full-catalog choice from the LLM wins; an empty/hallucinated one
        # falls back to the keyword route.
        valid_keys = set(registry) if registry else set(_load_registry())
        if parsed.report_key and parsed.report_key in valid_keys:
            routing_source = "llm"
        else:
            parsed.report_key = selected_report
    elif selected_report:
        if parsed.report_key and parsed.report_key != selected_report:
            parsed.alternatives = [{"intent": parsed.report_key, "confidence": 0.25}]
        parsed.report_key = selected_report
    elif not (parsed.report_key or "").strip():
        # LLM left the report blank — recover the report that owns the parsed
        # metric/dimension rather than erroring on a well-formed parse.
        resolved = fallback_report or _report_for_metric_shape(parsed, registry, question)
        if resolved:
            parsed.report_key = resolved
            routing_source = "context"
    return finalize_query(parsed, question, previous_filters, routing_source)
