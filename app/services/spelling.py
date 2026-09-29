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
