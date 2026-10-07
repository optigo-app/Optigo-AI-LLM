"""Report master-data (vocabulary) service.

One bootstrap call per report returns the real filter vocabulary — live values
for dimension columns (department, priority, bill mode, ...) plus the static
canonical literals declared in report_columns/*.json. Frontends call this once
on report load instead of assuming enum values.

Live values come from the governed path only: a grouped COUNT query through
the shared LLM-chat SP (same call_real_report_api used by /chat), so master
data respects the same tables + base_filter + tenant DB resolution. Results
are cached per (company, report) for a short TTL — masters change rarely.

The same cache doubles as a runtime vocabulary for filter normalization:
match_master_value() lets QueryPlan map user words like "rnd" onto the
tenant's actual stored values instead of relying on static guesses.
"""

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.column_registry import _REGISTRY, get_canonical_values

logger = logging.getLogger(__name__)

MASTER_VALUE_CAP = 200          # max distinct values fetched per master column
MASTER_CACHE_TTL = 3600.0       # seconds; master vocab changes rarely

# (company_code, report_key) -> (expiry_epoch, {column: [values]})
# AND the full endpoint payload, so repeat /masters calls never hit the SP.
_MASTER_CACHE: Dict[tuple, tuple] = {}      # vocab  : key -> (expiry, {col: [values]})
_PAYLOAD_CACHE: Dict[tuple, tuple] = {}     # payload: key -> (expiry, response)

# Columns where a live distinct-list would be high-cardinality, sensitive, or
# meaningless to enumerate (identifiers, date buckets, multi-value concats).
_MASTER_BLOCKLIST = {
    "stockdocumentno", "stockbarcode", "skuno", "invn", "designno", "designcode",
    "customername", "customerfullname", "customeridentity", "customercode",
    "job_customerfirmname", "job_customercode", "loginusercode", "po",
    "serialjobno", "quotationno", "quotation_skuno", "jobno", "job_jobno",
    "jobno_excel", "designsetnolist", "jobbulkno", "groupjob", "productid",
    "lineid", "autocode", "forencodedesignsids", "cpo", "batch_no", "batchnumber",
    "srorderno", "srversionname", "mfgdesign", "stockdocument", "invoiceno",
    "month", "year", "week", "diacolor", "diaquality",
}


def get_master_columns(report_key: str) -> List[str]:
    """Columns declared as masters for a report (report JSON `masters` list).

    Auto-fallback when the list is absent: string-typed filterable columns,
    minus high-cardinality blocklist.
    """
    cfg = _REGISTRY.get(report_key, {}) or {}
    declared = cfg.get("masters")
    if isinstance(declared, list) and declared:
        return [c for c in declared if c in (cfg.get("columns") or {})]
    cols = cfg.get("columns") or {}
    out = []
    for name, meta in cols.items():
        if not isinstance(meta, dict):
            continue
        if name.lower() in _MASTER_BLOCKLIST or (meta.get("sql") or "").lower() in _MASTER_BLOCKLIST:
            continue
        if meta.get("type") != "string":
            continue
        if isinstance(meta.get("filter"), dict) or name in (cfg.get("canonical_values") or {}):
            out.append(name)
    return out


def _cache_key(company_code: str, report_key: str) -> tuple:
    return ((company_code or "").upper(), report_key)


def get_cached_masters(report_key: str, company_code: str = "") -> Optional[Dict[str, List[str]]]:
    hit = _MASTER_CACHE.get(_cache_key(company_code, report_key))
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _store_masters(company_code: str, report_key: str, values: Dict[str, List[str]], payload: Optional[Dict[str, Any]] = None) -> None:
    _MASTER_CACHE[_cache_key(company_code, report_key)] = (time.time() + MASTER_CACHE_TTL, values)
    if payload is not None:
        _PAYLOAD_CACHE[_cache_key(company_code, report_key)] = (time.time() + MASTER_CACHE_TTL, payload)


def get_cached_master_payload(report_key: str, company_code: str = "") -> Optional[Dict[str, Any]]:
    """Full cached /masters response — prevents repeat SP fan-out on reload."""
    hit = _PAYLOAD_CACHE.get(_cache_key(company_code, report_key))
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def merge_master_values(report_key: str, company_code: str, column: str, live_values: List[str]) -> None:
    """Merge live-fetched values into the runtime vocab cache without a full
    masters refresh — used when a grouped query already produced the list."""
    key = _cache_key(company_code, report_key)
    _, existing = _MASTER_CACHE.get(key, (0.0, {}))
    merged = dict(existing)
    have = {v.lower() for v in existing.get(column, [])}
    merged[column] = existing.get(column, []) + [v for v in live_values if v and v.lower() not in have]
    _MASTER_CACHE[key] = (time.time() + MASTER_CACHE_TTL, merged)
    # Keep the endpoint payload in sync so a later /masters call serves it
    p_hit = _PAYLOAD_CACHE.get(key)
    if p_hit and p_hit[0] > time.time():
        entry = p_hit[1].get("masters", {}).get(column)
        if isinstance(entry, dict):
            entry["values"] = merged[column]
            entry["count"] = len(entry["values"])
            if entry.get("source") == "error":
                entry["source"] = "live"


async def _fetch_column_values(report_key: str, column: str, **scope) -> List[str]:
    """Grouped-count through the governed SP — returns distinct dimension values."""
    from app.services.real_api_client import call_real_report_api

    spec = SimpleNamespace(
        metric_key="total_count",
        aggregation="count",
        dimension=column,
        limit=MASTER_VALUE_CAP,
        sort="desc",
    )
    data = await call_real_report_api(
        report_key=report_key,
        intent_spec=spec,
        validated_filters={},
        appuserid=scope.get("appuserid", ""),
        ip_address=scope.get("ip_address", "127.0.0.1"),
        yearcode=scope.get("yearcode", ""),
        sp_number=scope.get("sp_number"),
    )
    return [d for d in (data.get("dimensions") or []) if str(d).strip()]


async def get_report_masters(
    report_key: str,
    *,
    company_code: str = "",
    appuserid: str = "",
    ip_address: str = "127.0.0.1",
    yearcode: str = "",
    sp_number: Optional[int] = None,
    live: bool = True,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Return {masters: {column: {label, source, values}}} for a report.

    Cached per (company, report) — repeat calls return the stored payload and
    never re-hit the SP. ``refresh=True`` forces a live re-fetch. Static
    canonical literals are always included; live distinct values are merged on
    top when the real API is enabled. Per-column failures degrade to
    source='static'/'error' without failing the whole response.
    """
    if not refresh:
        cached = get_cached_master_payload(report_key, company_code)
        if cached is not None:
            return {**cached, "cached": True}

    cfg = _REGISTRY.get(report_key, {})
    columns_cfg = cfg.get("columns") or {}
    columns = get_master_columns(report_key)

    live_ok = bool(live and settings.use_real_api)
    result: Dict[str, Any] = {"report_key": report_key, "masters": {}}
    vocab: Dict[str, List[str]] = {}

    async def one(col: str):
        meta = columns_cfg.get(col, {}) or {}
        label = meta.get("description") or col
        static = sorted({v for v in get_canonical_values(report_key, col).values() if v})
        values: List[str] = list(static)
        source = "static"
        if live_ok:
            try:
                live_vals = await _fetch_column_values(
                    report_key, col,
                    appuserid=appuserid, ip_address=ip_address,
                    yearcode=yearcode, sp_number=sp_number,
                )
                if live_vals:
                    have = {v.lower() for v in values}
                    values = live_vals + [v for v in values if v.lower() not in have]
                    source = "live" if not static else "live+static"
            except Exception as exc:  # per-column degrade, never fail the batch
                logger.warning("masters fetch failed %s.%s: %s", report_key, col, exc)
                source = "static" if static else "error"
        return col, {"label": label, "source": source, "values": values, "count": len(values)}

    for col, entry in await asyncio.gather(*(one(c) for c in columns)):
        result["masters"][col] = entry
        if entry["values"]:
            vocab[col] = entry["values"]

    result["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["cached"] = False
    _store_masters(company_code, report_key, vocab, payload=dict(result))
    return result


def report_vocabulary(report_key: str, company_code: str = "") -> Dict[str, List[str]]:
    """{column: [values]} — canonical literals + live cached master values.

    Used to decide which column a qualifier word actually belongs to (e.g.
    'rnd' is an order-type value, not a department) and to rescue qualifiers
    the LLM dropped from the parse.
    """
    cfg = _REGISTRY.get(report_key, {}) or {}
    cols = cfg.get("columns") or {}
    live = get_cached_masters(report_key, company_code)
    if not live and not company_code:
        for (comp, rk), (exp, values) in _MASTER_CACHE.items():
            if rk == report_key and exp > time.time() and values:
                live = values
                break
    vocab: Dict[str, List[str]] = {}
    for col in cols:
        vals: List[str] = []
        have = set()
        # canonical aliases collapse onto the same literal ('rnd','r&d' -> 'RND')
        for v in get_canonical_values(report_key, col).values():
            v = str(v).strip()
            if v and v.lower() not in have:
                vals.append(v)
                have.add(v.lower())
        for v in (live or {}).get(col, []):
            v = str(v).strip()
            if v and v.lower() not in have:
                vals.append(v)
                have.add(v.lower())
        if vals:
            vocab[col] = vals
    return vocab


def find_column_for_value(
    report_key: str,
    value: str,
    company_code: str = "",
    exclude: Optional[set] = None,
    allow_long_needle: bool = True,
) -> Optional[tuple]:
    """Return (column, matched_value) when ``value`` belongs to exactly one
    column's vocabulary — else None.

    Case-insensitive exact first, then unambiguous containment. None is
    returned when nothing matches or more than one (column, value) pair claims
    the word — ambiguous cases are safer left untouched.

    ``allow_long_needle=False`` drops the ``needle.startswith(value)``
    direction — used by question-word rescue where a phrase like 'diamond in
    yesterday' must not match the value 'Diamond' on its head word alone.
    """
    needle = (value or "").strip().lower()
    if not needle:
        return None
    vocab = report_vocabulary(report_key, company_code)
    exclude = {e.lower() for e in (exclude or set())}
    exact = [(c, v) for c, vs in vocab.items() if c.lower() not in exclude
             for v in vs if v.lower() == needle]
    # Prefix containment only: 'repair' starts 'Repair Job' (headword match),
    # but 'order' is just the tail of 'Sales Order' — tail-word hits are
    # almost always false positives on generic nouns. Values shorter than
    # 3 chars ('P', 'W' metal colors) or 1-2 char needles are excluded —
    # they prefix-match unrelated words ('priority jobs' -> 'P').
    contain = [(c, v) for c, vs in vocab.items() if c.lower() not in exclude
               for v in vs
               if len(needle) >= 3 and len(v.strip()) >= 3
               and (v.lower().startswith(needle)
                    or (allow_long_needle and needle.startswith(v.lower())))]
    if exact:
        # An exact hit only wins if no OTHER column also claims the word —
        # 'regular' is exact for OrderTypeName but also inside jobtype's
        # 'Regular Job'; contested words must not be retargeted.
        ex_cols = {c for c, _ in exact}
        if len(exact) == 1 and not ({c for c, _ in contain} - ex_cols):
            return exact[0]
        return None
    return contain[0] if len(contain) == 1 else None


def match_master_value(
    report_key: str,
    column: str,
    value: str,
    company_code: str = "",
) -> str:
    """Map a user-supplied filter word to a live master value, if unambiguous.

    Conservative: exact case-insensitive hit, else single substring/containment
    candidate. Returns the original value when nothing clear matches.
    """
    vocab = get_cached_masters(report_key, company_code)
    if not vocab and not company_code:
        # No tenant context (e.g. inside validated_filters): use any cached
        # vocab for this report — master values are enum-like and the query
        # still executes under the real tenant scope.
        for (comp, rk), (exp, values) in _MASTER_CACHE.items():
            if rk == report_key and exp > time.time() and values:
                vocab = values
                break
    if not vocab:
        return value
    candidates = vocab.get(column) or []
    if not candidates:
        return value
    needle = (value or "").strip().lower()
    if not needle:
        return value
    for v in candidates:
        if v.lower() == needle:
            return v
    hits = [v for v in candidates if needle in v.lower() or v.lower() in needle]
    return hits[0] if len(hits) == 1 else value


def clear_master_cache(report_key: str = "", company_code: str = "") -> int:
    """Invalidate cached masters (optionally scoped). Returns entries removed."""
    def _doomed(store):
        return [
            k for k in store
            if (not report_key or k[1] == report_key) and (not company_code or k[0] == company_code.upper())
        ]
    n = 0
    for store in (_MASTER_CACHE, _PAYLOAD_CACHE):
        for k in _doomed(store):
            del store[k]
            n += 1
    return n
