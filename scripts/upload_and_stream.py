#!/usr/bin/env python3
"""
Upload CSVs to Data Cloud via the Salesforce Drive presigned-URL flow.
Creates one DLO (Data Lake Object / data stream) per CSV file.

Usage:
    python3 upload_and_stream.py --config config.json [--data-dir data/<slug>]

Proven pattern from sfdrive_fileupload_headless.py (Storm orgs, 2026-05-29):
  1. Lightning frontdoor → /lightning/page/home → inline.js → eikoocnekot cookie → aura_token
  2. aura://SfDriveController/ACTION$generateSFDrivePresignedCredentials (NOT mintPresignedUrl)
  3. PUT CSV bytes to S3 presigned URL
  4. POST /ssot/data-streams with datastreamType=CONNECTORSFRAMEWORK + dataLakeObjectInfo body

Idempotent: if a stream with the same name already exists, it is skipped.
"""
import argparse
import csv as _csv
import datetime
import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import api as _api  # noqa: E402

API_V = "v62.0"
D = chr(36)  # literal '$' — keep OUT of f-strings to avoid confusion

# Category for each CSV filename pattern.
#
# Rule (per user definition):
#   PROFILE     ("¿Quién es?")   — descriptive/stable data. NO Event DateTime required.
#   ENGAGEMENT  ("¿Qué hizo y cuándo?") — immutable action events with Event DateTime,
#               linked to individual, orderable in timeline. Subject to P2Y lookback.
#               MUST have event_datetime / sent_date / tx_datetime mapped as
#               eventDateTimeFieldName.  DO NOT use for contracts/policies (older-than-2y
#               records would become invisible to segment filters).
#   OTHER       ("¿Qué tiene?")  — contracts, product holdings, reference data.
#               Mutable records whose state/dates change over time. No lookback limit.
CATEGORY_MAP = {
    # ── PROFILE ──────────────────────────────────────────────────────────────
    "contacts":              "Profile",
    "contact_emails":        "Profile",
    "contact_phones":        "Profile",
    "contact_addresses":     "Profile",
    # ── ENGAGEMENT ───────────────────────────────────────────────────────────
    # Immutable interaction events — all have a required Event DateTime field.
    "email_engagement":      "Engagement",  # sent_date (DateTime)
    "web_engagement":        "Engagement",  # event_datetime (DateTime) — NEW
    "loyalty_transactions":  "Engagement",  # event_datetime (DateTime) — earn/redeem events
    "transactions":          "Engagement",  # tx_datetime (DateTime)    — banking spend events
    "flight_bookings":       "Engagement",  # booking_datetime (DateTime) — airlines
    # ── ENGAGEMENT (continued) — Transaction Journal events ─────────────────
    # Immutable purchase/fill events with DateTime — promoted from Other.
    # Subject to P2Y lookback — records older than 2 years become invisible to segments.
    "purchase_orders":       "Engagement",  # order_datetime (DateTime)   — food B2C
    "sales_orders":          "Engagement",  # order_datetime (DateTime)   — retail
    "wholesale_orders":      "Engagement",  # order_datetime (DateTime)   — food B2B
    "prescriptions":         "Engagement",  # fill_datetime (DateTime)    — pharma
    # ── OTHER ────────────────────────────────────────────────────────────────
    # Contracts, portfolios, reference data — mutable, no lookback limit.
    "financial_accounts":    "Other",
    "service_contracts":     "Other",
    "insurance_policies":    "Other",
    "insurance_claims":      "Other",
    "order_lines":           "Other",
    "usage_records":         "Other",
    "wholesale_order_lines": "Other",
    "ht_subscriptions":      "Other",
    "ht_usage_records":      "Other",
    "ht_support_tickets":    "Other",
    "banking_products":      "Other",       # NEW — credit cards, loans, mortgage holdings
    "utility_contracts":     "Other",       # utilities — contract holdings
    "consumption_records":   "Other",       # utilities — monthly usage records
    "medical_visits":        "Other",
    "lab_results":           "Other",
    "memberships":           "Other",
    "activity_records":      "Engagement",
    # ── ECOMMERCE ────────────────────────────────────────────────────────────
    "ecom_orders":           "Engagement",  # order_datetime (DateTime)
    "ecom_order_lines":      "Other",
    "cart_abandonments":     "Engagement",  # abandonment_datetime (DateTime)
    # ── HOSPITALITY ──────────────────────────────────────────────────────────
    "hotel_stays":           "Engagement",  # checkin_datetime (DateTime)
    # ── MEDIA ────────────────────────────────────────────────────────────────
    "subscriptions":         "Other",       # plan holdings — mutable
    "content_views":         "Engagement",  # view_datetime (DateTime)
    # ── AUTOMOTIVE ───────────────────────────────────────────────────────────
    "vehicles":              "Other",       # vehicle ownership — mutable
    "service_records":       "Other",       # service history — mutable
    # ── REAL ESTATE ──────────────────────────────────────────────────────────
    "property_inquiries":    "Engagement",  # inquiry_datetime (DateTime)
    "property_transactions": "Other",       # closed deals — mutable
    # ── BETTING ──────────────────────────────────────────────────────────────
    "betting_accounts":      "Other",       # account holdings — mutable
    "betting_transactions":  "Engagement",  # transaction_datetime (DateTime)
    # ── POSTAL ───────────────────────────────────────────────────────────────
    "parcels":               "Engagement",  # ship_datetime (DateTime)
    "postal_products":       "Other",       # subscription holdings — mutable
}

# Which column is the primary key, by file stem
PK_MAP = {
    "contacts":             "id",
    "contact_emails":       "id",
    "contact_phones":       "id",
    "contact_addresses":    "id",
    "email_engagement":     "event_id",
    "web_engagement":       "event_id",
    "banking_products":     "product_id",
    "insurance_policies":   "policy_id",
    "insurance_claims":     "claim_id",
    "purchase_orders":      "order_id",
    "order_lines":          "line_id",
    "loyalty_transactions": "tx_id",
    "sales_orders":         "order_id",
    "financial_accounts":   "account_id",
    "transactions":         "tx_id",
    "prescriptions":        "rx_id",
    "service_contracts":    "contract_id",
    "usage_records":        "usage_id",
    "wholesale_orders":     "order_id",
    "wholesale_order_lines": "line_id",
    "ht_subscriptions":     "sub_id",
    "ht_usage_records":     "usage_id",
    "ht_support_tickets":   "ticket_id",
    "flight_bookings":      "booking_id",
    "utility_contracts":    "contract_id",
    "consumption_records":  "record_id",
    "medical_visits":       "visit_id",
    "lab_results":          "result_id",
    "memberships":          "membership_id",
    "activity_records":     "activity_id",
    "ecom_orders":          "order_id",
    "ecom_order_lines":     "line_id",
    "cart_abandonments":    "abandonment_id",
    "hotel_stays":          "stay_id",
    "subscriptions":        "subscription_id",
    "content_views":        "view_id",
    "vehicles":             "vehicle_id",
    "service_records":      "service_id",
    "property_inquiries":   "inquiry_id",
    "property_transactions":"transaction_id",
    "betting_accounts":     "account_id",
    "betting_transactions": "tx_id",
    "parcels":              "parcel_id",
    "postal_products":      "product_id",
}

# Event date field for Engagement DLOs only (field name: eventDateTimeFieldName in API).
# MUST be a DateTime field in the CSV (YYYY-MM-DDTHH:MM:SS.000Z).
# Only streams listed as "Engagement" in CATEGORY_MAP need an entry here.
EVENT_DATE_MAP = {
    "email_engagement":     "sent_date",
    "web_engagement":       "event_datetime",
    "loyalty_transactions": "event_datetime",
    "transactions":         "tx_datetime",
    # Transaction Journal events — promoted to Engagement
    "purchase_orders":      "order_datetime",
    "sales_orders":         "order_datetime",
    "wholesale_orders":     "order_datetime",
    "prescriptions":        "fill_datetime",
    "flight_bookings":      "booking_datetime",
    "activity_records":     "activity_date",
    "ecom_orders":          "order_datetime",
    "cart_abandonments":    "abandonment_datetime",
    "hotel_stays":          "checkin_datetime",
    "content_views":        "view_datetime",
    "property_inquiries":   "inquiry_datetime",
    "betting_transactions": "transaction_datetime",
    "parcels":              "ship_datetime",
}


def infer_schema(csv_path: Path, pk_col: str = None) -> list:
    """Return [(name, dataType, isPrimaryKey), ...] inferred from CSV header + sample."""
    def classify(vals):
        if not vals:
            return "Text"
        def is_num(v):
            try:
                float(v); return True
            except ValueError:
                return False
        if all(is_num(v) for v in vals):
            return "Number"
        if all(re.match(r"^\d{4}-\d{2}-\d{2}[T ]?\d{2}:\d{2}", v) for v in vals):
            return "DateTime"
        if all(re.match(r"^\d{4}-\d{2}-\d{2}$", v) for v in vals):
            return "Date"
        if all(re.match(r"^\d{4}-\d{2}$", v) for v in vals):
            return "Date"
        return "Text"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = _csv.reader(f)
        header = next(reader)
        samples: dict[str, list] = {h: [] for h in header}
        for i, row in enumerate(reader):
            if i >= 200:
                break
            for h, v in zip(header, row):
                if v:
                    samples[h].append(v)

    pk = pk_col or header[0]
    return [(h, classify(samples[h]), h == pk) for h in header]


def get_user_id(alias: str) -> str:
    """Get the Salesforce user ID for the org alias."""
    try:
        out = subprocess.check_output(
            ["sf", "org", "display", "user", "--target-org", alias, "--json"],
            stderr=subprocess.DEVNULL,
        )
        return json.loads(out)["result"]["id"]
    except Exception:
        return ""


def get_lightning_url(instance_url: str) -> str:
    """Convert instance URL to Lightning URL."""
    # e.g. https://foo.my.salesforce.com → https://foo.lightning.force.com
    return re.sub(r"\.my\.salesforce\.com", ".lightning.force.com", instance_url)


def get_session(instance_url: str, access_token: str) -> tuple:
    """
    Return (opener, aura_token, aura_context) using the proven
    Lightning home → inline.js → eikoocnekot cookie approach.
    """
    lurl = get_lightning_url(instance_url)
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]

    # Step 1: frontdoor — establishes session cookies
    fd_url = f"{instance_url}/secur/frontdoor.jsp?sid={urllib.parse.quote(access_token)}"
    try:
        opener.open(fd_url, timeout=40).read()
    except Exception:
        pass  # redirect / 302 is fine

    # Step 2: Lightning home page to get the aura context blob + jwt
    try:
        page = opener.open(f"{lurl}/lightning/page/home", timeout=40).read().decode(errors="replace")
    except Exception as e:
        raise RuntimeError(f"Could not load Lightning home: {e}")

    enc_m = re.search(r'/l/(%7B[^/]+%7D)/inline\.js', page)
    jwt_m = re.search(r'/inline\.js\?jwt=([^"\'&\s]+)', page)
    if not enc_m or not jwt_m:
        raise RuntimeError("Could not find inline.js reference in Lightning home page. "
                           "Check that the org is accessible and Data Cloud is provisioned.")

    enc = enc_m.group(1)     # URL-encoded aura.context JSON
    jwt = jwt_m.group(1)

    # Step 3: Load inline.js to get the token cookie name
    js_url = f"{lurl}/l/{enc}/inline.js?jwt={jwt}"
    js = opener.open(js_url, timeout=40).read().decode(errors="replace")

    ctx = json.loads(urllib.parse.unquote(enc))
    token_name_m = re.search(r'"eikoocnekot"\s*:\s*"([^"]+)"', js)
    if not token_name_m:
        raise RuntimeError("Could not find eikoocnekot token name in inline.js")

    token_cookie_name = token_name_m.group(1)
    try:
        aura_token = next(c.value for c in cj if c.name == token_cookie_name)
    except StopIteration:
        raise RuntimeError(f"Cookie '{token_cookie_name}' not found. "
                           "Cookies: " + str([c.name for c in cj]))

    aura_context = {
        "mode":    ctx["mode"],
        "fwuid":   ctx["fwuid"],
        "app":     ctx["app"],
        "loaded":  ctx["loaded"],
        "dn":      [],
        "globals": {},
        "uad":     False,
    }

    return opener, lurl, aura_token, aura_context


def aura_call(opener, lurl: str, aura_token: str, aura_context: dict, message: dict) -> dict:
    """POST an Aura action and return the parsed response dict."""
    body = urllib.parse.urlencode({
        "message":       json.dumps(message),
        "aura.context":  json.dumps(aura_context),
        "aura.token":    aura_token,
    }).encode()
    req = urllib.request.Request(
        f"{lurl}/aura?r=1&aura.ApexAction.execute=1",
        data=body, method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8")
    req.add_header("User-Agent", "Mozilla/5.0")
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def mint_presigned_url(opener, lurl: str, aura_token: str, aura_context: dict,
                       uid: str, filename: str) -> tuple[str, dict]:
    """
    Call SfDriveController.generateSFDrivePresignedCredentials.
    Returns (presigned_url, returnValue_dict).
    """
    _UTC = getattr(datetime, "UTC", datetime.timezone.utc)
    iso = datetime.datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%S:%f")[:-3] + "Z"
    drive_dir = f"{D}dc_file_upload{D}/{uid}/{iso}/"

    desc = f"aura://SfDriveController/ACTION{D}generateSFDrivePresignedCredentials"
    msg = {
        "actions": [{
            "id": "1;a",
            "descriptor": desc,
            "callingDescriptor": "UNKNOWN",
            "params": {
                "generateSFDrivePresignedCredentialsInput": {
                    "driveDirectory": drive_dir,
                    "fileName": filename,
                }
            },
        }]
    }
    resp = aura_call(opener, lurl, aura_token, aura_context, msg)
    actions = resp.get("actions", [])
    if not actions or actions[0].get("state") != "SUCCESS":
        raise RuntimeError(f"generateSFDrivePresignedCredentials failed: {str(resp)[:500]}")
    rv = actions[0]["returnValue"]
    presigned = rv["presignedUrl"].replace("&amp;", "&")
    return presigned, rv


def put_s3(presigned_url: str, csv_bytes: bytes, extra_headers: dict = None) -> int:
    """PUT CSV bytes to the S3 presigned URL. Returns HTTP status."""
    headers = {
        "Content-Type":   "text/csv",
        "Content-Length": str(len(csv_bytes)),
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(presigned_url, data=csv_bytes, method="PUT", headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status


def parse_s3_dirs(presigned_url: str) -> tuple[str, str]:
    """
    Parse the presigned URL to extract importDirectory and parentDirectory
    as expected by the DataConnector/UploadedFiles stream body.

    S3 path structure:
      /<bucket>/<prefix>/flup-fileUploads/<dc_file_upload$>/<uid>/<iso>/<file>

    Returns:
      parentDirectory = "s3://<bucket>/<prefix>/flup-fileUploads/<dc_file_upload$>"
      importDirectory = "<uid>/<iso>"
    """
    pp = urlparse(presigned_url)
    bucket = pp.netloc.split(".s3.")[0]
    segs = [unquote(x) for x in pp.path.lstrip("/").split("/")]
    # Find the flup-* segment (e.g. "flup-fileUploads")
    fi = next((i for i, x in enumerate(segs) if x.startswith("flup-")), None)
    if fi is None:
        raise RuntimeError(f"Could not find flup-* segment in S3 URL: {presigned_url[:200]}")
    parent_segs = segs[:fi + 1]   # up to and including flup-fileUploads
    # segs[fi+1] is the dc_file_upload$ directory name
    parent_dir = "s3://" + bucket + "/" + "/".join(parent_segs) + "/" + segs[fi + 1]
    import_dir = "/".join(segs[fi + 2:-1])   # <uid>/<iso>
    return import_dir, parent_dir


def create_stream(core_url: str, core_token: str,
                  stream_name: str, stream_label: str,
                  category: str, fields: list,
                  filename: str, import_dir: str, parent_dir: str,
                  event_date_field: str = None) -> tuple[int, dict]:
    """
    POST to /ssot/data-streams using the proven DataConnector/UploadedFiles body format.
    """
    dll_name = stream_name + "__dll"

    dlo_fields = [
        {"name": n, "label": n, "dataType": dt, "isPrimaryKey": pk}
        for n, dt, pk in fields
    ]
    source_fields = [
        {"name": n, "dataType": dt}
        for n, dt, _ in fields
    ]
    mappings = [
        {"sourceFieldLabel": n, "targetFieldReturntype": dt, "targetFieldName": n}
        for n, dt, _ in fields
    ]

    body = {
        "name":             stream_name,
        "label":            stream_label,
        "dataAccessMode":   "INGEST",
        "datastreamType":   "CONNECTORSFRAMEWORK",
        "connectorInfo": {
            "connectorType":    "DataConnector",
            "connectorDetails": {"name": "UploadedFiles"},
        },
        "advancedAttributes": {
            "importDirectory":        import_dir,
            "fileName":               filename,
            "fileType":               "CSV",
            "delimiter":              ",",
            "isDataStreamConfigValid": "true",
            "parentDirectory":        parent_dir,
        },
        "dataLakeObjectInfo": {
            "name":          dll_name,
            "label":         stream_label,
            "category":      category,
            "dataspaceInfo": [{"name": "default"}],
            "dataLakeFieldInputRepresentations": dlo_fields,
            **({"eventDateTimeFieldName": event_date_field}
               if category == "Engagement" and event_date_field else {}),
        },
        "sourceFields": source_fields,
        "mappings":     mappings,
        "refreshConfig": {
            "frequency":              {"frequencyType": "None"},
            "refreshMode":            "TOTAL_REPLACE",
            "isAccelerationEnabled":  False,
        },
    }

    return _api(core_url, core_token, "POST",
                f"/services/data/{API_V}/ssot/data-streams", body)


def trigger_run(core_url: str, core_token: str, stream_name: str) -> bool:
    """Trigger an ingestion run for a data stream (stream name is also the path ID)."""
    status, data = _api(
        core_url, core_token, "POST",
        f"/services/data/{API_V}/ssot/data-streams/{stream_name}/actions/run",
        {},
    )
    if status in (200, 201, 202):
        return True
    # Legacy fallback
    status2, _ = _api(
        core_url, core_token, "POST",
        f"/services/data/{API_V}/ssot/data-streams/{stream_name}/runs",
        {},
    )
    return status2 in (200, 201, 202)


def list_existing_streams(core_url: str, core_token: str) -> set:
    """Return set of existing stream names (paginates through all pages)."""
    url = f"/services/data/{API_V}/ssot/data-streams?dataspace=default"
    names = set()
    while url:
        status, data = _api(core_url, core_token, "GET", url)
        if status != 200 or not isinstance(data, dict):
            break
        for s in data.get("dataStreams", []):
            names.add(s.get("name", ""))
            names.add(s.get("connectorName", ""))
        url = data.get("nextPageUrl")  # None when last page
    return names - {""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    slug = cfg.get("clientSlug", "client")
    prefix = slug.replace("-", "_").title().replace("_", "")  # e.g. "Migdal"
    data_dir = Path(args.data_dir or cfg.get("outputDir", f"data/{slug}"))

    print(f"\n📤  Uploading CSVs for {cfg.get('clientName', slug)}")
    print(f"    Org:  {alias}")
    print(f"    Data: {data_dir.resolve()}\n")

    # Auth — core token only (CDP token optional)
    from _auth import get_tokens, _sf_display
    core_url, core_token, _cdp_url, _cdp_tok = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}")

    existing = list_existing_streams(core_url, core_token)
    print(f"  ✓  Found {len(existing)} existing streams\n")

    # Get user ID for drive directory
    uid = get_user_id(alias)
    if not uid:
        print("  ⚠️  Could not get user ID — upload may fail")
    else:
        print(f"  ✓  User ID: {uid}")

    # Get Lightning session + Aura CSRF
    access_token, instance_url = _sf_display(alias)
    print(f"  ⚡  Establishing Lightning session...", end=" ", flush=True)
    opener, lurl, aura_token, aura_context = get_session(instance_url, access_token)
    print(f"✓  ({lurl})\n")

    csvs = sorted(data_dir.glob("*.csv"))
    if not csvs:
        print(f"  ⚠️  No CSV files found in {data_dir}")
        sys.exit(1)

    def resolve_map(mapping: dict, stem: str, default=None):
        """Exact match first; then longest-prefix match (handles *_2025_2026 suffixed files)."""
        if stem in mapping:
            return mapping[stem]
        # Try progressively shorter prefixes by dropping trailing _XXXX segments
        parts = stem.split("_")
        for i in range(len(parts) - 1, 0, -1):
            prefix_key = "_".join(parts[:i])
            if prefix_key in mapping:
                return mapping[prefix_key]
        return default

    results = []
    for csv_path in csvs:
        stem = csv_path.stem  # e.g. "contacts" or "insurance_policies_2025_2026"
        stream_name  = f"{prefix}_{stem.replace('_', ' ').title().replace(' ', '_')}"
        stream_label = f"{cfg.get('clientName', prefix)} {stem.replace('_', ' ').title()}"
        category     = resolve_map(CATEGORY_MAP, stem, "Profile")
        pk_col       = resolve_map(PK_MAP, stem)
        event_field  = EVENT_DATE_MAP.get(stem) if category == "Engagement" else None

        if stream_name in existing:
            print(f"  ↩  {stream_name}  (already exists — skipped)")
            results.append({"file": csv_path.name, "stream": stream_name, "status": "existing"})
            continue

        csv_bytes = csv_path.read_bytes()
        size_kb = len(csv_bytes) / 1024
        print(f"  ⬆  {csv_path.name}  ({size_kb:.0f} KB)", end=" ", flush=True)

        if args.dry_run:
            fields = infer_schema(csv_path, pk_col)
            print(f"[dry-run — {len(fields)} fields]")
            results.append({"file": csv_path.name, "stream": stream_name, "status": "dry-run"})
            continue

        fields = infer_schema(csv_path, pk_col)

        try:
            # Step 1: Mint presigned URL
            presigned, rv = mint_presigned_url(
                opener, lurl, aura_token, aura_context,
                uid, csv_path.name,
            )

            # Step 2: Parse S3 path components
            import_dir, parent_dir = parse_s3_dirs(presigned)

            # Step 3: PUT CSV to S3
            extra_hdrs = rv.get("headers", {})  # BYOK KMS headers if any
            put_status = put_s3(presigned, csv_bytes, extra_hdrs)
            if put_status not in (200, 201, 204):
                print(f"⚠️  S3 PUT returned {put_status}")
                results.append({"file": csv_path.name, "stream": stream_name,
                                 "status": f"upload-fail-{put_status}"})
                continue

            # Step 4: Create stream
            status, resp = create_stream(
                core_url, core_token,
                stream_name, stream_label,
                category, fields,
                csv_path.name, import_dir, parent_dir,
                event_field,
            )

        except Exception as e:
            print(f"✗  {e}")
            results.append({"file": csv_path.name, "stream": stream_name,
                             "status": "error", "detail": str(e)})
            continue

        if status in (200, 201):
            print(f"✓  → {stream_name}")
            results.append({"file": csv_path.name, "stream": stream_name,
                             "status": "created", "id": resp.get("id", "")})
        elif status == 409 or "DUPLICATE" in str(resp).upper() or "already exists" in str(resp).lower():
            print(f"↩  (duplicate)")
            results.append({"file": csv_path.name, "stream": stream_name, "status": "duplicate"})
        else:
            errmsg = str(resp)[:200]
            print(f"✗  ({status}: {errmsg})")
            results.append({"file": csv_path.name, "stream": stream_name,
                             "status": f"error-{status}", "detail": errmsg})

        time.sleep(0.5)  # be gentle on rate limits

    # Save results
    results_path = data_dir / "upload_results.json"
    results_path.write_text(json.dumps(results, indent=2))

    created  = [r for r in results if r["status"] == "created"]
    skipped  = [r for r in results if r["status"] in ("existing", "duplicate")]
    failed   = [r for r in results if r["status"] not in ("created", "existing", "duplicate", "dry-run")]

    print(f"\n✅  {len(created)} new streams created")
    print(f"   {len(skipped)} already existed (skipped)")
    if failed:
        print(f"   ⚠️  {len(failed)} failures — see {results_path}")

    # Trigger ingestion — wait 30s first so Salesforce has time to register the streams.
    # Include "existing" streams too: a stream may have been created in a previous partial
    # run but never had ingestion triggered (status stays NONE). Re-triggering is safe —
    # refreshMode is TOTAL_REPLACE so it just re-ingests the latest file.
    streams_to_trigger = [r["stream"] for r in results if r["status"] in ("created", "duplicate", "existing")]
    if streams_to_trigger:
        print(f"\n⏳  Waiting 30 seconds for streams to register before triggering ingestion...")
        time.sleep(30)
        print(f"🔄  Triggering ingestion on {len(streams_to_trigger)} streams...")
        for stream_name in streams_to_trigger:
            triggered = trigger_run(core_url, core_token, stream_name)
            status_label = "✓" if triggered else "⚠️ (will retry on verify)"
            print(f"   {status_label}  {stream_name}")
            time.sleep(0.5)

    print(f"\n⏳  Ingestion running in the background.")
    print(f"   Check Data Cloud Setup → Data Streams to monitor progress.")


if __name__ == "__main__":
    main()
