"""Metric validation — Layer 2 metric-type guard and unit classification.

Detects the user's semantic intent type (weight/amount/count/rate) from the
question and corrects the LLM's metric choice when there's a type mismatch.
Also provides the unit label (currency/weight/count/rate) used downstream.
"""
import logging
import re
from typing import Any, Dict

from app.services.catalog_builder import _get_metric_catalog

logger = logging.getLogger(__name__)


# Intent type detection from question keywords
_WEIGHT_KEYWORDS = {
    "weight", "wt", "gram", "gm", "gms", "carat", "ct", "ctw",
    "material", "materials", "material used", "material weight",
    "material qty", "material breakup", "total material",
    "gross weight", "gross wt", "net weight", "net wt",
}
_AMOUNT_KEYWORDS = {
    "amount", "value", "cost", "price", "total amount", "business amount",
    "sales amount", "bill amount", "revenue", "total sales",
    "gold amount", "gold value", "silver amount", "diamond amount",
    "metal amount", "labour amount", "labour charge",
}
_COUNT_KEYWORDS = {
    # Explicit counting phrases only. Bare entity nouns (customers, bills,
    # designs, jobs) are NOT here — "top 5 customers by revenue" is an
    # amount question where 'customers' is the dimension, not the metric.
    "how many", "count", "number of", "no of", "no. of",
    "total records", "pieces", "pcs",
}
_RATE_KEYWORDS = {"rate", "per gram", "per carat", "per gm"}


def _kw_re(keywords) -> "re.Pattern":
    """Compile a keyword set into a word-boundary regex.

    Substring matching ('kw in q') caused false positives — 'generates'
    matched 'rate', 'discount' matched 'count'. Word boundaries fix this.
    """
    parts = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)


_WEIGHT_RE = _kw_re(_WEIGHT_KEYWORDS)
_COUNT_RE = _kw_re(_COUNT_KEYWORDS)
_RATE_RE = _kw_re(_RATE_KEYWORDS)
_AMOUNT_RE = _kw_re(_AMOUNT_KEYWORDS)


# "total <countable entity>" → count intent ("total order" = how many orders).
# Negative lookahead keeps "total order amount" / "total sales value" as amount.
_COUNT_ENTITY_RE = re.compile(
    r"\btotal\s+(?:no\.?\s*of\s+)?"
    r"(orders?|jobs?|bills?|customers?|designs?|pieces|items?|"
    r"transactions?|records?|invoices?|vouchers?|entries|products?)\b"
    r"(?!\s*(?:amount|value|amt|sales|revenue|weight|wt|cost|price))",
    re.IGNORECASE,
)


def _detect_intent_type(question: str) -> str:
    """Detect the user's semantic intent type from the question.

    Returns one of: 'weight', 'amount', 'count', 'rate', 'unknown'.
    Priority: count > rate > weight > amount (most specific first).
    """
    q = " " + question.lower().strip() + " "

    if _COUNT_ENTITY_RE.search(q):
        return "count"

    # Check count first (most specific — "how many" is unambiguous)
    if _COUNT_RE.search(q):
        return "count"

    # Check rate
    if _RATE_RE.search(q):
        return "rate"

    # Check weight — but "gold amount" should NOT trigger weight even though "gold" is present
    # Strip amount phrases first to avoid false positives
    q_without_amount = q
    for kw in ("gold amount", "gold value", "gold cost", "silver amount",
               "diamond amount", "metal amount", "labour amount",
               "total amount", "business amount", "sales amount", "bill amount"):
        q_without_amount = q_without_amount.replace(kw, "")

    if _WEIGHT_RE.search(q_without_amount):
        return "weight"

    # Check amount
    if _AMOUNT_RE.search(q):
        return "amount"

    return "unknown"


def validate_metric_intent(metric: str, question: str, report_key: str = "sales_report") -> str:
    """Validate that the LLM's chosen metric matches the user's intent type.

    If the question asks for weight but the metric is an amount, override
    to the correct weight metric. Similarly for count/rate mismatches.

    Returns the corrected metric name (or the original if no mismatch).
    """
    catalog = _get_metric_catalog(report_key)
    if not catalog:
        return metric

    metric_meta = catalog.get(metric, {})
    metric_type = metric_meta.get("type", "amount")  # default to amount

    intent_type = _detect_intent_type(question)

    if intent_type == "unknown" or intent_type == metric_type:
        return metric  # no mismatch

    q_lower = question.lower()

    best_match = None
    best_score = 0

    for cat_metric, cat_meta in catalog.items():
        if cat_meta.get("type") != intent_type:
            continue
        for alias in cat_meta.get("aliases", []):
            if alias in q_lower:
                score = len(alias)  # longer match = more specific
                if score > best_score:
                    best_score = score
                    best_match = cat_metric

    if best_match:
        logger.warning(
            "Metric intent override: '%s' (type=%s) → '%s' (type=%s) "
            "for question: %s",
            metric, metric_type, best_match, intent_type, question[:80]
        )
        return best_match

    # No specific match found — use type-based fallback
    # weight → grosswt, count → total_count, rate → MetalRate
    type_fallbacks = {
        "weight": "grosswt",
        "count": "total_count",
        "rate": "MetalRate",
    }
    fallback = type_fallbacks.get(intent_type)
    if fallback and fallback in catalog:
        logger.warning(
            "Metric intent fallback: '%s' (type=%s) → '%s' (type=%s) "
            "for question: %s",
            metric, metric_type, fallback, intent_type, question[:80]
        )
        return fallback

    return metric


# ── Metric unit classification (shared across parser, main.py, block_builder) ──
# Hardcoded sets serve as fallback; primary lookup is dynamic from report_columns.json
CURRENCY_METRICS = {
    "Amount", "design_TotalAmouont", "Discount", "totaltaxAmount",
    "MetalAmount", "DiamondAmount", "ColorStoneAmount", "LabourAmount",
    "OtherAmount", "UnitCost", "TotalSettingCost", "TotalDiamondHandling",
    "totalLabourAmt", "totalOtherAmt",
    "GoldAmt", "SilverAmt", "PlatinumAmt", "OtherAmt", "WastageAmount",
}
WEIGHT_METRICS = {
    "grosswt", "netwt", "netwt_24k", "packageWt",
    "MetalLoss", "NetWtWithLoss",
    "GoldWt", "SilverWt", "PlatinumWt", "OtherWt",
    "Pure_Gold_Wt", "Pure_Silver_Wt", "Pure_Platinum_Wt", "Pure_Other_Wt",
    "miscwt", "dctw", "csctw", "D_Wt_Cm", "D_Wt_Ct", "C_Wt_Cm", "C_Wt_Ct",
}
COUNT_METRICS = {"total_count", "unique_customers", "unique_designs"}
RATE_METRICS = {"MetalRate", "Tunch", "Wastage"}


def get_metric_unit(metric_name: str, report_key: str = "sales_report") -> str:
    """Return the unit classification for a metric name.

    Primary lookup: dynamic from report_columns.json type field.
    Fallback: hardcoded sets above.
    """
    from app.services.column_registry import get_metric_unit as _dynamic_unit
    dynamic = _dynamic_unit(metric_name, report_key)
    if dynamic != "currency" or metric_name in CURRENCY_METRICS:
        # If dynamic found a non-currency type, or metric is in hardcoded currency set, use dynamic
        if dynamic != "currency":
            return dynamic
        if metric_name in CURRENCY_METRICS:
            return "currency"
    if metric_name in WEIGHT_METRICS:
        return "weight"
    if metric_name in COUNT_METRICS:
        return "count"
    if metric_name in RATE_METRICS:
        return "rate"
    return dynamic


def get_metric_unit_label(metric_name: str, report_key: str = "sales_report") -> str:
    """Return the display unit suffix for a metric (e.g. 'ctw', 'pcs', 'g', '%')."""
    from app.services.column_registry import get_metric_unit_label as _dynamic_label
    return _dynamic_label(metric_name, report_key)
