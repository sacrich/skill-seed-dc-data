#!/usr/bin/env python3
"""
Create the Identity Resolution ruleset — Normalized Email + Exact Name.

Usage:
    python3 setup_ir.py --config config.json

Creates:
  - Match rules:   Fuzzy Name + Normalized Email (primary), Exact Email (secondary)
  - Recon rules:   Individual + ContactPointEmail + ContactPointPhone + ContactPointAddress
                   (baked at create — you CANNOT late-add to a published ruleset)
  - Starts the IR job (~15-40 min on Storm)

Proven endpoint (2026-06-24):
  POST /services/data/v62.0/ssot/identity-resolutions?dataspace=default
  (NOT identity-resolution-rulesets — 404)

Idempotent: reuses any existing individual-config ruleset rather than creating a 2nd
one (re-creating triggers a full re-matching job that blanks UnifiedIndividual transiently).

Auto-drop: if an entity is rejected as "can't be used for identity resolution" the script
drops that entity and retries (up to 5 times).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import get_tokens, api  # noqa: E402

API_V = "v62.0"
BASE = f"/services/data/{API_V}/ssot"

RECON_ENTITIES = [
    "ssot__Individual__dlm",
    "ssot__ContactPointEmail__dlm",
    "ssot__ContactPointPhone__dlm",
    "ssot__ContactPointAddress__dlm",
]

# B2B Account-level IR entities (food_b2b, hightech)
B2B_RECON_ENTITIES = [
    "ssot__Account__dlm",
    "ssot__AccountEmailAddress__dlm",
]


def build_b2b_recon_rules(skip_entities: set = None) -> list:
    """Build reconciliation rules for Account-level IR (food_b2b / hightech).

    configurationType="account" creates UnifiedssotAccountRt__dlm.
    Reconciliation covers: Account + AccountEmailAddress.
    """
    skip = skip_entities or set()
    rules = []
    for entity in B2B_RECON_ENTITIES:
        if entity in skip:
            continue
        rules.append({
            "entityName": entity,
            "ruleType": "mostfrequent",
            "shouldIgnoreEmptyValue": True,
            "sources": [],
            "fields": [],
        })
    return rules


def post_account_ruleset(core_url: str, token: str, label: str,
                         recon_rules: list) -> tuple:
    """POST an Account-level IR ruleset.

    configurationType="account" creates UnifiedssotAccountRt__dlm (not UnifiedIndividual).
    Match rules: exact company name + normalized email, with email-only fallback.
    """
    body = {
        "label": label,
        "description": "Match B2B accounts by company name and email address. "
                        "Account and AccountEmailAddress entities reconciled.",
        "configurationType": "account",
        "doesRunAutomatically": True,
        "matchRules": [
            {
                "label": "Company Name and Email",
                "criteria": [
                    {
                        "entityName": "ssot__Account__dlm",
                        "fieldName": "ssot__Name__c",
                        "matchMethodType": "exact",
                        "shouldMatchOnBlank": False,
                    },
                    {
                        "entityName": "ssot__AccountEmailAddress__dlm",
                        "fieldName": "ssot__EmailAddress__c",
                        "matchMethodType": "exactnormalized",
                        "shouldMatchOnBlank": False,
                    },
                ],
            },
            {
                "label": "Exact Email",
                "criteria": [
                    {
                        "entityName": "ssot__AccountEmailAddress__dlm",
                        "fieldName": "ssot__EmailAddress__c",
                        "matchMethodType": "exact",
                        "shouldMatchOnBlank": False,
                    }
                ],
            },
        ],
        "reconciliationRules": recon_rules,
    }
    return api(core_url, token, "POST",
               f"{BASE}/identity-resolutions?dataspace=default", body)


def _dmo_has_source(core_url: str, token: str, dmo: str) -> bool:
    """True iff the DMO has ≥1 mapped source (precondition for IR reconciliation)."""
    status, data = api(core_url, token, "GET",
                       f"{BASE}/data-model-object-mappings"
                       f"?dmoDeveloperName={dmo}&dataspace=default")
    if status != 200 or not isinstance(data, dict):
        return False
    return len(data.get("objectSourceTargetMaps", [])) > 0


def list_rulesets(core_url: str, token: str) -> list:
    """Return all existing IR rulesets on this dataspace."""
    status, data = api(core_url, token, "GET",
                       f"{BASE}/identity-resolutions?dataspace=default")
    if status != 200 or not isinstance(data, dict):
        return []
    return data.get("identityResolutions", [])


def is_limit_error(resp) -> bool:
    """Return True if the API response indicates the org's IR ruleset limit was reached."""
    msg = json.dumps(resp).upper()
    return any(k in msg for k in (
        "MAXIMUM NUMBER", "LIMIT_EXCEEDED", "LIMIT EXCEEDED",
        "MAX_RULESETS", "MAXIMUM RULESETS", "REACHED THE MAXIMUM",
        "TOO MANY", "EXCEEDED THE LIMIT",
    ))


def print_ruleset_table(rulesets: list) -> None:
    """Print a human-readable table of all rulesets."""
    print(f"\n  {'#':<4} {'ID':<22} {'Type':<12} {'Status':<14} {'Label'}")
    print(f"  {'─'*4} {'─'*22} {'─'*12} {'─'*14} {'─'*30}")
    for i, r in enumerate(rulesets, 1):
        rid    = (r.get("id") or "")[:22]
        rtype  = (r.get("configurationType") or "individual")[:12]
        status = (r.get("rulesetStatus") or r.get("status") or "?")[:14]
        label  = (r.get("label") or "")[:40]
        print(f"  {i:<4} {rid:<22} {rtype:<12} {status:<14} {label}")
    print(f"\n  Total rulesets on this org: {len(rulesets)}")


def build_recon_rules(skip_entities: set = None) -> list:
    """Build reconciliation rules, skipping entities the org rejects.

    GOTCHAS (proven reference 2026-06-24):
      - Do NOT include linkDmoName / unifiedDmoName in POST body (READ-ONLY, causes JSON_PARSER_ERROR)
      - ruleType is lowercase "mostfrequent"
      - sources and fields arrays are REQUIRED (empty ok)
    """
    skip = skip_entities or set()
    rules = []
    for entity in RECON_ENTITIES:
        if entity in skip:
            continue
        rules.append({
            "entityName": entity,
            "ruleType": "mostfrequent",
            "shouldIgnoreEmptyValue": True,
            "sources": [],
            "fields": [],
        })
    return rules


def post_ruleset(core_url: str, token: str, label: str,
                 recon_rules: list) -> tuple:
    """POST a new IR ruleset.

    Proven body format (from reference skill create-identity-resolution.py):
      - endpoint: POST /ssot/identity-resolutions?dataspace=default
      - top-level fields: label, description, configurationType, doesRunAutomatically
      - matchRules: label + criteria[] with matchMethodType
      - reconciliationRules: entityName, ruleType, shouldIgnoreEmptyValue, sources, fields
      - NO developerName, NO dataSpace in body
    """
    body = {
        "label": label,
        "description": "Match individuals across sources by fuzzy name + normalized email. "
                        "All ContactPoint entities reconciled.",
        "configurationType": "individual",
        "doesRunAutomatically": True,
        "matchRules": [
            {
                "label": "Fuzzy Name and Normalized Email",
                "criteria": [
                    {
                        "entityName": "ssot__Individual__dlm",
                        "fieldName": "ssot__FirstName__c",
                        "matchMethodType": "fuzzy",
                        "shouldMatchOnBlank": False,
                    },
                    {
                        "entityName": "ssot__Individual__dlm",
                        "fieldName": "ssot__LastName__c",
                        "matchMethodType": "exact",
                        "shouldMatchOnBlank": False,
                    },
                    {
                        "entityName": "ssot__ContactPointEmail__dlm",
                        "fieldName": "ssot__EmailAddress__c",
                        "matchMethodType": "exactnormalized",
                        "shouldMatchOnBlank": False,
                    },
                ],
            },
            {
                "label": "Exact Email",
                "criteria": [
                    {
                        "entityName": "ssot__ContactPointEmail__dlm",
                        "fieldName": "ssot__EmailAddress__c",
                        "matchMethodType": "exact",
                        "shouldMatchOnBlank": False,
                    }
                ],
            },
        ],
        "reconciliationRules": recon_rules,
    }
    return api(core_url, token, "POST",
               f"{BASE}/identity-resolutions?dataspace=default", body)


def trigger_run_now(core_url: str, token: str, rs_id: str) -> bool:
    """Trigger run-now on an existing ruleset. Returns True on success."""
    for attempt in range(4):
        run_st, run_resp = api(core_url, token, "POST",
                               f"{BASE}/identity-resolutions/{rs_id}/actions/run-now",
                               body={})
        rc = (run_resp.get("resultCode") if isinstance(run_resp, dict) else None) or ""
        if run_st in (200, 201, 202) or "AlreadyRunning" in str(rc):
            return True
        if attempt < 3:
            time.sleep(10)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--list-only", action="store_true",
                    help="List existing IR rulesets and exit (no create/trigger)")
    ap.add_argument("--use-id",
                    help="Reuse an existing ruleset by ID (skip creation, just trigger run-now)")
    ap.add_argument("--skip-recon-entity", action="append", default=[],
                    help="Drop a recon entity the org rejects (repeatable), "
                         "e.g. --skip-recon-entity ssot__ContactPointPhone__dlm")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    slug = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()
    client_name = cfg.get("clientName", slug)

    # B2B Account IR: food_b2b and hightech use configurationType="account"
    b2b_account = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")
    ir_config_type = "account" if b2b_account else "individual"

    # Include client name to avoid developer-name collisions when multiple demos share the org.
    ruleset_label = f"{client_name} Account Identity Resolution" if b2b_account else "Individual Identity Resolution"
    required_dmo = "ssot__Account__dlm" if b2b_account else "ssot__Individual__dlm"

    if b2b_account:
        print(f"\n🔑  Setting up B2B Account Identity Resolution for {client_name}")
        print(f"    Org:     {alias}")
        print(f"    Rules:   Exact Company Name + Normalized Email, Exact Email")
        print(f"    Type:    account  (creates UnifiedssotAccountRt__dlm)\n")
    else:
        print(f"\n🔑  Setting up Identity Resolution for {client_name}")
        print(f"    Org:     {alias}")
        print(f"    Rules:   Fuzzy Name + Normalized Email, Exact Email\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}")

    all_rulesets = list_rulesets(core_url, core_token)

    # ── --list-only: show table and exit ─────────────────────────────────────
    if args.list_only:
        print_ruleset_table(all_rulesets)
        sys.exit(0)

    # ── --use-id: reuse a specific ruleset by ID ──────────────────────────────
    if args.use_id:
        target = next((r for r in all_rulesets if r.get("id") == args.use_id), None)
        if not target:
            print(f"  ❌  Ruleset ID '{args.use_id}' not found on this org.")
            print_ruleset_table(all_rulesets)
            sys.exit(1)
        rs_id    = target.get("id")
        rs_label = target.get("label", "?")
        rs_type  = (target.get("configurationType") or "individual").lower()
        if rs_type != ir_config_type:
            print(f"  ⚠️  Ruleset '{rs_label}' is type '{rs_type}' but demo needs '{ir_config_type}'.")
            print(f"      Using it anyway — unified profiles will be {rs_type}-based.")
        print(f"  ↩  Using existing ruleset: '{rs_label}' (id={rs_id})")
        print(f"  ▶  Triggering run-now…", end=" ", flush=True)
        ok = trigger_run_now(core_url, core_token, rs_id)
        print("✓" if ok else "⚠️  (run manually from UI)")
        print(f"  ⏳  IR job started — ~15-40 min to unify profiles.")
        sys.exit(0)

    # ── Check for matching rulesets ───────────────────────────────────────────
    matching = [r for r in all_rulesets
                if (r.get("configurationType") or "individual").lower() == ir_config_type]

    if matching:
        # Show all matching rulesets — wizard already asked SE which to use;
        # in non-interactive mode, prefer PUBLISHED, else first.
        active = next(
            (r for r in matching
             if (r.get("rulesetStatus") or "").upper() in ("PUBLISHED", "PUBLISHING")),
            matching[0],
        )
        rs_id     = active.get("id", "")
        rs_status = (active.get("rulesetStatus") or active.get("status", "unknown")).upper()
        rs_label  = active.get("label", "?")

        if len(matching) > 1:
            print(f"  ℹ️  {len(matching)} existing '{ir_config_type}' rulesets found — using PUBLISHED one:")
        else:
            print(f"  ↩  Existing '{ir_config_type}' ruleset found:")
        print(f"     label='{rs_label}'  id={rs_id}  status={rs_status}")

        if rs_status in ("NEW", "DRAFT", "UNPUBLISHED") and not active.get("matchRules"):
            print(f"  ⚠️  Ruleset '{rs_label}' is in {rs_status} with no match rules.")
            print(f"      Options:")
            print(f"        A) Delete it in the UI and re-run to create a fresh one.")
            print(f"        B) Reuse it by ID:  python3 setup_ir.py --config {args.config} --use-id {rs_id}")
            print_ruleset_table(all_rulesets)
            sys.exit(3)  # exit code 3 = existing IR found, needs SE decision
        elif rs_status in ("NEW", "DRAFT", "UNPUBLISHED"):
            print(f"  ℹ️  Ruleset is in {rs_status} — publishing now...")
            pub_st, pub_resp = api(core_url, core_token, "PATCH",
                                   f"{BASE}/identity-resolutions/{rs_id}?dataspace=default",
                                   {"rulesetStatus": "PUBLISHING"})
            if pub_st in (200, 201, 202, 204):
                print(f"  ✓  Published — IR job started (~15-40 min)")
            else:
                print(f"  ⚠️  Publish failed: {pub_st} {str(pub_resp)[:120]}")
        else:
            print(f"  ▶  Triggering run-now to unify seeded data…", end=" ", flush=True)
            ok = trigger_run_now(core_url, core_token, rs_id)
            print("✓" if ok else f"⚠️  (trigger manually from UI)")
            print(f"  ⏳  IR job started — ~15-40 min to unify profiles.")

        if matching:
            sys.exit(0)

    # ── Check which DMOs have mapped sources ──────────────────────────────────
    print("  Checking DMO readiness...")
    skip_entities = set(args.skip_recon_entity)
    check_entities = B2B_RECON_ENTITIES if b2b_account else RECON_ENTITIES
    for entity in check_entities:
        has_src = _dmo_has_source(core_url, core_token, entity)
        short = entity.replace("ssot__", "").replace("__dlm", "")
        if has_src:
            print(f"    ✓  {short}")
        elif entity == required_dmo:
            print(f"    ⚠️  {short} — no mapped source yet (REQUIRED — aborting)")
            print(f"\n  Run create_mappings.py first, then wait for ingestion.")
            sys.exit(1)
        else:
            print(f"    ⚠️  {short} — no mapped source → will be skipped in recon rules")
            skip_entities.add(entity)

    print()

    # ── Create ruleset with auto-drop on INVALID_INPUT ────────────────────────
    structural_left = 5
    LOCK_BUDGET_S = 300
    LOCK_BACKOFF = [8, 12, 18, 24, 30]
    lock_attempt = 0
    lock_t0 = time.time()
    status, resp = None, None
    dropped = set(skip_entities)

    while True:
        if b2b_account:
            recon_rules = build_b2b_recon_rules(skip_entities=dropped)
        else:
            recon_rules = build_recon_rules(skip_entities=dropped)
        print(f"  →  Creating ruleset '{ruleset_label}' "
              f"({len(recon_rules)} recon entities)...", end=" ", flush=True)
        if b2b_account:
            status, resp = post_account_ruleset(core_url, core_token, ruleset_label, recon_rules)
        else:
            status, resp = post_ruleset(core_url, core_token, ruleset_label, recon_rules)

        if status in (200, 201):
            print("✓")
            break

        msg = json.dumps(resp)
        print(f"✗  ({status})")

        # CASE C: transient row-lock — retry on its own time budget
        if "UNABLE_TO_LOCK_ROW" in msg and (time.time() - lock_t0) < LOCK_BUDGET_S:
            wait = LOCK_BACKOFF[min(lock_attempt, len(LOCK_BACKOFF) - 1)]
            lock_attempt += 1
            print(f"  ℹ️  Transient row lock — retrying in {wait}s "
                  f"(attempt {lock_attempt}, {int(time.time()-lock_t0)}s/{LOCK_BUDGET_S}s)")
            time.sleep(wait)
            continue

        structural_left -= 1
        if structural_left <= 0:
            break

        # CASE B: org limit reached — surface it clearly and exit
        if is_limit_error(resp):
            print(f"\n  ❌  ORG RULESET LIMIT REACHED")
            all_rulesets = list_rulesets(core_url, core_token)
            print_ruleset_table(all_rulesets)
            print(f"\n  Cannot create a new '{ir_config_type}' ruleset — org limit reached.")
            print(f"  Options:")
            print(f"    1. Delete an existing ruleset in the UI:")
            print(f"       Data Cloud → Setup → Identity Resolution → (select) → Delete")
            print(f"       Then re-run: python3 setup_ir.py --config {args.config}")
            print(f"    2. Reuse an existing ruleset (may be wrong type):")
            print(f"       python3 setup_ir.py --config {args.config} --use-id <ID>")
            sys.exit(2)  # exit code 2 = limit error — wizard checks for this

        # CASE C: entity rejected as IR-ineligible → drop and retry
        m = re.search(r"data model object '([^']+)' can't be used for identity resolution",
                      msg, re.IGNORECASE)
        if m:
            bad = m.group(1)
            if bad in dropped or bad == required_dmo:
                print(f"  ✗  Cannot drop root entity '{bad}' — giving up.")
                break
            dropped.add(bad)
            short = bad.replace("ssot__", "").replace("__dlm", "")
            print(f"  ⚠️  '{short}' rejected as IR-ineligible — dropping and retrying")
            continue

        # Other error — don't loop blindly
        print(f"  ✗  Unexpected error: {msg[:300]}")
        break

    if status in (200, 201):
        rs_id = resp.get("id", "?")
        rs_status = resp.get("rulesetStatus") or resp.get("status", "?")
        dropped_names = sorted(e.replace("ssot__", "").replace("__dlm", "")
                               for e in dropped - set(args.skip_recon_entity))
        print(f"  ✓  Ruleset created  id={rs_id}  status={rs_status}")
        if dropped_names:
            print(f"  ℹ️  Dropped (org-ineligible): {dropped_names}")
            print(f"     Their UnifiedContactPoint tiles will be empty (cosmetic only).")

        # Trigger run-now explicitly on the new ruleset.
        # doesRunAutomatically=True queues a run, but an explicit POST /actions/run-now
        # ensures the job starts immediately rather than waiting for the next scheduler tick.
        print(f"  ▶  Triggering run-now on new ruleset…", end=" ", flush=True)
        time.sleep(5)  # brief wait for the ruleset to finish provisioning
        ok2 = trigger_run_now(core_url, core_token, rs_id)
        print("✓" if ok2 else "⚠️  (run will start automatically)")

        unified_dmo = "UnifiedssotAccountRt__dlm" if b2b_account else "UnifiedssotIndividualRt__dlm"
        print(f"\n  ⏳  Identity Resolution running — ~15-40 min to unify profiles.")
        print(f"     Check: Data Cloud Setup → Identity Resolution")
        print(f"     When status = PUBLISHED, {unified_dmo} will have rows.")
    else:
        print(f"\n  ✗  Failed to create ruleset: {status} {str(resp)[:300]}")
        print("     Ensure mappings exist and streams have finished ingesting.")
        sys.exit(1)

    # Persist result
    out_dir = Path(cfg.get("outputDir", f"data/{slug}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ir_result.json").write_text(json.dumps({
        "label": ruleset_label,
        "id": resp.get("id"),
        "status": resp.get("rulesetStatus") or resp.get("status"),
        "droppedEntities": list(dropped),
    }, indent=2))


if __name__ == "__main__":
    main()
