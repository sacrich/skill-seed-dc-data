#!/usr/bin/env python3
"""
preflight.py — Check all prerequisites before running the seed wizard.

Verifies:
  1. Python version ≥ 3.9
  2. Salesforce CLI installed and version ≥ 2.x
  3. Org authenticated (if --alias provided)
  4. Data Cloud API accessible (if --alias provided)

Usage:
    python3 preflight.py                      # checks Python + sf CLI only
    python3 preflight.py --alias demo-clalit  # full check including org + Data Cloud
    python3 preflight.py --config config-clalit.json  # read alias from config file
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def check_python() -> bool:
    v = sys.version_info
    ok = v >= (3, 9)
    label = f"{v.major}.{v.minor}.{v.micro}"
    if ok:
        print(f"  ✅  Python {label}")
    else:
        print(f"  ❌  Python {label} — need ≥ 3.9")
        print(f"      Download: https://www.python.org/downloads/")
    return ok


def check_sf_cli() -> bool:
    try:
        result = subprocess.run(
            ["sf", "--version"],
            capture_output=True, text=True, timeout=15
        )
        output = (result.stdout or result.stderr or "").strip().split("\n")[0]
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if m:
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
            ok = major >= 2
            label = f"{major}.{minor}.{patch}"
            if ok:
                print(f"  ✅  Salesforce CLI {label}")
            else:
                print(f"  ❌  Salesforce CLI {label} — need ≥ 2.x")
                print(f"      Run: npm install -g @salesforce/cli")
            return ok
        # Version string found but couldn't parse — warn and continue
        print(f"  ⚠️  Salesforce CLI found (version unclear: {output[:60]})")
        return True
    except FileNotFoundError:
        print("  ❌  Salesforce CLI not found")
        print("      Run: npm install -g @salesforce/cli")
        return False
    except subprocess.TimeoutExpired:
        print("  ⚠️  Salesforce CLI check timed out — may still work")
        return True
    except Exception as e:
        print(f"  ⚠️  Could not check sf CLI: {e}")
        return False


def check_org(alias: str) -> tuple:
    """Returns (instance_url, access_token) or (None, None) on failure."""
    try:
        env = {**__import__("os").environ, "SFDX_DISABLE_DNS_CHECK": "true"}
        result = subprocess.run(
            ["sf", "org", "display", "--target-org", alias, "--json"],
            capture_output=True, text=True, timeout=30, env=env
        )
        data = json.loads(result.stdout or "{}")
        if data.get("status") == 0:
            r = data.get("result", {})
            url = r.get("instanceUrl", "").rstrip("/")
            username = r.get("username", "")
            token = r.get("accessToken", "")

            # sf CLI ≥2.136 may REDACT the access token
            if not token or "REDACTED" in token.upper():
                r2 = subprocess.run(
                    ["sf", "org", "auth", "show-access-token", "-o", alias, "--json"],
                    capture_output=True, text=True, timeout=15, env=env
                )
                d2 = json.loads(r2.stdout or "{}")
                res2 = d2.get("result", {})
                token = res2.get("accessToken", "") if isinstance(res2, dict) else str(res2)

            print(f"  ✅  Org '{alias}': {username}")
            print(f"      Instance: {url}")
            return url, token
        else:
            msg = data.get("message") or data.get("name") or "unknown error"
            print(f"  ❌  Org '{alias}' auth failed: {msg}")
            print(f"      Run: sf org login web --alias {alias}")
            return None, None
    except json.JSONDecodeError:
        print(f"  ❌  Could not parse sf org display output for '{alias}'")
        return None, None
    except subprocess.TimeoutExpired:
        print(f"  ❌  Timed out connecting to org '{alias}'")
        return None, None
    except Exception as e:
        print(f"  ❌  Could not check org '{alias}': {e}")
        return None, None


def check_data_cloud(instance_url: str, access_token: str) -> bool:
    """Try to hit the Data Cloud streams list endpoint."""
    if not instance_url or not access_token:
        print("  ⏭️  Data Cloud check skipped (no org credentials)")
        return False

    url = f"{instance_url}/services/data/v62.0/ssot/data-streams?dataspace=default"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"  ✅  Data Cloud API accessible (HTTP {r.status})")
            return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        if e.code == 401:
            print("  ❌  Data Cloud API: auth token expired")
            print(f"      Run: sf org login web --alias <alias>")
        elif e.code == 403:
            print("  ❌  Data Cloud API: access denied")
            print("      The org may not have Data Cloud licensed/provisioned.")
            print("      Contact your Salesforce admin to verify Data Cloud is enabled.")
        elif e.code == 404:
            print("  ❌  Data Cloud API endpoint not found")
            print("      Possible reasons: org not on v62 API, or Data Cloud not provisioned.")
        else:
            print(f"  ⚠️  Data Cloud API returned HTTP {e.code}")
            if body:
                print(f"      Response: {body}")
            print("      This may still work — proceed with caution.")
            return True  # Non-fatal
        if body and e.code not in (401,):
            print(f"      Detail: {body}")
        return False
    except urllib.error.URLError as e:
        print(f"  ❌  Could not reach org: {e.reason}")
        print("      Check your network connection and VPN.")
        return False
    except Exception as e:
        print(f"  ⚠️  Data Cloud check error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Check prerequisites for seed-demo-data."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--alias", "-a", help="Salesforce org alias to check")
    group.add_argument("--config", "-c", help="Path to config-<slug>.json (reads alias from it)")
    args = parser.parse_args()

    alias = args.alias
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            print(f"❌  Config file not found: {args.config}")
            sys.exit(1)
        cfg = json.loads(cfg_path.read_text())
        alias = cfg.get("org_alias") or cfg.get("alias")
        if not alias:
            print("❌  Config has no 'org_alias' field.")
            sys.exit(1)

    print("\n🔍  Pre-flight check — seed-demo-data\n")

    results = []
    results.append(check_python())
    results.append(check_sf_cli())

    instance_url, access_token = None, None
    if alias:
        print()
        instance_url, access_token = check_org(alias)
        results.append(instance_url is not None)
        print()
        dc_ok = check_data_cloud(instance_url, access_token)
        results.append(dc_ok)
    else:
        print()
        print("  ℹ️  Skipping org checks — pass --alias <org> to verify Data Cloud access.")

    all_ok = all(results)
    print()
    if all_ok:
        print("✅  All checks passed — ready to seed.\n")
        sys.exit(0)
    else:
        failed = sum(1 for r in results if not r)
        print(f"❌  {failed} check(s) failed — fix the issues above before running the wizard.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
