#!/usr/bin/env python3
"""
Poll Identity Resolution until the job completes successfully.

Usage:
    python3 verify_ir.py --config config.json
    python3 verify_ir.py --config config.json --timeout 2400   # wait up to 40 min
    python3 verify_ir.py --config config.json --trigger        # trigger run-now first

Polls every 30 seconds until:
  - lastJobStatus == SUCCESS  (or equivalent)
  - totalUnifiedProfiles > 0

B2B support: for food_b2b and hightech ("b2b": true in config) looks for
a ruleset with configurationType="account" and reports UnifiedssotAccountRt__dlm.
All other industries use configurationType="individual" → UnifiedssotIndividualRt__dlm.

Exit codes:
  0 — IR completed, unified profiles > 0
  1 — timeout, IR failed, or no ruleset found

IMPORTANT: Do NOT proceed to Step 6e (create CIs) until this returns 0.
The unified link table (UnifiedLinkssotIndividualRt__dlm or UnifiedLinkssotAccountRt__dlm)
is only populated after IR completes — CIs run before IR produce 0 rows.
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

# IR jobs take 15–40 min — default 40 min timeout
DEFAULT_TIMEOUT_S = 2400
POLL_INTERVAL_S   = 30

# Map configurationType → unified DMO name (for display only)
UNIFIED_DMO = {
    "individual": "UnifiedssotIndividualRt__dlm",
    "account":    "UnifiedssotAccountRt__dlm",
}


def list_rulesets(core_url: str, token: str) -> list:
    """Return all IR rulesets from the org."""
    status, data = api(core_url, token, "GET",
                       f"{BASE}/identity-resolutions?dataspace=default")
    if status != 200 or not isinstance(data, dict):
        return []
    return data.get("identityResolutions", [])


def get_ruleset(core_url: str, token: str, rs_id: str) -> dict:
    """Fetch a single ruleset by ID for up-to-date status fields."""
    status, data = api(core_url, token, "GET",
                       f"{BASE}/identity-resolutions/{rs_id}?dataspace=default")
    if status == 200 and isinstance(data, dict):
        return data
    return {}


def trigger_run_now(core_url: str, token: str, rs_id: str) -> tuple:
    """Trigger a run-now on the given ruleset. Returns (ok, note)."""
    for attempt in range(4):
        run_st, run_resp = api(core_url, token, "POST",
                               f"{BASE}/identity-resolutions/{rs_id}/actions/run-now",
                               body={})
        rc = (run_resp.get("resultCode") if isinstance(run_resp, dict) else None) or ""
        if run_st in (200, 201, 202) or "AlreadyRunning" in str(rc):
            return True, rc or str(run_st)
        if attempt < 3:
            time.sleep(10)
    return False, f"HTTP {run_st} {str(run_resp)[:80]}"


def _parse_job_status(rs: dict) -> tuple[str, int]:
    """
    Returns (job_status_upper, unified_profiles).
    Handles the many field names Data Cloud uses across API versions.
    """
    job_status = (
        rs.get("lastJobStatus")
        or rs.get("jobStatus")
        or rs.get("status")
        or ""
    ).upper()

    ruleset_status = (rs.get("rulesetStatus") or "").upper()

    # Treat PUBLISHING as still running
    if ruleset_status in ("PUBLISHING",) and not job_status:
        job_status = "RUNNING"

    profiles = 0
    try:
        profiles = int(rs.get("totalUnifiedProfiles") or rs.get("unifiedProfiles") or 0)
    except (TypeError, ValueError):
        profiles = 0

    return job_status, profiles


def _status_icon(job_status: str, profiles: int) -> str:
    if job_status in ("SUCCESS", "COMPLETED", "COMPLETE") and profiles > 0:
        return "✅"
    if job_status in ("SUCCESS", "COMPLETED", "COMPLETE") and profiles == 0:
        return "⚠️ "
    if job_status in ("RUNNING", "IN_PROGRESS", "PROCESSING", "PUBLISHING"):
        return "🔄"
    if job_status in ("FAILED", "ERROR", "FAILURE"):
        return "❌"
    if job_status in ("", "NONE", "NOT_STARTED", "NEW"):
        return "⏳"
    return "❓"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json",
                    help="Path to config JSON (default: config.json)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help=f"Max seconds to wait (default: {DEFAULT_TIMEOUT_S})")
    ap.add_argument("--trigger", action="store_true",
                    help="Trigger run-now before polling")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias    = cfg["orgAlias"]
    slug     = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()
    b2b      = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")
    ir_type  = "account" if b2b else "individual"
    unified_dmo = UNIFIED_DMO[ir_type]

    print(f"\n⏳  Waiting for Identity Resolution — {cfg.get('clientName', slug)}")
    print(f"    Org:       {alias}")
    print(f"    IR model:  {ir_type}  →  {unified_dmo}")
    print(f"    Timeout:   {args.timeout}s (polling every {POLL_INTERVAL_S}s)\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}\n")

    # Find the matching ruleset
    all_rulesets = list_rulesets(core_url, core_token)
    matching = [
        r for r in all_rulesets
        if (r.get("configurationType") or "individual").lower() == ir_type
    ]

    if not matching:
        print(f"  ❌  No IR ruleset found with configurationType='{ir_type}'.")
        if all_rulesets:
            wrong = [(r.get("label", "?"), r.get("configurationType", "?"))
                     for r in all_rulesets]
            print(f"     Found rulesets with wrong type: {wrong}")
            print(f"     Run setup_ir.py first to create the correct ruleset.")
        else:
            print(f"     No IR rulesets exist at all. Run setup_ir.py first.")
        sys.exit(1)

    # Pick the most relevant ruleset (prefer PUBLISHED/ACTIVE)
    rs = next(
        (r for r in matching
         if (r.get("rulesetStatus") or "").upper() in ("PUBLISHED", "PUBLISHING")),
        matching[0],
    )
    rs_id    = rs.get("id", "")
    rs_label = rs.get("label", "?")
    print(f"  ↩  Ruleset:  '{rs_label}'  (id={rs_id})")
    print(f"     Status:   {(rs.get('rulesetStatus') or 'UNKNOWN').upper()}\n")

    # Optionally trigger run-now first
    if args.trigger:
        print("  ▶  Triggering run-now...", end=" ", flush=True)
        ok, note = trigger_run_now(core_url, core_token, rs_id)
        print(f"{'✓' if ok else '⚠️'}  ({note})")
        print()
        time.sleep(10)  # brief pause before first poll

    start_t    = time.time()
    last_print = 0.0

    while True:
        elapsed = time.time() - start_t
        now_str = time.strftime("%H:%M:%S")

        # Fetch fresh status
        fresh = get_ruleset(core_url, core_token, rs_id)
        if not fresh:
            # Fallback: re-list
            all_rs = list_rulesets(core_url, core_token)
            fresh  = next((r for r in all_rs if r.get("id") == rs_id), rs)

        job_status, profiles = _parse_job_status(fresh)
        icon = _status_icon(job_status, profiles)

        # Print status (throttled)
        if time.time() - last_print >= POLL_INTERVAL_S - 2 or elapsed < 5:
            print(f"  [{now_str}]  elapsed={int(elapsed)}s")
            profiles_str = f"{profiles:,}" if profiles > 0 else "—"
            print(f"    {icon}  lastJobStatus={job_status:<15}  "
                  f"unifiedProfiles={profiles_str}")
            print()
            last_print = time.time()

        # ── SUCCESS ──────────────────────────────────────────────────────────
        is_done = job_status in ("SUCCESS", "COMPLETED", "COMPLETE")
        if is_done and profiles > 0:
            print(f"  ✅  Identity Resolution COMPLETE")
            print(f"     Unified profiles: {profiles:,}")
            print(f"     Total wait: {int(elapsed)}s\n")
            print(f"  ✅  GATE PASSED — safe to proceed to Step 6e (create CIs)")
            print(f"     {unified_dmo} is populated.\n")

            out_dir = Path(cfg.get("outputDir", f"data/{slug}"))
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "ir_status.json").write_text(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_s": int(elapsed),
                "rulesetId": rs_id,
                "label": rs_label,
                "configurationType": ir_type,
                "lastJobStatus": job_status,
                "totalUnifiedProfiles": profiles,
                "unifiedDmo": unified_dmo,
            }, indent=2))
            sys.exit(0)

        if is_done and profiles == 0:
            # Completed but no profiles — might be a data issue, but don't block indefinitely
            print(f"  ⚠️  IR job finished but totalUnifiedProfiles=0.")
            print(f"     This usually means the mapped source DMO had no rows when IR ran.")
            print(f"     Check: were all streams ingested before setup_ir.py was run?")
            print(f"     Try running setup_ir.py again (it will trigger run-now on the existing ruleset).\n")
            sys.exit(1)

        # ── FAILED ───────────────────────────────────────────────────────────
        if job_status in ("FAILED", "ERROR", "FAILURE"):
            print(f"  ❌  Identity Resolution job FAILED.")
            print(f"     Check: Data Cloud Setup → Identity Resolution → '{rs_label}' → Run History")
            print(f"     Common causes:")
            print(f"       • Source DMO has no rows (run verify_ingestion.py first)")
            print(f"       • Mapping not created for the required DMO")
            print(f"       • Ruleset was created before data was ingested (re-trigger: python3 setup_ir.py --config {args.config})\n")
            sys.exit(1)

        # ── TIMEOUT ──────────────────────────────────────────────────────────
        if elapsed >= args.timeout:
            print(f"  ⏰  Timeout after {args.timeout}s. IR is still running.")
            print(f"     Current status: {job_status}  profiles: {profiles}")
            print(f"     IR jobs typically take 15–40 minutes depending on volume.")
            print(f"     Options:")
            print(f"       1. Re-run with a longer timeout:  --timeout 3600")
            print(f"       2. Check progress in UI: Data Cloud Setup → Identity Resolution → Run History")
            print(f"       3. Come back and re-run when the job shows Completed in the UI\n")
            sys.exit(1)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
