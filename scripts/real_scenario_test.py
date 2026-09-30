"""Real-scenario question sweep: 50 questions per report.

Covers the phrasing real users type — multi-filter analytics, top-N,
comparisons, multi-period, and common Indian-English typos — run through
the real /chat endpoint. Writes results to scripts/real_scenario_results.json
and prints a pass/fail summary.

Usage:
    .\\.venv\\Scripts\\python.exe scripts\\real_scenario_test.py
    .\\.venv\\Scripts\\python.exe scripts\\real_scenario_test.py --report sales_report
"""

import argparse
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

QUESTIONS = {
    # ------------------------------------------------------------------
    # SALES — UI already shows totals; users ask AI for cuts, rankings,
    # comparisons, and typo-laden phrasing.
    # ------------------------------------------------------------------
    "sales_report": [
        # multi-filter
        "ring sales by customer this month",
        "18k gold sales by sales rep this year",
        "gold ring net weight by branch",
        "pendant sales by customer today",
        "bangle sales by design this month",
        "diamond sales by customer this year",
        "sales of ring and pendant this month",
        "18k ring sales today",
        "gold sales by customer by month",
        "customer wise gold weight this year",
        # analytics / rankings
        "which customer bought the most gold this year",
        "top 5 designs by net weight this month",
        "which sales rep sold most diamond pieces",
        "top 3 categories by sales this month",
        "which branch gave highest sales this year",
        "least selling design this month",
        "which metal type is selling best",
        "top customer by diamond weight",
        "average bill value by customer this month",
        "which product type performs worst",
        # multi-period / comparison
        "sales for this year this month and today",
        "gross weight for this year today and month",
        "compare sales this month vs last month",
        "net weight this week and last week",
        "sales growth this month",
        "compare gold weight today vs yesterday",
        "units sold this month and last month",
        "diamond pieces for today and yesterday",
        "sales today and yesterday",
        "compare category wise sales this month vs last month",
        # typos / indian english
        "totel selas today",
        "gros wieght this mnth",
        "custmer wise selas",
        "wich disign sell most",
        "top custmer by amout",
        "selas rep performance",
        "dimond pcs today",
        "yestrday total sell",
        "net wt of gold tday",
        "comper selas this mnth last mnth",
        # natural phrasing
        "how much business we did today",
        "what did we sell most this month",
        "who is buying diamond jewellery",
        "which designs should we make more",
        "what is our best selling item",
        "how many customers came today",
        "give me sales summary for this week",
        "what is trending in our sales",
        "which category is not performing",
        "show me something interesting about sales",
    ],
    # ------------------------------------------------------------------
    # ORDER — users want pipeline insight, not the raw grid.
    # ------------------------------------------------------------------
    "order_report": [
        # multi-filter
        "pending orders by customer this month",
        "sold orders by sales rep this year",
        "corporate orders by design this month",
        "sample line orders by customer",
        "orders by customer this month",
        "order amount by design this year",
        "customer wise order quantity this month",
        "order gross weight by customer",
        "orders by order type this month",
        "design wise remaining quantity",
        # analytics
        "which customer has most pending orders",
        "top 5 designs by order amount",
        "who ordered most this year",
        "which order type is most common",
        "biggest order by amount this month",
        "how many quotation jobs are pending",
        "orders stuck in pending",
        "top customer by order quantity",
        "which designs are ordered most",
        "order vs pending ratio this month",
        # multi-period / comparison
        "order gross wt for this year today and month",
        "compare orders this month vs last month",
        "order amount for this month and last month",
        "remaining jobs today and this week",
        "eta jobs this month",
        "quotation jobs for this year",
        "order amount today and yesterday",
        "orders placed this week and last week",
        "pending orders today",
        "order quantity for this year and this month",
        # typos / indian english
        "totel ordrs today",
        "pendng ordr by custmer",
        "wich custmer ordr most",
        "ordr amout this mnth",
        "etajobs todya",
        "remaning ordrs",
        "qutation jobs cont",
        "gros wt ordr today",
        "comper ordr this mnth last mnth",
        "how meny ordrs pendng",
        # natural phrasing
        "how many orders we got today",
        "what orders are still pending",
        "which customer gives us most orders",
        "show me today's order summary",
        "what is the value of pending orders",
        "how many orders did we complete this month",
        "which orders are running late",
        "give order pipeline for this month",
        "who placed the biggest order",
        "what should we deliver first",
    ],
    # ------------------------------------------------------------------
    # WIP — production insight: where jobs are stuck, who is slow.
    # ------------------------------------------------------------------
    "wip_report": [
        # multi-filter
        "wip jobs by department this month",
        "job cost by customer this year",
        "regular jobs by department",
        "repair jobs by worker",
        "jobs by location this month",
        "gross weight by department this year",
        "sample jobs by customer",
        "worker wise job cost",
        "customer wise wip jobs this month",
        "department wise net weight",
        # analytics
        "which department has most jobs",
        "top 5 customers by wip job cost",
        "which worker has most pending jobs",
        "where are jobs getting stuck",
        "highest job cost job right now",
        "which job type takes most cost",
        "jobs running behind schedule",
        "total wip value by department",
        "which customer jobs are in progress",
        "average job cost by department",
        # multi-period / comparison
        "req gross wt for this year today and month",
        "compare wip jobs this month vs last month",
        "job cost for this year and this month",
        "wip jobs today and yesterday",
        "new jobs this week and last week",
        "job cost this month",
        "wip jobs count for this year this month and today",
        "net weight for this month and today",
        "compare job cost this month vs last month",
        "jobs added today and yesterday",
        # typos / indian english
        "totel wip jobs",
        "wich departmnt has most jobs",
        "job cost by custmer",
        "pendingg jobs today",
        "werk wise jobs",
        "wip gros wieght",
        "how meny jobs in progres",
        "jobs stuk in departmnt",
        "comper wip this mnth last mnth",
        "reguler jobs count",
        # natural phrasing
        "how many jobs are in production",
        "what is stuck in manufacturing",
        "which department is overloaded",
        "show me today's production summary",
        "what is the value of work in progress",
        "which jobs need attention",
        "who is working on most jobs",
        "how much work is pending in polishing",
        "give me production pipeline",
        "what is our factory workload",
    ],
    # ------------------------------------------------------------------
    # TAX — compliance/cross-check questions.
    # ------------------------------------------------------------------
    "tax_report": [
        # multi-filter
        "tax amount by month this year",
        "gst by customer this month",
        "tax amount by bill today",
        "customer wise tax this year",
        "monthly tax summary",
        "tax by category this month",
        "gst amount for gold sales",
        "tax by sales rep this month",
        "bill wise tax today",
        "tax amount by design this year",
        # analytics
        "which customer paid most tax",
        "highest tax bill today",
        "total gst collected this year",
        "which month had highest tax",
        "average tax per bill",
        "tax contribution by customer",
        "top 5 bills by tax amount",
        "which category generates most gst",
        "how much tax we collected today",
        "compare tax across months",
        # multi-period / comparison
        "tax amount for this year this month and today",
        "compare tax this month vs last month",
        "gst today and yesterday",
        "tax this week and last week",
        "tax growth this month",
        "tax for today",
        "tax for this month",
        "compare gst this year vs last year",
        "monthly tax for this year",
        "tax amount today and this month",
        # typos / indian english
        "totel tax amout",
        "gst collectd this mnth",
        "wich bill has higest tax",
        "tax by custmer",
        "comper tax this mnth last mnth",
        "todya gst",
        "montly tax sumary",
        "how meny tax we colect",
        "tax for gold selas",
        "bil wise tax",
        # natural phrasing
        "how much gst we paid this month",
        "what is our tax liability",
        "show me today's tax collection",
        "which customer contributes most tax",
        "give me tax summary for this quarter",
        "how much tax on gold sales",
        "what is the gst breakup",
        "tax report for this week",
        "which invoice has highest tax",
        "total tax collected this year",
    ],
}


# Questions that intentionally route to a different report than the section
# they are filed under. The Tax report has no customer/category/design dims —
# tax-by-customer analytics legitimately execute on Sales. Expected report is
# checked against the "Sources:" line in the answer.
EXPECTED_OVERRIDE = {
    "tax by customer": "sales_report",
    "tax by category": "sales_report",
    "tax by design": "sales_report",
    "tax by custmer": "sales_report",
    "which customer contributes most tax": "sales_report",
    "tax for gold selas": "sales_report",
    "how much tax on gold sales": "sales_report",
    "higest tax bill": "sales_report",
    "wich bill has higest tax": "sales_report",
    "which invoice has highest tax": "sales_report",
}


def _expected_report(section_report: str, question: str) -> str:
    return EXPECTED_OVERRIDE.get(question.strip().lower(), section_report)


def _source_matches(src: str, expected: str) -> bool:
    """Loose match of the 'Sources:' line against the expected report key."""
    if not src:
        return True  # no source line -> can't verify, don't penalise
    return src.lower().replace(" ", "").startswith(expected.split("_")[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", choices=sorted(QUESTIONS), default=None)
    parser.add_argument("--out", default="scripts/real_scenario_results.json")
    args = parser.parse_args()

    from fastapi.testclient import TestClient
    from app.main import app

    report_keys = [args.report] if args.report else list(QUESTIONS)
    results = []

    with TestClient(app) as client:
        client.post("/cache/invalidate")
        for rk in report_keys:
            print(f"\n{'=' * 70}\n{rk}  ({len(QUESTIONS[rk])} questions)\n{'=' * 70}")
            for i, q in enumerate(QUESTIONS[rk], 1):
                t0 = time.time()
                try:
                    r = client.post("/chat", json={
                        "question": q, "company_code": "DEMO",
                        "user_id": "u123", "appuserid": "admin@orail.co.in",
                        "yearcode": "", "ip_address": "",
                    })
                    d = r.json()
                    if r.status_code != 200:
                        print(f"[ERR] {i:2}. {q} -> HTTP {r.status_code}: {str(d)[:100]}")
                        results.append({
                            "report": rk, "question": q,
                            "status": f"http_{r.status_code}",
                            "answer": str(d)[:200], "sources": "",
                            "ok": False, "latency_s": round(time.time() - t0, 1),
                        })
                        continue
                    a = d.get("answer") or {}
                    status = d.get("status", "?")
                    answer = (a.get("value") or "").replace("\n", " | ")
                    latency = round(time.time() - t0, 1)
                    ok = status == "success" and bool(answer.strip())
                    # detect wrong-report routing from the Sources line
                    src = ""
                    if "Sources:" in answer:
                        src = answer.split("Sources:")[-1].strip()
                    expected = _expected_report(rk, q)
                    routed_ok = _source_matches(src, expected)
                    flag = "OK " if ok and routed_ok else ("ROUTE" if ok else "FAIL")
                    print(f"[{flag}] {i:2}. {q}\n      -> {answer[:140]}")
                    results.append({
                        "report": rk, "question": q, "status": status,
                        "expected_report": expected,
                        "answer": a.get("value", ""), "sources": src,
                        "ok": ok and routed_ok, "latency_s": latency,
                    })
                except Exception as exc:
                    print(f"[ERR] {i:2}. {q} -> {exc}")
                    results.append({
                        "report": rk, "question": q, "status": "exception",
                        "answer": str(exc), "sources": "", "ok": False,
                        "latency_s": round(time.time() - t0, 1),
                    })

    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for rk in report_keys:
        rows = [r for r in results if r["report"] == rk]
        ok = sum(1 for r in rows if r["ok"])
        # categorize failures: clarify / route_miss / http / exception / backend
        cats = {"clarify": 0, "route_miss": 0, "http": 0, "exception": 0, "backend": 0, "empty": 0}
        for r in rows:
            if r["ok"]:
                continue
            st = r["status"]
            if st == "clarify":
                cats["clarify"] += 1
            elif st.startswith("http_"):
                cats["http"] += 1
            elif st == "exception":
                cats["exception"] += 1
            elif st == "success":  # answered but wrong report
                cats["route_miss"] += 1
            elif st == "error":
                cats["backend"] += 1
            else:
                cats["empty"] += 1
        detail = " ".join(f"{k}={v}" for k, v in cats.items() if v)
        print(f"{rk:15} {ok}/{len(rows)} ok   ({detail})")
    fails = [r for r in results if not r["ok"]]
    if fails:
        print("\nFailures:")
        for r in fails:
            print(f"  [{r['report']}] {r['question']} -> {r['status']}: "
                  f"{(r['answer'] or '')[:90]}")
    print(f"\nFull results -> {args.out}")


if __name__ == "__main__":
    main()
