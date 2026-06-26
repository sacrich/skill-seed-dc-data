#!/usr/bin/env python3
"""
Deploy the Insurance Customer 360 contact page to Salesforce.

Usage:
    python3 deploy_insurance_page.py --config config.json

What it does:
  1. Reads config.json (clientName, clientSlug, orgAlias, industry)
  2. Copies the template from ../templates/insurance-contact-page/
  3. Substitutes tokens: PREFIX, CLIENT, brand colours, JWT key, DLO name
  4. Writes a deployable force-app structure in data/<slug>/force-app/
  5. Runs: sf project deploy start --source-dir <path> --target-org <alias>

Token reference:
  PREFIX           → slug title-cased (e.g. "Migdal")
  __CLIENT__       → cfg.clientName
  __CLIENT_UPPER__ → cfg.clientName.upper()
  __ADMIN_USERNAME__          → from sf org display
  __JWT_CONSUMER_KEY__        → config.jwtConsumerKey OR empty placeholder
  __JWT_PKCS8_DER_BASE64__    → config.jwtKeyBase64    OR empty placeholder
  __CONTACTS_DLO__            → resolved from upload_results.json
  __HOUSEHOLD_LINK_DMO__      → "__HOUSEHOLD_LINK_DMO__" (not built in Phase 1)
  __C_PRIMARY__ etc.          → theme colours from config.theme
  __CCY_SYMBOL__              → config.ccySymbol (default ₪)
  __NUM_LOCALE__              → config.numLocale (default he-IL)

Insurance-specific:
  The FlexiPage must be manually assigned in Lightning App Builder after deploy
  (sf CLI can deploy the XML, but record-page assignments need the Builder UI).

Idempotent: re-running re-deploys the same components (overwrite).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "insurance-contact-page"

# ── Default brand theme (Migdal: navy + gold) ────────────────────────────────
DEFAULT_THEME = {
    "c_primary":      "#0f2044",
    "c_primary_dark": "#08152e",
    "c_accent":       "#c9a84c",
    "c_accent_deep":  "#9d7b28",
    "c_accent_soft":  "#f0d68a",
    "c_accent_10":    "rgba(201,168,76,.10)",
    "c_accent_15":    "rgba(201,168,76,.15)",
    "c_accent_18":    "rgba(201,168,76,.18)",
    "c_accent_40":    "rgba(201,168,76,.40)",
    "c_accent_45":    "rgba(201,168,76,.45)",
    "c_accent_50":    "rgba(201,168,76,.50)",
    "c_primary_08":   "rgba(15,32,68,.08)",
    "c_primary_10":   "rgba(15,32,68,.10)",
    "c_bg_alt":       "#fafaf8",
    "c_bg_alt2":      "#f0ede6",
    "c_border":       "#e2ddd4",
    "c_muted":        "#a09880",
    "c_hero_ink":     "#ffffff",
    "c_hero_ink_16":  "rgba(255,255,255,.16)",
    "c_hero_accent":  "#c9a84c",
}


def get_admin_username(alias: str) -> str:
    try:
        out = subprocess.run(
            ["sf", "org", "display", "--target-org", alias, "--json"],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(out.stdout)
        return data.get("result", {}).get("username", "")
    except Exception:
        return ""


def get_contacts_dlo(slug: str, output_dir: Path) -> str:
    """Resolve the DLO API name of the contacts stream from upload_results.json."""
    results_file = output_dir / "upload_results.json"
    if not results_file.exists():
        return ""
    try:
        results = json.loads(results_file.read_text())
        prefix = slug.replace("-", "_").title().replace("_", "")
        target = f"{prefix}_Contacts"
        for r in results:
            if r.get("stream", "").lower() == target.lower():
                # The stream name IS the DLO connector name
                return r.get("stream", "")
    except Exception:
        pass
    return ""


def substitute(text: str, subs: dict) -> str:
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def copy_and_sub(src: Path, dst: Path, subs: dict):
    dst.parent.mkdir(parents=True, exist_ok=True)
    content = src.read_text(encoding="utf-8")
    content = substitute(content, subs)
    dst.write_text(content, encoding="utf-8")


def build_subs(cfg: dict, prefix: str, admin_user: str, contacts_dlo: str) -> dict:
    """Build the full token→value substitution map."""
    theme = {**DEFAULT_THEME, **cfg.get("theme", {})}
    client = cfg.get("clientName", prefix)
    ccy    = cfg.get("ccySymbol", "₪")
    locale = cfg.get("numLocale", "he-IL")

    subs = {
        # Structural
        "PREFIX":                   prefix,
        "__CLIENT__":               client,
        "__CLIENT_UPPER__":         client.upper(),
        "__ADMIN_USERNAME__":       admin_user,
        "__JWT_CONSUMER_KEY__":     cfg.get("jwtConsumerKey", "REPLACE_JWT_CONSUMER_KEY"),
        "__JWT_PKCS8_DER_BASE64__": cfg.get("jwtKeyBase64",   "REPLACE_JWT_KEY"),
        "__CONTACTS_DLO__":         contacts_dlo or f"{prefix}Contacts",
        "__HOUSEHOLD_LINK_DMO__":   "__HOUSEHOLD_LINK_DMO__",  # not built in Phase 1
        "__CCY_SYMBOL__":           ccy,
        "__NUM_LOCALE__":           locale,
        # Brand tokens (CSS + Apex)
        "__C_PRIMARY__":      theme["c_primary"],
        "__C_PRIMARY_DARK__": theme["c_primary_dark"],
        "__C_ACCENT__":       theme["c_accent"],
        "__C_ACCENT_DEEP__":  theme["c_accent_deep"],
        "__C_ACCENT_SOFT__":  theme["c_accent_soft"],
        "__C_ACCENT_10__":    theme["c_accent_10"],
        "__C_ACCENT_15__":    theme["c_accent_15"],
        "__C_ACCENT_18__":    theme["c_accent_18"],
        "__C_ACCENT_40__":    theme["c_accent_40"],
        "__C_ACCENT_45__":    theme["c_accent_45"],
        "__C_ACCENT_50__":    theme["c_accent_50"],
        "__C_PRIMARY_08__":   theme["c_primary_08"],
        "__C_PRIMARY_10__":   theme["c_primary_10"],
        "__C_BG_ALT__":       theme["c_bg_alt"],
        "__C_BG_ALT2__":      theme["c_bg_alt2"],
        "__C_BORDER__":       theme["c_border"],
        "__C_MUTED__":        theme["c_muted"],
        "__C_HERO_INK__":     theme["c_hero_ink"],
        "__C_HERO_INK_16__":  theme["c_hero_ink_16"],
        "__C_HERO_ACCENT__":  theme["c_hero_accent"],
    }
    return subs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-deploy", action="store_true", help="Build force-app but skip sf deploy")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias  = cfg["orgAlias"]
    slug   = cfg.get("clientSlug", "client")
    prefix = slug.replace("-", "_").title().replace("_", "")
    output_dir = Path(cfg.get("outputDir", f"data/{slug}"))

    print(f"\n🏗️   Building Insurance Customer 360 for {cfg.get('clientName', slug)}")
    print(f"    Org:    {alias}")
    print(f"    Prefix: {prefix}\n")

    # ── Resolve tokens ────────────────────────────────────────────────────────
    admin_user   = get_admin_username(alias)
    contacts_dlo = get_contacts_dlo(slug, output_dir)
    print(f"  ✓  Admin user:    {admin_user or '⚠️  not found — fill manually'}")
    print(f"  ✓  Contacts DLO:  {contacts_dlo or '⚠️  not found — run upload_and_stream first'}")

    subs = build_subs(cfg, prefix, admin_user, contacts_dlo)

    # ── Build force-app directory ─────────────────────────────────────────────
    force_app = output_dir / "force-app" / "main" / "default"
    lwc_out   = force_app / "lwc"
    cls_out   = force_app / "classes"
    fp_out    = force_app / "flexipages"

    # Copy + substitute LWC components
    for comp_dir in (TEMPLATE / "lwc").iterdir():
        if not comp_dir.is_dir():
            continue
        comp_name = comp_dir.name.replace("PREFIX", prefix)
        dst_dir   = lwc_out / comp_name
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in comp_dir.iterdir():
            dst_name = src_file.name.replace("PREFIX", prefix)
            dst_file = dst_dir / dst_name
            if args.dry_run:
                print(f"  [dry-run] → {dst_file.relative_to(output_dir)}")
            else:
                copy_and_sub(src_file, dst_file, subs)
                print(f"  ✓  {dst_file.relative_to(output_dir)}")

    # Copy + substitute Apex classes
    for src_file in (TEMPLATE / "classes").iterdir():
        dst_name = src_file.name.replace("PREFIX", prefix)
        dst_file = cls_out / dst_name
        if args.dry_run:
            print(f"  [dry-run] → {dst_file.relative_to(output_dir)}")
        else:
            copy_and_sub(src_file, dst_file, subs)
            print(f"  ✓  {dst_file.relative_to(output_dir)}")

    # Copy + substitute FlexiPage
    fp_src = TEMPLATE / "flexipages" / "PREFIX_Insurance_Contact_Page.flexipage-meta.xml"
    fp_dst = fp_out / f"{prefix}_Insurance_Contact_Page.flexipage-meta.xml"
    if args.dry_run:
        print(f"  [dry-run] → {fp_dst.relative_to(output_dir)}")
    else:
        copy_and_sub(fp_src, fp_dst, subs)
        print(f"  ✓  {fp_dst.relative_to(output_dir)}")

    if args.dry_run:
        print("\n  ℹ️  Dry run — no files written.")
        return

    # ── Deploy ────────────────────────────────────────────────────────────────
    if args.skip_deploy:
        print(f"\n  ℹ️  --skip-deploy set. Force-app built at: {force_app.relative_to(output_dir)}")
        print(f"     Deploy manually:  sf project deploy start --source-dir {force_app} --target-org {alias}")
        return

    print(f"\n  ▶  Deploying to org '{alias}'...")
    result = subprocess.run(
        ["sf", "project", "deploy", "start",
         "--source-dir", str(force_app),
         "--target-org",  alias,
         "--json"],
        capture_output=True, text=True,
    )
    try:
        out = json.loads(result.stdout)
        if out.get("status", 1) == 0:
            print("  ✅  Deploy successful!")
        else:
            errs = out.get("result", {}).get("details", {}).get("componentFailures", [])
            print(f"  ✗  Deploy failed ({len(errs)} errors)")
            for e in errs[:5]:
                print(f"      {e.get('fullName','?')}: {e.get('problem','?')}")
    except Exception:
        print(f"  ✗  Deploy returned exit code {result.returncode}")
        print(result.stdout[:400])
        print(result.stderr[:200])
        return

    print(f"""
  Next steps:
  1. Open Lightning App Builder for a Contact record
  2. Select the "{prefix} Insurance 360" page → Assign as org/app default
  3. Open a Contact that was seeded by seed-dc-data
  4. The Insurance Hero (left), Policies, Claims and Engagement tabs should appear

  ⚠️  If churn_score / ltv show "—", the contacts DLO may need manual token check:
     Current __CONTACTS_DLO__ = "{contacts_dlo or '(empty)'}"
     Verify: sf data query --query "SELECT Id FROM {contacts_dlo or '<DLO>'} LIMIT 1" --use-tooling-api
""")


if __name__ == "__main__":
    main()
