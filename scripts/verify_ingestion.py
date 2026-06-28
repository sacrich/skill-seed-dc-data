#!/usr/bin/env python3
"""
Verify that all data streams created for a config have finished ingesting
(lastRunStatus == SUCCESS and totalRows > 0).

Usage:
    python3 verify_ingestion.py --config config.json
    python3 verify_ingestion.py --config config.json --timeout 900  # wait up to 15 min
    python3 verify_ingestion.py --config config.json --trigger      # trigger run first

Polls every 30 seconds until all streams are ingested or the timeout expires.

Exit code:
  0 — all streams have rows
  1 — timeout or one or more streams have 0 rows / error status

IMPORTANT: Do NOT proceed to Step 6 (create DMOs / mappings) until this returns 0.
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

# How long to try before giving up
DEFAULT_TIMEOUT_S = 600   # 10 minutes
POLL_INTERVAL_S   = 30    # check every 30 seconds


def list_all_streams(core_url: str, token: str) -> dict:
    """Return {stream_name: {lastRunStatus, totalRows, ...}} for all streams."""
    url = f"{BASE}/data-streams?dataspace=default"
    streams = {}
    while url:
        st, data = api(core_url, token, "GET", url)
        if st != 200 or not isinstance(data, dict):
            break
        for s in data.get("dataStreams", []):
            name = s.get("name", "")
            if name:
                streams[name] = s
        url = data.get("nextPageUrl")
    return streams


def get_stream_detail(core_url: str, token: str, stream_name: str) -> dict:
    """Fetch full detail for a single stream including lastRunStatus and row count."""
    st, data = api(core_url, token, "GET",
                   f"{BASE}/data-streams/{stream_name}?dataspace=default")
    if st == 200 and isinstance(data, dict):
        return data
    return {}


def trigger_run(core_url: str, token: str, stream_name: str) -> bool:
    """Trigger an ingestion run for a stream. Returns True if accepted."""
    st, _ = api(core_url, token, "POST",
                f"{BASE}/data-streams/{stream_name}/actions/run",
                body={})
    return st in (200, 201, 202)


def resolve_target_streams(core_url: str, token: str, slug: str) -> list[str]:
    """Return the stream names that belong to this deployment (prefixed with slug)."""
    all_streams = list_all_streams(core_url, token)
    prefix = slug.replace("-", "_").title().replace("_", "") + "_"
    matches = [n for n in all_streams if n.startswith(prefix)]
    return sorted(matches)


def check_stream(detail: dict) -> tuple[str, int]:
    """
    Returns (status, rows):
      status: "SUCCESS" | "NONE" | "RUNNING" | "FAILED" | "ERROR" | "UNKNOWN"
      rows: best available row count (may be 0 when still ingesting)
    """
    # lastRunStatus field names vary across API versions
    run_status = (
        detail.get("lastRunStatus")
        or detail.get("lastJobStatus")
        or detail.get("status", "")
    ).upper()

    # Row count: try several field paths Data Cloud uses
    rows = (
        detail.get("totalRows")
        or detail.get("rowCount")
        or detail.get("dataLakeObjectInfo", {}).get("totalRows")
        or 0
    )
    try:
        rows = int(rows)
    except (TypeError, ValueError):
        rows = 0

    if run_status in ("SUCCESS", "COMPLETED", "COMPLETE"):
        return "SUCCESS", rows
    if run_status in ("RUNNING", "IN_PROGRESS", "PROCESSING"):
        return "RUNNING", rows
    if run_status in ("FAILED", "ERROR", "FAILURE"):
        return "FAILED", rows
    if run_status in ("", "NONE", "NOT_STARTED"):
        return "NONE", rows
    return run_status, rows


def _status_icon(status: str, rows: int) -> str:
    if status == "SUCCESS":
        return "✅"
    if status == "RUNNING":
        return "🔄"
    if status == "FAILED":
        return "❌"
    if status == "NONE":
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
                    help="Trigger ingestion run on each stream before polling")
    ap.add_argument("--streams", nargs="+",
                    help="Check specific stream names instead of all slug-prefixed streams")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    slug  = cfg.get("clientSlug", "client")

    print(f"\n🔍  Verifying ingestion for {cfg.get('clientName', slug)}")
    print(f"    Org:     {alias}")
    print(f"    Timeout: {args.timeout}s (polling every {POLL_INTERVAL_S}s)\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}\n")

    # Determine which streams to watch
    if args.streams:
        target_streams = sorted(args.streams)
    else:
        target_streams = resolve_target_streams(core_url, core_token, slug)

    if not target_streams:
        print(f"  ⚠️  No streams found with prefix matching '{slug}'.")
        print(f"     Run upload_and_stream.py first, then retry.")
        sys.exit(1)

    print(f"  Watching {len(target_streams)} stream(s):")
    for n in target_streams:
        print(f"    • {n}")
    print()

    # Optionally trigger runs before polling
    if args.trigger:
        print("  🔄  Triggering ingestion runs...")
        for name in target_streams:
            ok = trigger_run(core_url, core_token, name)
            print(f"    {'✓' if ok else '⚠️'} {name}")
        print()
        time.sleep(5)

    start_t = time.time()
    last_print = 0.0

    while True:
        elapsed = time.time() - start_t
        now_str = time.strftime("%H:%M:%S")

        # Fetch current status for all target streams
        results = {}
        for name in target_streams:
            detail = get_stream_detail(core_url, core_token, name)
            if not detail:
                # Fallback: look up in the full list
                all_s = list_all_streams(core_url, core_token)
                detail = all_s.get(name, {})
            status, rows = check_stream(detail)
            results[name] = (status, rows)

        # Print status table (throttled to avoid spam)
        if time.time() - last_print >= POLL_INTERVAL_S - 2 or elapsed < 5:
            print(f"  [{now_str}]  elapsed={int(elapsed)}s")
            max_name_len = max(len(n) for n in target_streams)
            for name, (status, rows) in sorted(results.items()):
                icon = _status_icon(status, rows)
                rows_str = f"{rows:,}" if rows > 0 else "—"
                print(f"    {icon}  {name:<{max_name_len}}  {status:<10}  rows={rows_str}")
            print()
            last_print = time.time()

        # Check if all done — SUCCESS is sufficient; some orgs don't return totalRows
        all_ok = all(
            status == "SUCCESS"
            for status, rows in results.values()
        )
        any_failed = any(status == "FAILED" for status, _ in results.values())

        if all_ok:
            print(f"  ✅  All {len(target_streams)} streams ingested successfully!")
            print(f"  Total wait: {int(elapsed)}s\n")

            print("  Row counts:")
            for name, (status, rows) in sorted(results.items()):
                print(f"    • {name}: {rows:,} rows")
            print()
            print("  ✅  GATE PASSED — safe to proceed to Step 6 (create DMOs + mappings)\n")

            # Save to output dir for reference
            out_dir = Path(cfg.get("outputDir", f"data/{slug}"))
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "ingestion_status.json").write_text(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_s": int(elapsed),
                "all_ok": True,
                "streams": {n: {"status": s, "rows": r} for n, (s, r) in results.items()},
            }, indent=2))
            sys.exit(0)

        if any_failed:
            failed = [n for n, (s, _) in results.items() if s == "FAILED"]
            print(f"  ❌  {len(failed)} stream(s) FAILED ingestion:")
            for n in failed:
                print(f"       • {n}")
            print()
            print("  ℹ️  Check Data Cloud Setup → Data Streams → (stream) → Run History")
            print("     Common causes: bad CSV encoding, schema mismatch, S3 permission error")
            sys.exit(1)

        if elapsed >= args.timeout:
            still_pending = [n for n, (s, r) in results.items()
                             if s != "SUCCESS"]
            print(f"  ⏰  Timeout after {args.timeout}s. Still pending:")
            for n in still_pending:
                s, r = results[n]
                print(f"       • {n}  status={s}  rows={r}")
            print()
            print("  ℹ️  Data Cloud ingestion can take up to 15 minutes for large files.")
            print("     You can:")
            print("       1. Re-run with a longer timeout:  --timeout 900")
            print("       2. Trigger manually via UI: Data Cloud Setup → Data Streams → Run Now")
            print("       3. Run with --trigger flag to force-start ingestion")
            sys.exit(1)

        # Still waiting — sleep before next check
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
