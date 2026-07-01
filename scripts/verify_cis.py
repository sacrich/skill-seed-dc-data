#!/usr/bin/env python3
"""
Verify that all Calculated Insights created for this config have data (row count > 0).

Usage:
    python3 verify_cis.py --config config.json
    python3 verify_cis.py --config config.json --timeout 1200  # wait up to 20 min
    python3 verify_cis.py --config config.json --trigger       # trigger run-now first

Polls every 30 seconds. A CI is considered "ready" when:
  SELECT COUNT(*) FROM <CI> returns > 0

CIs return 0 rows when:
  - IR had not completed when the CI ran (most common)
  - The source DMO PartyId__c values don't match any SourceRecordId__c in the UnifiedLink table
  - The CI ran before the custom DMO had data

Exit codes:
  0 — all CIs have rows
  1 — timeout or one or more CIs have 0 rows after retries

IMPORTANT: Do NOT create segments until this returns 0.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import get_tokens, api  # noqa: E402

API_V = "v62.0"
BASE  = f"/services/data/{API_V}/ssot"

DEFAULT_TIMEOUT_S = 1200   # 20 minutes — CIs run fast once IR is done
POLL_INTERVAL_S   = 30


# ── Industry CI name templates ────────────────────────────────────────────────

INDUSTRY_CIS = {
    "insurance": [
        "PolicySummary",
        "ClaimsSummary",
        "EngagementScore",
        "CustomerRiskProfile",
        "PolicyTypeBreakdown",
    ],
    "food": [
        "PurchaseSummary",
        "CategorySpend",
        "LoyaltyProfile",
        "CustomerValue",
        "EngagementScore",
    ],
    "food_b2b": [
        "WholesaleSummary",
        "CategoryPenetration",
        "AccountHealth",
        "OrderFrequency",
        "EngagementScore",
    ],
    "retail": [
        "PurchaseSummary",
        "CategoryAffinity",
        "ChannelProfile",
        "CustomerValue",
        "EngagementScore",
    ],
    "banking": [
        "AccountSummary",
        "ProductHoldings",
        "SpendingProfile",
        "CustomerRiskProfile",
        "EngagementScore",
    ],
    "pharma": [
        "PrescriptionSummary",
        "TherapeuticProfile",
        "AdherenceProfile",
        "CustomerHealthValue",
        "EngagementScore",
    ],
    "telco": [
        "ServiceSummary",
        "UsageProfile",
        "ChurnRisk",
        "ProductBundle",
        "EngagementScore",
    ],
    "hightech": [
        "SubscriptionSummary",
        "UsageHealthScore",
        "SupportProfile",
        "AccountHealthProfile",
        "EngagementScore",
    ],
    "real_estate": [
        "InquiryProfile",
        "TransactionProfile",
        "CustomerValue",
        "EngagementScore",
        "BudgetProfile",
    ],
}


def ci_api_names(slug: str, industry: str) -> list[str]:
    """Return the list of CI apiNames for this config."""
    base_names = INDUSTRY_CIS.get(industry, [])
    # Slug prefix: same logic used in create_calculated_insights.py
    prefix = slug.replace("-", "_").title().replace("_", "")
    return [f"{prefix}_{name}__cio" for name in base_names]


def query_ci_count(core_url: str, token: str, ci_name: str):
    """
    Run SELECT COUNT(*) on the CI. Returns the count, or None if the query errors
    (CI doesn't exist or hasn't materialized yet).
    """
    st, data = api(core_url, token, "POST",
                   f"{BASE}/query?dataspace=default",
                   body={"sql": f"SELECT COUNT(*) as cnt FROM {ci_name}"})
    if st != 200 or not isinstance(data, dict):
        return None
    # Response shape: {"data": [{"cnt": 1234}]} or {"records": [...]}
    rows = data.get("data") or data.get("records") or []
    if rows and isinstance(rows[0], dict):
        val = rows[0].get("cnt") or rows[0].get("CNT") or rows[0].get("count") or 0
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
    return None


def trigger_ci_run(core_url: str, token: str, ci_name: str) -> bool:
    """Trigger run-now on a single CI. Returns True if accepted."""
    st, resp = api(core_url, token, "POST",
                   f"{BASE}/calculated-insights/{ci_name}/actions/run?dataspace=default",
                   body={})
    rc = (resp.get("resultCode") if isinstance(resp, dict) else None) or ""
    return st in (200, 201, 202) or "ALREADY_IN_PROCESS" in str(rc)


def _status_icon(count) -> str:
    if count is None:
        return "❓"
    if count > 0:
        return "✅"
    return "⏳"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help=f"Max seconds to wait (default: {DEFAULT_TIMEOUT_S})")
    ap.add_argument("--trigger", action="store_true",
                    help="Trigger run-now on all CIs before polling")
    ap.add_argument("--cis", nargs="+",
                    help="Check specific CI apiNames instead of all industry CIs")
    args = ap.parse_args()

    cfg      = json.loads(Path(args.config).read_text())
    alias    = cfg["orgAlias"]
    slug     = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()

    if args.cis:
        target_cis = args.cis
    else:
        target_cis = ci_api_names(slug, industry)

    if not target_cis:
        print(f"  ⚠️  No CI names found for industry '{industry}'. "
              f"Pass --cis <name1> <name2> ... explicitly.")
        sys.exit(1)

    print(f"\n🔍  Verifying Calculated Insights — {cfg.get('clientName', slug)}")
    print(f"    Org:      {alias}")
    print(f"    Industry: {industry}")
    print(f"    Timeout:  {args.timeout}s (polling every {POLL_INTERVAL_S}s)\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}\n")

    print(f"  Watching {len(target_cis)} CI(s):")
    for ci in target_cis:
        print(f"    • {ci}")
    print()

    # Optionally trigger runs
    if args.trigger:
        print("  ▶  Triggering CI run-now...")
        for ci in target_cis:
            ok = trigger_ci_run(core_url, core_token, ci)
            print(f"    {'✓' if ok else '⚠️'} {ci}")
        print()
        time.sleep(10)

    start_t    = time.time()
    last_print = 0.0
    max_len    = max(len(ci) for ci in target_cis)

    while True:
        elapsed = time.time() - start_t
        now_str = time.strftime("%H:%M:%S")

        counts = {}
        for ci in target_cis:
            counts[ci] = query_ci_count(core_url, core_token, ci)

        if time.time() - last_print >= POLL_INTERVAL_S - 2 or elapsed < 5:
            print(f"  [{now_str}]  elapsed={int(elapsed)}s")
            for ci in target_cis:
                cnt = counts[ci]
                icon = _status_icon(cnt)
                cnt_str = f"{cnt:,}" if cnt is not None else "query error"
                print(f"    {icon}  {ci:<{max_len}}  rows={cnt_str}")
            print()
            last_print = time.time()

        # ── All CIs have data ─────────────────────────────────────────────
        all_ok = all(c is not None and c > 0 for c in counts.values())
        if all_ok:
            print(f"  ✅  All {len(target_cis)} CIs have data!")
            print(f"  Total wait: {int(elapsed)}s\n")
            print(f"  Row counts:")
            for ci in target_cis:
                print(f"    • {ci}: {counts[ci]:,} rows")
            print()
            print(f"  ✅  GATE PASSED — safe to proceed to Step 6f (create Segments)\n")

            out_dir = Path(cfg.get("outputDir", f"data/{slug}"))
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "ci_status.json").write_text(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_s": int(elapsed),
                "all_ok": True,
                "cis": {ci: counts[ci] for ci in target_cis},
            }, indent=2))
            sys.exit(0)

        # ── Timeout ───────────────────────────────────────────────────────
        if elapsed >= args.timeout:
            empty = [ci for ci, cnt in counts.items() if not cnt]
            print(f"  ⏰  Timeout after {args.timeout}s. {len(empty)} CI(s) still have 0 rows:")
            for ci in empty:
                print(f"       • {ci}  rows={counts[ci]}")
            print()
            print(f"  Diagnose:")
            print(f"    1. Confirm IR is done: python3 verify_ir.py --config {args.config}")
            print(f"    2. Check PartyId__c match:")
            print(f"       SELECT SourceRecordId__c FROM UnifiedLinkssotIndividualRt__dlm LIMIT 3")
            print(f"       → must match the contact_id UUIDs in your transactional DMOs")
            print(f"    3. Re-trigger and wait: python3 verify_cis.py --config {args.config} --trigger --timeout 1200\n")
            sys.exit(1)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
