#!/usr/bin/env python3
"""
Investigate Data Cloud DMO relationships, mappings, and diagnose setup issues.

Usage:
    python3 investigate_relationships.py --config config.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import get_tokens, api  # noqa: E402

API_V = "v62.0"
BASE = f"/services/data/{API_V}/ssot"

CUSTOM_DMOS = [
    "InsurancePolicy__dlm",
    "InsuranceClaim__dlm",
    "ssot__EmailEngagement__dlm",    # platform standard DMO (was EmailEngagement__dlm)
    "ssot__WebsiteEngagement__dlm",  # platform standard DMO (was WebEngagement__dlm)
]


def list_relationships(core_url, token):
    """List all DMO relationships."""
    st, data = api(core_url, token, "GET",
                   f"{BASE}/data-model-object-relationships?dataspace=default")
    print(f"\n=== ALL RELATIONSHIPS (HTTP {st}) ===")
    if st != 200:
        print(f"  Error: {data}")
        return []
    rels = data.get("dataModelObjectRelationships", [])
    print(f"  Total: {len(rels)}")
    for r in rels:
        print(f"\n  id={r.get('id')}")
        print(f"    label={r.get('label')}")
        print(f"    from:  {r.get('fromDataModelObject', {}).get('developerName')} "
              f"field={r.get('fromFieldName')}")
        print(f"    to:    {r.get('toDataModelObject', {}).get('developerName')} "
              f"field={r.get('toFieldName')}")
        print(f"    type:  {r.get('relationType')}")
        print(f"    status:{r.get('status')}")
    return rels


def list_dmo_mappings(core_url, token, dmo_name):
    """Show all field mappings for a DMO."""
    st, data = api(core_url, token, "GET",
                   f"{BASE}/data-model-object-mappings"
                   f"?dmoDeveloperName={dmo_name}&dataspace=default")
    print(f"\n=== MAPPINGS for {dmo_name} (HTTP {st}) ===")
    if st != 200:
        print(f"  Error: {data}")
        return
    maps = data.get("objectSourceTargetMaps", [])
    if not maps:
        print("  (no mappings)")
        return
    for m in maps:
        print(f"  sourceObject={m.get('sourceObjectName')}  "
              f"sourceName={m.get('sourceName')}")
        for f in m.get("fieldMappings", []):
            print(f"    {f.get('sourceFieldName'):40s}  →  "
                  f"{f.get('targetFieldName'):40s}  type={f.get('dataType')}")


def list_dmo_detail(core_url, token, dmo_name):
    """Get DMO detail including description."""
    st, data = api(core_url, token, "GET",
                   f"{BASE}/data-model-objects/{dmo_name}?dataspace=default")
    print(f"\n=== DMO DETAIL: {dmo_name} (HTTP {st}) ===")
    if st != 200:
        print(f"  Error: {data}")
        return None
    print(f"  label:       {data.get('label')}")
    print(f"  category:    {data.get('category')}")
    print(f"  description: {data.get('description', '(none)')!r}")
    return data


def list_all_dmos(core_url, token):
    """List all DMOs including their descriptions."""
    st, data = api(core_url, token, "GET",
                   f"{BASE}/data-model-objects?dataspace=default")
    print(f"\n=== ALL DMOs (HTTP {st}) ===")
    if st != 200:
        print(f"  Error: {data}")
        return []
    dmos = data.get("dataModelObjects", [])
    for d in dmos:
        name = d.get("developerName", "")
        if any(c in name for c in ["__dlm"]) and not name.startswith("ssot__"):
            print(f"  {name:50s}  desc={d.get('description', '(none)')!r}")
    return dmos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    core_url, core_token, _, _ = get_tokens(alias)
    print(f"\n✓ Authenticated: {core_url}")

    # 1. List all relationships — look for duplicates
    rels = list_relationships(core_url, core_token)

    # 2. Show relationship duplicates
    print("\n=== DUPLICATE ANALYSIS ===")
    seen = {}
    for r in rels:
        from_dmo = r.get("fromDataModelObject", {}).get("developerName", "")
        from_field = r.get("fromFieldName", "")
        to_dmo = r.get("toDataModelObject", {}).get("developerName", "")
        to_field = r.get("toFieldName", "")
        key = f"{from_dmo}.{from_field} → {to_dmo}.{to_field}"
        if key not in seen:
            seen[key] = []
        seen[key].append(r.get("id"))
    for key, ids in seen.items():
        if len(ids) > 1:
            print(f"  DUPLICATE: {key}")
            print(f"    IDs: {ids}")
        else:
            print(f"  OK:        {key}  (id={ids[0]})")

    # 3. Check EmailEngagement mappings (platform standard DMO)
    list_dmo_mappings(core_url, core_token, "ssot__EmailEngagement__dlm")

    # 4. Check InsurancePolicy mappings
    list_dmo_mappings(core_url, core_token, "InsurancePolicy__dlm")

    # 5. Check IndividualProfile mappings
    list_dmo_mappings(core_url, core_token, "IndividualProfile__dlm")

    # 6. Check DMO descriptions
    print("\n=== CUSTOM DMO DESCRIPTIONS ===")
    for dmo in CUSTOM_DMOS:
        list_dmo_detail(core_url, core_token, dmo)


if __name__ == "__main__":
    main()
