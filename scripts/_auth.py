"""Auth helper — get Core + CDP tokens from an sf CLI-authenticated org.

No Connected App, no JWT key file required.
Uses `sf org display` to get the SFDX session token, then exchanges it
for a CDP (a360) token to call Data Cloud APIs.

Usage:
    from _auth import get_tokens
    core_url, core_token, cdp_url, cdp_token = get_tokens(org_alias)
"""
import json
import subprocess
import urllib.parse
import urllib.request
import urllib.error


def _sf_display(alias: str) -> tuple[str, str]:
    """Return (accessToken, instanceUrl) from `sf org display`."""
    # SFDX_DISABLE_DNS_CHECK=true avoids MyDomainResolverTimeoutError on some networks
    _env = {**__import__("os").environ, "SFDX_DISABLE_DNS_CHECK": "true"}
    result = json.loads(
        subprocess.check_output(
            ["sf", "org", "display", "--target-org", alias, "--json"],
            stderr=subprocess.DEVNULL,
            env=_env,
        )
    )["result"]

    # sf CLI ≥2.136 may REDACT the access token — fall back to show-access-token
    tok = result.get("accessToken", "")
    if not tok or "REDACTED" in tok.upper():
        tok_r = json.loads(
            subprocess.check_output(
                ["sf", "org", "auth", "show-access-token", "-o", alias, "--json"],
                stderr=subprocess.DEVNULL,
                env=_env,
            )
        )["result"]
        tok = tok_r.get("accessToken", "") if isinstance(tok_r, dict) else str(tok_r)

    if not tok:
        raise RuntimeError(
            f"Could not retrieve access token for org '{alias}'. "
            f"Run: sf org login web --alias {alias}"
        )
    return tok.strip(), result["instanceUrl"].rstrip("/")


def _post_form(url: str, data: dict, timeout: int = 30) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def get_tokens(alias: str) -> tuple[str, str, str, str]:
    """Return (core_url, core_token, cdp_url, cdp_token).

    core_* → use for /services/data/v62.0/ssot/* (stream, mapping, IR APIs)
    cdp_*  → use for /api/v1/... and /api/v2/... (query, ingest APIs)
    """
    core_token, core_url = _sf_display(alias)

    status, resp = _post_form(
        f"{core_url}/services/a360/token",
        {
            "grant_type": "urn:salesforce:grant-type:external:cdp",
            "subject_token": core_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
    )
    if status != 200:
        # CDP token is only needed for the Apex controller's query fallback.
        # Upload, mapping, relationships, and IR scripts all use the core token.
        # Warn and continue — don't abort.
        print(f"  ⚠️  CDP token exchange returned {status}: {resp.get('error','?')} "
              f"— {resp.get('error_description','')}")
        print(f"     (Core token still valid; seeding scripts will proceed normally.)")
        return core_url, core_token, "", ""
    iu = resp.get("instance_url", "")
    cdp_url = iu if iu.startswith("http") else ("https://" + iu if iu else "")
    return core_url, core_token, cdp_url, resp.get("access_token", "")


def api(base_url: str, token: str, method: str, path: str, body=None, timeout: int = 120):
    """Make an authenticated REST call. Returns (status, response_dict)."""
    url = f"{base_url}{path}" if path.startswith("/") else path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt}
