#!/usr/bin/env python3
"""
Register DMO-to-DMO relationships via Salesforce Metadata API.

Usage:
    python3 create_relationships.py --config config.json

Why this matters:
  - Without relationships, CIs that JOIN across DMOs fail or return no rows
  - The Data Graph traversal (for the Contact 360 page) needs these edges
  - Segments using related-object criteria need them

Implementation note (proven from reference build-data360-demo skill):
  - REST POST /ssot/data-model-objects/{dmo}/relationships returns UNKNOWN_EXCEPTION
  - The ONLY working path is Metadata API: sf project deploy start with
    fieldSrcTrgtRelationship-meta.xml files in force-app/main/default/fieldSrcTrgtRelationships/
  - Pre-check existing joins via GET /ssot/data-model-objects/{child}/relationships to avoid
    deploying a conflicting INACTIVE duplicate (RATÉ dryrunv5 2026-06-06)

Industry relationship graphs:

  standard (B2C, all industries):
    Individual ──< ssot__WebsiteEngagement  (ssot__IndividualId__c → ssot__Individual__dlm.ssot__Id__c)
    Individual ──< ssot__EmailEngagement    (ssot__IndividualId__c → ssot__Individual__dlm.ssot__Id__c)

  standard (B2B, food_b2b/hightech):
    Account ──< ssot__WebsiteEngagement     (ssot__IndividualId__c → ssot__Account__dlm.ssot__Id__c)
    Account ──< ssot__EmailEngagement       (ssot__IndividualId__c → ssot__Account__dlm.ssot__Id__c)

  insurance:
    Individual ──< InsurancePolicy   (PartyId__c → ssot__Individual__dlm.ssot__Id__c)
    InsurancePolicy ──< InsuranceClaim  (PolicyId__c → InsurancePolicy__dlm.Id__c)
    Individual ──< InsuranceClaim    (PartyId__c → Individual, for direct joins)

  food:
    Individual ──< PurchaseOrder     (PartyId__c → Individual)
    PurchaseOrder ──< OrderLine      (OrderId__c → PurchaseOrder__dlm.Id__c)
    Individual ──< LoyaltyTransaction (PartyId__c → Individual)

  retail:
    Individual ──< SalesOrder        (PartyId__c → Individual)
    SalesOrder ──< OrderLine         (OrderId__c → SalesOrder__dlm.Id__c)
    Individual ──< LoyaltyTransaction (PartyId__c → Individual)

  banking:
    Individual ──< FinancialAccount  (PartyId__c → Individual)
    FinancialAccount ──< Transaction (AccountId__c → FinancialAccount__dlm.Id__c)
    Individual ──< BankingProduct    (PartyId__c → Individual)
    Individual ──< LoyaltyTransaction (PartyId__c → Individual)

  pharma:
    Individual ──< Prescription      (PartyId__c → Individual)

  telco:
    Individual ──< ServiceContract   (PartyId__c → Individual)
    ServiceContract ──< UsageRecord  (ContractId__c → ServiceContract__dlm.Id__c)

Idempotent: existing relationships (any status) are skipped; re-deploying an ALREADY-EXISTING
FieldSrcTrgtRelationship creates a conflicting INACTIVE duplicate that never activates.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import get_tokens, api  # noqa: E402

API_V = "v62.0"
BASE = f"/services/data/{API_V}/ssot"
IND = "ssot__Individual__dlm"
ACCT = "ssot__Account__dlm"

# Standard relationships — created for EVERY B2C industry.
# NOTE: ssot__WebsiteEngagement__dlm and ssot__EmailEngagement__dlm are PLATFORM STANDARD DMOs.
# Their built-in relationships to Individual may already exist on the org.
# The idempotency check (existing_joins_for_dmo) will skip them if already present.
# NOTE: IndividualProfile__dlm removed — enrichment fields now live on ssot__Individual__dlm directly.
STANDARD_RELATIONSHIPS = [
    ("ssot__WebsiteEngagement__dlm", "ssot__IndividualId__c", IND, "ssot__Id__c", "WebsiteEngagement → Individual"),
    ("ssot__EmailEngagement__dlm",   "ssot__IndividualId__c", IND, "ssot__Id__c", "EmailEngagement → Individual"),
]

# B2B standard relationships — used for food_b2b and hightech.
# In B2B Account model, contacts map to ssot__Account__dlm (not Individual).
# ssot__IndividualId__c on the engagement DMOs stores the account source record ID.
B2B_STANDARD_RELATIONSHIPS = [
    ("ssot__WebsiteEngagement__dlm", "ssot__IndividualId__c", ACCT, "ssot__Id__c", "WebsiteEngagement → Account"),
    ("ssot__EmailEngagement__dlm",   "ssot__IndividualId__c", ACCT, "ssot__Id__c", "EmailEngagement → Account"),
]

# Industry-specific relationships.
# Relationship spec: (source_dmo, source_fk, target_dmo, target_pk, label)
# All cardinality = ManyToOne (source has many rows per one target row)
INDUSTRY_RELATIONSHIPS = {
    # NOTE: EmailEngagement → Individual relationship removed from per-industry lists.
    # It now lives in STANDARD_RELATIONSHIPS (B2C) and B2B_STANDARD_RELATIONSHIPS (B2B)
    # as ssot__EmailEngagement__dlm.ssot__IndividualId__c — registered once, applies to all.
    "insurance": [
        ("InsurancePolicy__dlm", "PartyId__c",   IND,                    "ssot__Id__c", "Policy → Individual"),
        ("InsuranceClaim__dlm",  "PolicyId__c",  "InsurancePolicy__dlm", "Id__c",       "Claim → Policy"),
        ("InsuranceClaim__dlm",  "PartyId__c",   IND,                    "ssot__Id__c", "Claim → Individual"),
    ],
    "food": [
        ("PurchaseOrder__dlm",     "PartyId__c",  IND,                   "ssot__Id__c", "PurchaseOrder → Individual"),
        ("OrderLine__dlm",         "OrderId__c",  "PurchaseOrder__dlm",  "Id__c",       "OrderLine → PurchaseOrder"),
        ("OrderLine__dlm",         "PartyId__c",  IND,                   "ssot__Id__c", "OrderLine → Individual"),
        ("LoyaltyTransaction__dlm","PartyId__c",  IND,                   "ssot__Id__c", "LoyaltyTransaction → Individual"),
    ],
    "retail": [
        ("SalesOrder__dlm",        "PartyId__c",  IND,              "ssot__Id__c", "SalesOrder → Individual"),
        ("OrderLine__dlm",         "OrderId__c",  "SalesOrder__dlm","Id__c",       "OrderLine → SalesOrder"),
        ("OrderLine__dlm",         "PartyId__c",  IND,              "ssot__Id__c", "OrderLine → Individual"),
        ("LoyaltyTransaction__dlm","PartyId__c",  IND,              "ssot__Id__c", "LoyaltyTransaction → Individual"),
    ],
    "banking": [
        ("FinancialAccount__dlm",  "PartyId__c",   IND,                    "ssot__Id__c", "FinancialAccount → Individual"),
        ("Transaction__dlm",       "AccountId__c", "FinancialAccount__dlm","Id__c",       "Transaction → FinancialAccount"),
        ("Transaction__dlm",       "PartyId__c",   IND,                    "ssot__Id__c", "Transaction → Individual"),
        ("BankingProduct__dlm",    "PartyId__c",   IND,                    "ssot__Id__c", "BankingProduct → Individual"),
        ("LoyaltyTransaction__dlm","PartyId__c",   IND,                    "ssot__Id__c", "LoyaltyTransaction → Individual"),
    ],
    "pharma": [
        ("Prescription__dlm", "PartyId__c", IND, "ssot__Id__c", "Prescription → Individual"),
    ],
    "telco": [
        ("ServiceContract__dlm", "PartyId__c",   IND,                    "ssot__Id__c", "ServiceContract → Individual"),
        ("UsageRecord__dlm",     "ContractId__c","ServiceContract__dlm", "Id__c",       "UsageRecord → ServiceContract"),
        ("UsageRecord__dlm",     "PartyId__c",   IND,                    "ssot__Id__c", "UsageRecord → Individual"),
    ],
    "utilities": [
        ("UtilityContract__dlm",  "PartyId__c",   IND,                   "ssot__Id__c", "UtilityContract → Individual"),
        ("ConsumptionRecord__dlm","ContractId__c","UtilityContract__dlm","Id__c",       "ConsumptionRecord → UtilityContract"),
        ("ConsumptionRecord__dlm","PartyId__c",   IND,                   "ssot__Id__c", "ConsumptionRecord → Individual"),
    ],
    "airlines": [
        ("FlightBooking__dlm",    "PartyId__c", IND, "ssot__Id__c", "FlightBooking → Individual"),
        ("LoyaltyTransaction__dlm","PartyId__c", IND, "ssot__Id__c", "LoyaltyTransaction → Individual"),
    ],
    # B2B Account model — parent is ssot__Account__dlm, NOT ssot__Individual__dlm
    "food_b2b": [
        ("WholesaleOrder__dlm",     "PartyId__c", ACCT,                  "ssot__Id__c", "WholesaleOrder → Account"),
        ("WholesaleOrderLine__dlm", "OrderId__c", "WholesaleOrder__dlm", "Id__c",       "WholesaleOrderLine → WholesaleOrder"),
        ("WholesaleOrderLine__dlm", "PartyId__c", ACCT,                  "ssot__Id__c", "WholesaleOrderLine → Account"),
        ("LoyaltyTransaction__dlm", "PartyId__c", ACCT,                  "ssot__Id__c", "LoyaltyTransaction → Account"),
    ],
    # B2B Account model — parent is ssot__Account__dlm, NOT ssot__Individual__dlm
    "hightech": [
        ("HtSubscription__dlm",  "PartyId__c",       ACCT,                  "ssot__Id__c", "HtSubscription → Account"),
        ("HtUsageRecord__dlm",   "SubscriptionId__c","HtSubscription__dlm", "Id__c",       "HtUsageRecord → HtSubscription"),
        ("HtUsageRecord__dlm",   "PartyId__c",       ACCT,                  "ssot__Id__c", "HtUsageRecord → Account"),
        ("HtSupportTicket__dlm", "PartyId__c",       ACCT,                  "ssot__Id__c", "HtSupportTicket → Account"),
    ],
    "healthcare": [
        ("MedicalVisit__dlm", "PartyId__c", IND, "ssot__Id__c", "MedicalVisit → Individual"),
        ("LabResult__dlm",    "PartyId__c", IND, "ssot__Id__c", "LabResult → Individual"),
    ],
    "sports_club": [
        ("Membership__dlm",    "PartyId__c", IND, "ssot__Id__c", "Membership → Individual"),
        ("ActivityRecord__dlm","PartyId__c", IND, "ssot__Id__c", "ActivityRecord → Individual"),
    ],
    "ecommerce": [
        ("EcomOrder__dlm",       "PartyId__c", IND, "ssot__Id__c", "EcomOrder → Individual"),
        ("EcomOrderLine__dlm",   "PartyId__c", IND, "ssot__Id__c", "EcomOrderLine → Individual"),
        ("CartAbandonment__dlm", "PartyId__c", IND, "ssot__Id__c", "CartAbandonment → Individual"),
    ],
    "hospitality": [
        ("HotelStay__dlm",          "PartyId__c", IND, "ssot__Id__c", "HotelStay → Individual"),
        ("LoyaltyTransaction__dlm", "PartyId__c", IND, "ssot__Id__c", "LoyaltyTransaction → Individual"),
    ],
    "media": [
        ("Subscription__dlm", "PartyId__c", IND, "ssot__Id__c", "Subscription → Individual"),
        ("ContentView__dlm",  "PartyId__c", IND, "ssot__Id__c", "ContentView → Individual"),
    ],
    "automotive": [
        ("Vehicle__dlm",       "PartyId__c", IND, "ssot__Id__c", "Vehicle → Individual"),
        ("ServiceRecord__dlm", "PartyId__c", IND, "ssot__Id__c", "ServiceRecord → Individual"),
    ],
    "real_estate": [
        ("PropertyInquiry__dlm",     "PartyId__c", IND, "ssot__Id__c", "PropertyInquiry → Individual"),
        ("PropertyTransaction__dlm", "PartyId__c", IND, "ssot__Id__c", "PropertyTransaction → Individual"),
    ],
    "betting": [
        ("BettingAccount__dlm",     "PartyId__c", IND, "ssot__Id__c", "BettingAccount → Individual"),
        ("BettingTransaction__dlm", "PartyId__c", IND, "ssot__Id__c", "BettingTransaction → Individual"),
    ],
    "postal": [
        ("Parcel__dlm",        "PartyId__c", IND, "ssot__Id__c", "Parcel → Individual"),
        ("PostalProduct__dlm", "PartyId__c", IND, "ssot__Id__c", "PostalProduct → Individual"),
    ],
}


def rel_metadata_xml(label: str, src_field: str, tgt_field: str) -> str:
    """Generate FieldSrcTrgtRelationship metadata XML.

    src_field = "{child_dmo}.{child_fk}"  e.g. "InsurancePolicyV2__dlm.PartyId__c"
    tgt_field = "{parent_dmo}.{parent_pk}" e.g. "ssot__Individual__dlm.ssot__Id__c"
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<FieldSrcTrgtRelationship xmlns="http://soap.sforce.com/2006/04/metadata" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        '  <definitionCreationType>Custom</definitionCreationType>\n'
        f'  <masterLabel>{label}</masterLabel>\n'
        '  <owner>DataCloud</owner>\n'
        '  <relationshipCardinality>ManyToOne</relationshipCardinality>\n'
        f'  <sourceFieldName>{src_field}</sourceFieldName>\n'
        '  <targetEntity xsi:nil="true"/>\n'
        f'  <targetFieldName>{tgt_field}</targetFieldName>\n'
        '</FieldSrcTrgtRelationship>\n'
    )


def existing_joins_for_dmo(core_url: str, token: str, child_dmo: str) -> set:
    """Return set of (child_dmo, child_field, parent_dmo) for all existing joins on this DMO.

    Uses GET /ssot/data-model-objects/{child}/relationships — returns ANY status
    so we can avoid deploying a duplicate that would land INACTIVE and never activate.

    Response shape (proven 2026-06-24):
      sourceObject.name = child DMO API name
      sourceField.name  = child FK field
      targetObject.name = parent DMO API name
    """
    st, resp = api(core_url, token, "GET",
                   f"{BASE}/data-model-objects/{child_dmo}/relationships")
    if st != 200:
        return set()
    relist = resp if isinstance(resp, list) else (
        resp.get("relationships") or resp.get("data") or []
    )
    result = set()
    for r in relist:
        src_obj = (r.get("sourceObject") or {}).get("name", "")
        src_fld = (r.get("sourceField") or {}).get("name", "")
        tgt_obj = (r.get("targetObject") or {}).get("name", "")
        if src_obj and src_fld and tgt_obj:
            result.add((src_obj, src_fld, tgt_obj))
    return result


def deploy_relationship(org_alias: str, dev_name: str, label: str,
                        src_field: str, tgt_field: str) -> bool:
    """Deploy one FieldSrcTrgtRelationship via sf project deploy start.

    Returns True if the deploy succeeded (or the relationship already exists).
    """
    tmp = Path(tempfile.mkdtemp(prefix="d360-dmorel-"))
    fa = tmp / "force-app" / "main" / "default" / "fieldSrcTrgtRelationships"
    fa.mkdir(parents=True, exist_ok=True)

    (tmp / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app", "default": True}],
        "namespace": "",
        "sourceApiVersion": "62.0",
        "sfdcLoginUrl": "https://login.salesforce.com",
    }))

    xml_file = fa / f"{dev_name}.fieldSrcTrgtRelationship-meta.xml"
    xml_file.write_text(rel_metadata_xml(label, src_field, tgt_field))

    import os
    _env = {**os.environ, "SFDX_DISABLE_DNS_CHECK": "true"}
    cmd = [
        "sf", "project", "deploy", "start",
        "--source-dir", "force-app",
        "--target-org", org_alias,
        "--wait", "10",
        "--json",
    ]
    try:
        p = subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True, timeout=700, env=_env)
        try:
            out = json.loads(p.stdout)
            success = (out.get("result", {}) or {}).get("success")
        except Exception:
            out, success = {}, (p.returncode == 0)

        if success:
            return True

        det = (out.get("result", {}) or {}).get("details", {}) or {}
        failures = det.get("componentFailures") or []
        problems = "; ".join(c.get("problem", "") for c in failures)

        # If it already exists, treat as success
        if "already exists" in problems.lower() or "duplicate" in problems.lower():
            return True

        print(f"    Deploy failed: {problems[:150] or p.stderr[:150]}")
        return False

    except subprocess.TimeoutExpired:
        print(f"    Deploy timed out after 700s")
        return False
    except Exception as ex:
        print(f"    Deploy error: {ex}")
        return False
    finally:
        # Clean up temp dir
        import shutil
        shutil.rmtree(str(tmp), ignore_errors=True)


def make_dev_name(prefix: str, child_dmo: str, child_fk: str) -> str:
    """Generate a deterministic developer name ≤80 chars."""
    child = child_dmo.replace("ssot__", "").replace("__dlm", "")
    fk = child_fk.replace("ssot__", "").replace("__c", "")
    raw = f"{prefix}_{child}_{fk}"
    # Remove double underscores, truncate to 80
    raw = re.sub(r"_+", "_", raw).strip("_")[:80]
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    slug = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()
    prefix = slug.replace("-", "_").title().replace("_", "")  # e.g. Migdal

    # B2B Account model: food_b2b and hightech map contacts to ssot__Account__dlm
    b2b_account = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")

    print(f"\n🔗  Creating DMO relationships for {cfg.get('clientName', alias)} ({industry})")
    if b2b_account:
        print(f"    Model: B2B Account (parent = ssot__Account__dlm)")
    print(f"    Org: {alias}\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}\n")

    std_rels = B2B_STANDARD_RELATIONSHIPS if b2b_account else STANDARD_RELATIONSHIPS
    rels = std_rels + INDUSTRY_RELATIONSHIPS.get(industry, [])

    # Pre-check existing joins per child DMO (avoid deploying INACTIVE duplicates)
    child_dmos = list({src_dmo for src_dmo, *_ in rels})
    all_existing = set()
    for child in child_dmos:
        all_existing |= existing_joins_for_dmo(core_url, core_token, child)

    results = []
    for src_dmo, src_fk, tgt_dmo, tgt_pk, label in rels:
        key = (src_dmo, src_fk, tgt_dmo)
        if key in all_existing:
            print(f"  ↩  {label}  (already exists — skipping to avoid INACTIVE duplicate)")
            results.append({"rel": label, "status": "existing"})
            continue

        print(f"  →  {label}  ({src_dmo}.{src_fk} → {tgt_dmo}) ...", end=" ", flush=True)

        if args.dry_run:
            print("[dry-run]")
            results.append({"rel": label, "status": "dry-run"})
            continue

        dev_name = make_dev_name(prefix, src_dmo, src_fk)
        src_field = f"{src_dmo}.{src_fk}"
        tgt_field = f"{tgt_dmo}.{tgt_pk}"

        ok = deploy_relationship(alias, dev_name, label, src_field, tgt_field)
        if ok:
            print("✓")
            results.append({"rel": label, "status": "created"})
        else:
            print("✗")
            results.append({"rel": label, "status": "error"})

        time.sleep(0.5)

    created = sum(1 for r in results if r["status"] in ("created", "existing"))
    print(f"\n✅  {created}/{len(results)} relationships OK")
    if any(r["status"] == "error" for r in results):
        print("  ⚠️  Some relationships failed — custom DMOs may not exist yet.")
        print("     Create them first (via Metadata API) then re-run this script.")

    print("""
  These relationships enable:
  • CI queries that JOIN across DMOs (e.g. count claims per policy)
  • Data Graph traversal for the Contact 360 page
  • Segment criteria on related objects
""")

    # Persist
    out_dir = Path(cfg.get("outputDir", f"data/{slug}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "relationship_results.json").write_text(
        json.dumps(results, indent=2)
    )


if __name__ == "__main__":
    main()
