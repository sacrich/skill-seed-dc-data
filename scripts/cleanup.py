#!/usr/bin/env python3
"""
cleanup.py — Delete all demo artifacts created by seed-demo-data for a given client.

Deletes (in safe order):
  1. Segments        — client-slug prefixed, always safe
  2. Calculated Insights — client-slug prefixed, always safe
  3. Data Streams    — client-slug prefixed, always safe
  4. State file      — state-<slug>.json in CWD

With --full flag also deletes (shared — use with care):
  5. DMO Relationships — for industry-specific DMOs
  6. Custom DMOs       — industry-specific ones only (never standard ssot__ DMOs)

Usage:
    python3 cleanup.py --config config-clalit.json
    python3 cleanup.py --config config-clalit.json --full
    python3 cleanup.py --config config-clalit.json --dry-run

Idempotent: safe to run multiple times. 404s are treated as already-deleted.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import get_tokens, api  # noqa: E402

API_V = "v62.0"
BASE = f"/services/data/{API_V}/ssot"

# Industry-specific custom DMO names (shared across demos — only deleted with --full)
INDUSTRY_DMOS = {
    "insurance":   ["InsurancePolicy__dlm", "InsuranceClaim__dlm"],
    "food":        ["PurchaseOrder__dlm", "OrderLine__dlm", "LoyaltyTransaction__dlm"],
    "food_b2b":    ["WholesaleOrder__dlm", "WholesaleOrderLine__dlm", "LoyaltyTransaction__dlm"],
    "retail":      ["SalesOrder__dlm", "RetailOrderLine__dlm", "LoyaltyTransaction__dlm"],
    "banking":     ["FinancialAccount__dlm", "BankingTransaction__dlm", "BankingProduct__dlm", "LoyaltyTransaction__dlm"],
    "pharma":      ["Prescription__dlm"],
    "telco":       ["ServiceContract__dlm", "UsageRecord__dlm"],
    "hightech":    ["HtSubscription__dlm", "HtUsageRecord__dlm", "HtSupportTicket__dlm"],
    "utilities":   ["EnergyContract__dlm", "UsageReading__dlm"],
    "airlines":    ["FlightBooking__dlm", "LoyaltyTransaction__dlm"],
    "healthcare":  ["MedicalVisit__dlm", "LabResult__dlm"],
    "sports_club": ["Membership__dlm", "ActivityRecord__dlm"],
    "ecommerce":   ["EcomOrder__dlm", "EcomOrderLine__dlm", "CartAbandonment__dlm"],
    "hospitality": ["HotelStay__dlm", "LoyaltyTransaction__dlm"],
    "media":       ["Subscription__dlm", "ContentView__dlm"],
    "automotive":  ["Vehicle__dlm", "ServiceRecord__dlm"],
    "real_estate": ["PropertyInquiry__dlm", "PropertyTransaction__dlm"],
    "betting":     ["BettingAccount__dlm", "BettingTransaction__dlm"],
}

# Standard DMOs — never delete these
STANDARD_DMOS = {
    "ssot__Individual__dlm",
    "ssot__Account__dlm",
    "ssot__ContactPointEmail__dlm",
    "ssot__AccountEmailAddress__dlm",
    "ssot__WebsiteEngagement__dlm",
    "ssot__EmailEngagement__dlm",
}


def _list_all(core_url, token, endpoint, key, params=""):
    """Paginated GET — returns all items from a list endpoint."""
    items = []
    url = f"{BASE}/{endpoint}?dataspace=default{params}"
    while url:
        st, resp = api(core_url, token, "GET", url)
        if st not in (200, 201):
            return items
        if isinstance(resp, list):
            items.extend(resp)
            break
        data = resp.get(key) or resp.get("records") or resp.get("items") or []
        items.extend(data if isinstance(data, list) else [])
        # pagination
        nxt = resp.get("nextPageUrl") or resp.get("nextRecordsUrl") or ""
        url = nxt if nxt else None
    return items


def delete_segments(core_url, token, slug, dry_run):
    print("\n  🗑️  Segments...")
    items = _list_all(core_url, token, "segments", "segments")
    deleted = 0
    for seg in items:
        name = seg.get("apiName", "") or seg.get("name", "") or seg.get("marketSegmentApiName", "")
        msid = seg.get("marketSegmentId") or seg.get("id") or seg.get("marketSegmentApiName")
        if not name.upper().startswith(slug.upper()):
            continue
        if dry_run:
            print(f"    [dry-run] would delete segment: {name}")
            deleted += 1
            continue
        st, _ = api(core_url, token, "DELETE",
                    f"{BASE}/segments/{msid}?dataspace=default")
        if st in (200, 201, 204, 404):
            print(f"    ✅ deleted segment: {name}")
            deleted += 1
        else:
            print(f"    ⚠️  could not delete segment {name}: HTTP {st}")
    if deleted == 0:
        print("    (no segments found for this slug)")
    return deleted


def delete_cis(core_url, token, slug, dry_run):
    print("\n  🗑️  Calculated Insights...")
    items = _list_all(core_url, token, "calculated-insights", "calculatedInsights")
    deleted = 0
    for ci in items:
        name = ci.get("apiName", "") or ci.get("name", "")
        if not name.upper().startswith(slug.upper()):
            continue
        if dry_run:
            print(f"    [dry-run] would delete CI: {name}")
            deleted += 1
            continue
        st, resp = api(core_url, token, "DELETE",
                       f"{BASE}/calculated-insights/{name}?dataspace=default")
        if st in (200, 201, 204, 404):
            print(f"    ✅ deleted CI: {name}")
            deleted += 1
        elif st == 400 and "SEGMENT" in str(resp).upper():
            print(f"    ⚠️  CI {name} is still referenced by a segment — delete segments first, then retry")
        else:
            print(f"    ⚠️  could not delete CI {name}: HTTP {st} — {resp}")
    if deleted == 0:
        print("    (no CIs found for this slug)")
    return deleted


def delete_streams(core_url, token, slug, dry_run):
    print("\n  🗑️  Data Streams...")
    items = _list_all(core_url, token, "data-streams", "dataStreams")
    deleted = 0
    for stream in items:
        name = stream.get("name", "") or stream.get("apiName", "")
        if not name.upper().startswith(slug.upper()):
            continue
        if dry_run:
            print(f"    [dry-run] would delete stream: {name}")
            deleted += 1
            continue
        st, _ = api(core_url, token, "DELETE",
                    f"{BASE}/data-streams/{name}?dataspace=default")
        if st in (200, 201, 204, 404):
            print(f"    ✅ deleted stream: {name}")
            deleted += 1
        else:
            print(f"    ⚠️  could not delete stream {name}: HTTP {st}")
    if deleted == 0:
        print("    (no streams found for this slug)")
    return deleted


def delete_relationships(core_url, token, industry, dry_run):
    print("\n  🗑️  DMO Relationships (industry DMOs)...")
    dmos = INDUSTRY_DMOS.get(industry, [])
    deleted = 0
    for dmo in dmos:
        st, resp = api(core_url, token, "GET",
                       f"{BASE}/data-model-objects/{dmo}/relationships?dataspace=default")
        if st == 404:
            continue  # DMO doesn't exist, nothing to do
        rels = resp.get("relationships") or resp.get("records") or []
        if isinstance(resp, list):
            rels = resp
        for rel in rels:
            rel_id = rel.get("id") or rel.get("relationshipId") or rel.get("name")
            if not rel_id:
                continue
            if dry_run:
                print(f"    [dry-run] would delete relationship: {dmo} → {rel_id}")
                deleted += 1
                continue
            st2, _ = api(core_url, token, "DELETE",
                         f"{BASE}/data-model-objects/{dmo}/relationships/{rel_id}?dataspace=default")
            if st2 in (200, 201, 204, 404):
                print(f"    ✅ deleted relationship on {dmo}: {rel_id}")
                deleted += 1
            else:
                print(f"    ⚠️  could not delete relationship {rel_id} on {dmo}: HTTP {st2}")
    if deleted == 0:
        print("    (no relationships found for industry DMOs)")
    return deleted


def delete_dmos(core_url, token, industry, dry_run):
    print("\n  🗑️  Custom DMOs...")
    dmos = [d for d in INDUSTRY_DMOS.get(industry, []) if d not in STANDARD_DMOS]
    # Deduplicate (LoyaltyTransaction__dlm shared across industries)
    seen = set()
    deleted = 0
    for dmo in dmos:
        if dmo in seen:
            continue
        seen.add(dmo)
        # Check if DMO exists
        st, _ = api(core_url, token, "GET",
                    f"{BASE}/data-model-objects/{dmo}?dataspace=default")
        if st == 404:
            continue
        if dry_run:
            print(f"    [dry-run] would delete DMO: {dmo}")
            deleted += 1
            continue
        st2, resp2 = api(core_url, token, "DELETE",
                         f"{BASE}/data-model-objects/{dmo}?dataspace=default")
        if st2 in (200, 201, 204, 404):
            print(f"    ✅ deleted DMO: {dmo}")
            deleted += 1
        elif st2 == 400 and ("MAPPING" in str(resp2).upper() or "RELATIONSHIP" in str(resp2).upper()):
            print(f"    ⚠️  DMO {dmo} still has mappings or relationships — delete them first")
        else:
            print(f"    ⚠️  could not delete DMO {dmo}: HTTP {st2} — {resp2}")
    if deleted == 0:
        print("    (no custom DMOs found for this industry — may be shared with another demo)")
    return deleted


def delete_state_file(slug, cwd):
    state_path = cwd / f"state-{slug.lower()}.json"
    if state_path.exists():
        state_path.unlink()
        print(f"\n  🗑️  State file deleted: {state_path.name}")


def main():
    parser = argparse.ArgumentParser(description="Clean up all demo artifacts for a client slug.")
    parser.add_argument("--config", required=True, help="Path to config-<slug>.json")
    parser.add_argument("--full", action="store_true",
                        help="Also delete industry DMOs and relationships (shared — use with care)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without deleting anything")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"❌  Config file not found: {args.config}")
        sys.exit(1)

    cfg = json.loads(cfg_path.read_text())
    org_alias = cfg.get("org_alias") or cfg.get("alias")
    slug      = cfg.get("client_slug") or cfg.get("slug")
    industry  = cfg.get("industry", "")

    if not org_alias or not slug:
        print("❌  Config must have 'org_alias' and 'client_slug' fields.")
        sys.exit(1)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}🧹  Cleanup: {slug} ({industry}) on org {org_alias}")
    if args.full:
        print("  ⚠️  --full flag: will also delete industry DMOs and relationships.")
        print("      These are SHARED across demos on this org.")
        print("      Only proceed if no other demo uses this industry on the same org.\n")
        resp = input("  Type YES to confirm: ").strip()
        if resp != "YES":
            print("  Aborted.")
            sys.exit(0)

    print("\n  🔐  Authenticating...")
    core_url, core_token, _, _ = get_tokens(org_alias)
    print(f"  ✅  Connected to {core_url}")

    # Delete in safe order: segments first (depend on CIs), then CIs, then streams
    delete_segments(core_url, core_token, slug, args.dry_run)
    time.sleep(1)
    delete_cis(core_url, core_token, slug, args.dry_run)
    time.sleep(1)
    delete_streams(core_url, core_token, slug, args.dry_run)

    if args.full and industry:
        time.sleep(1)
        delete_relationships(core_url, core_token, industry, args.dry_run)
        time.sleep(1)
        delete_dmos(core_url, core_token, industry, args.dry_run)

    if not args.dry_run:
        delete_state_file(slug.lower(), cfg_path.parent)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}✅  Cleanup complete for {slug}.")
    if not args.full:
        print("     DMOs and relationships were NOT deleted (shared across demos).")
        print("     Re-run with --full to remove them too.")


if __name__ == "__main__":
    main()
