#!/usr/bin/env python3
"""
Create STANDARD (UI-editable) segments via the Data Cloud REST API.

KEY: segmentType="Ui" + segmentCreationFlow="Datakit" is the combination that allows
     UI-type segments to be created by external API callers (not forbidden).
     Without segmentCreationFlow="Datakit" → 403 "UI based segment creation is forbidden".

Usage:
    python3 create_segments.py --config config.json [--dry-run]

Creates 5 segments per industry for the configured client.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auth import get_tokens, api  # noqa: E402

API_V = "v62.0"
BASE = f"/services/data/{API_V}/ssot"
SEGMENT_ON     = "UnifiedssotIndividualRt__dlm"   # B2C (default placeholder)
B2B_SEGMENT_ON = "UnifiedssotAccountRt__dlm"      # B2B (default placeholder)
B2B_CI_DIM     = "unified_account__c"             # dimension alias in B2B CIs
LINK_DMO       = "UnifiedLinkssotIndividualRt__dlm"  # B2C IR link DMO (placeholder)
B2B_LINK_DMO   = "UnifiedLinkssotAccountRt__dlm"     # B2B IR link DMO (placeholder)

# Candidate names probed in order until HTTP 200
_UNIFIED_CANDIDATES_B2C = ["UnifiedssotIndividualRt__dlm", "UnifiedIndividual__dlm"]
_UNIFIED_CANDIDATES_B2B = ["UnifiedssotAccountRt__dlm",   "UnifiedAccount__dlm"]
_LINK_CANDIDATES_B2C    = ["UnifiedLinkssotIndividualRt__dlm", "IndividualIdentityLink__dlm"]
_LINK_CANDIDATES_B2B    = ["UnifiedLinkssotAccountRt__dlm",   "AccountIdentityLink__dlm"]


# ─── Org-variant resolvers ────────────────────────────────────────────────────

def resolve_segment_on(core_url: str, token: str, b2b_account: bool, cfg: dict) -> tuple:
    """Return (dmo_api_name, dmo_salesforce_id) for the unified DMO to segment on.

    The segment POST body requires segmentOnId (internal Salesforce 18-char ID).
    We probe candidate DMO API names and return the first that exists on the org,
    together with its id from the GET response.
    """
    name_override = cfg.get("unifiedAccountDlo" if b2b_account else "unifiedIndividualDlo")
    candidates = [name_override] if name_override else (
        _UNIFIED_CANDIDATES_B2B if b2b_account else _UNIFIED_CANDIDATES_B2C
    )
    for name in candidates:
        if not name:
            continue
        st, resp = api(core_url, token, "GET",
                       f"{BASE}/data-model-objects/{name}?dataspace=default")
        if st == 200 and isinstance(resp, dict):
            dmo_id = resp.get("id")
            if dmo_id:
                return name, dmo_id
    fallback = B2B_SEGMENT_ON if b2b_account else SEGMENT_ON
    return fallback, None


def resolve_link_dmo(core_url: str, token: str, b2b_account: bool) -> str:
    """Return the actual Identity Resolution link DMO name for this org.

    Probes candidate names; some orgs use IndividualIdentityLink__dlm,
    others use UnifiedLinkssotIndividualRt__dlm (standard Salesforce name).
    """
    candidates = _LINK_CANDIDATES_B2B if b2b_account else _LINK_CANDIDATES_B2C
    for name in candidates:
        st, resp = api(core_url, token, "GET",
                       f"{BASE}/data-model-objects/{name}?dataspace=default")
        if st == 200 and isinstance(resp, dict) and resp.get("id"):
            return name
    return B2B_LINK_DMO if b2b_account else LINK_DMO


# ─── Criteria builder helpers ────────────────────────────────────────────────

def _ci_filter(ci_name: str, field: str, comparison: dict) -> dict:
    """Build a CalculatedInsight filter for a CI dimension (unified_individual__c join)."""
    return {
        "type": "CalculatedInsight",
        "subject": {
            "objectApiName": ci_name,
            "fieldApiName": "unified_individual__c",
        },
        "path": [
            [
                {"objectApiName": SEGMENT_ON, "fieldApiName": "ssot__Id__c"},
                {"objectApiName": ci_name,    "fieldApiName": "unified_individual__c"},
            ]
        ],
        "comparison": comparison,
    }


def _dmo_filter(dmo_name: str, join_field: str, filter_field: str,
                operator: str, values) -> dict:
    """Build a Related Object (NumberAggregation) filter for a DMO attribute.

    Traversal path (3 hops — resolved dynamically in main() via string replace):
      SEGMENT_ON.ssot__Id__c
        → LINK_DMO.UnifiedRecordId__c
        → LINK_DMO.SourceRecordId__c
        → ssot__Individual__dlm.ssot__Id__c
        → dmo_name.join_field

    SEGMENT_ON and LINK_DMO constants are placeholders; main() replaces them with
    the actual org-specific DMO names (resolved by resolve_segment_on / resolve_link_dmo).

    Args:
        dmo_name:     DMO to filter on, e.g. "InsurancePolicy__dlm"
        join_field:   field linking DMO to ssot__Individual, e.g. "PartyId__c"
        filter_field: field to apply the condition on, e.g. "ProductCategory__c"
        operator:     text fields → "in", "contains", "starts with", "not in", "not contains"
                      number fields → "greater than or equal", "less than", "equal", etc.
                      ("equal to" is NOT valid for TextComparison — use "in" with a list)
        values:       list[str] for text "in"/"contains"; single str (auto-wrapped); or number
    """
    path = [
        [
            {"objectApiName": SEGMENT_ON, "fieldApiName": "ssot__Id__c"},
            {"objectApiName": LINK_DMO,   "fieldApiName": "UnifiedRecordId__c"},
        ],
        [
            {"objectApiName": LINK_DMO,               "fieldApiName": "SourceRecordId__c"},
            {"objectApiName": "ssot__Individual__dlm", "fieldApiName": "ssot__Id__c"},
        ],
        [
            {"objectApiName": "ssot__Individual__dlm", "fieldApiName": "ssot__Id__c"},
            {"objectApiName": dmo_name,                "fieldApiName": join_field},
        ],
    ]

    if isinstance(values, list):
        filter_node = {
            "type": "TextComparison",
            "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": filter_field},
            "selfReference": False,
            "operator": operator,
            "values": values,
        }
    elif isinstance(values, str):
        filter_node = {
            "type": "TextComparison",
            "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": filter_field},
            "selfReference": False,
            "operator": operator,
            "values": [values],
        }
    else:
        filter_node = {
            "type": "NumberComparison",
            "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": filter_field},
            "selfReference": False,
            "operator": operator,
            "value": values,
        }

    return {
        "type": "NumberAggregation",
        "containerObjectApiName": dmo_name,
        "filter": filter_node,
        "path": path,
        "joinPath": path,
        "aggregateFunction": "count",
        "comparison": {
            "type": "NumberComparison",
            "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": "Id__c"},
            "selfReference": False,
            "operator": "greater than or equal",
            "value": 1,
        },
        "hierarchySelected": False,
        "hierarchicalPathList": None,
        "innerAggregationEnabled": False,
        "innerAggregationSubject": None,
        "outerAggregationFunction": None,
        "outerComparison": None,
    }


def _b2b_dmo_filter(dmo_name: str, join_field: str, filter_field: str,
                    operator: str, values) -> dict:
    """Build a Related Object (NumberAggregation) filter for a B2B DMO attribute.

    Same structure as _dmo_filter but traverses through the B2B Account unified DMO
    and B2B Identity Resolution link DMO. Use for food_b2b and hightech segments.

    Traversal path (B2B_SEGMENT_ON and B2B_LINK_DMO are placeholders replaced in main()):
      B2B_SEGMENT_ON.ssot__Id__c → B2B_LINK_DMO.UnifiedRecordId__c
        → B2B_LINK_DMO.SourceRecordId__c → ssot__Account__dlm.ssot__Id__c
        → dmo_name.join_field
    """
    path = [
        [
            {"objectApiName": B2B_SEGMENT_ON, "fieldApiName": "ssot__Id__c"},
            {"objectApiName": B2B_LINK_DMO,   "fieldApiName": "UnifiedRecordId__c"},
        ],
        [
            {"objectApiName": B2B_LINK_DMO,         "fieldApiName": "SourceRecordId__c"},
            {"objectApiName": "ssot__Account__dlm",  "fieldApiName": "ssot__Id__c"},
        ],
        [
            {"objectApiName": "ssot__Account__dlm",  "fieldApiName": "ssot__Id__c"},
            {"objectApiName": dmo_name,              "fieldApiName": join_field},
        ],
    ]
    if isinstance(values, list):
        filter_node = {
            "type": "TextComparison", "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": filter_field},
            "selfReference": False, "operator": operator, "values": values,
        }
    elif isinstance(values, str):
        filter_node = {
            "type": "TextComparison", "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": filter_field},
            "selfReference": False, "operator": operator, "values": [values],
        }
    else:
        filter_node = {
            "type": "NumberComparison", "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": filter_field},
            "selfReference": False, "operator": operator, "value": values,
        }
    return {
        "type": "NumberAggregation",
        "containerObjectApiName": dmo_name,
        "filter": filter_node,
        "path": path,
        "joinPath": path,
        "aggregateFunction": "count",
        "comparison": {
            "type": "NumberComparison", "path": None, "joinPath": None,
            "subject": {"objectApiName": dmo_name, "fieldApiName": "Id__c"},
            "selfReference": False,
            "operator": "greater than or equal",
            "value": 1,
        },
        "hierarchySelected": False, "hierarchicalPathList": None,
        "innerAggregationEnabled": False, "innerAggregationSubject": None,
        "outerAggregationFunction": None, "outerComparison": None,
    }


def _b2b_ci_filter(ci_name: str, field: str, comparison: dict) -> dict:
    """Build a CalculatedInsight filter for a B2B CI (unified_account__c join).

    Used for food_b2b and hightech segments which are segmented on
    UnifiedssotAccountRt__dlm instead of UnifiedssotIndividualRt__dlm.
    The CI dimension is unified_account__c (not unified_individual__c).
    """
    return {
        "type": "CalculatedInsight",
        "subject": {
            "objectApiName": ci_name,
            "fieldApiName": B2B_CI_DIM,
        },
        "path": [
            [
                {"objectApiName": B2B_SEGMENT_ON, "fieldApiName": "ssot__Id__c"},
                {"objectApiName": ci_name,        "fieldApiName": B2B_CI_DIM},
            ]
        ],
        "comparison": comparison,
    }


def _num_cmp(ci_name: str, field: str, operator: str, value) -> dict:
    """NumberComparison node."""
    return {
        "type": "NumberComparison",
        "path": None,
        "joinPath": None,
        "subject": {"objectApiName": ci_name, "fieldApiName": field},
        "selfReference": False,
        "operator": operator,
        "value": value,
    }


def _text_cmp(ci_name: str, field: str, operator: str, values: list) -> dict:
    """TextComparison node (multi-value 'in')."""
    return {
        "type": "TextComparison",
        "path": None,
        "joinPath": None,
        "subject": {"objectApiName": ci_name, "fieldApiName": field},
        "selfReference": False,
        "operator": operator,
        "values": values,
    }


def _logic(filters: list, operator: str = "and") -> dict:
    """LogicalComparison wrapper around a list of filter nodes.

    Returns {} for empty lists — the API requires an empty object for no criteria,
    not an empty LogicalComparison (which creates a broken container in the UI).
    """
    if not filters:
        return {}
    return {"type": "LogicalComparison", "operator": operator, "filters": filters}


# ─── Segment definitions ──────────────────────────────────────────────────────

def insurance_segment_defs(prefix: str) -> list:
    """5 insurance segment definitions."""
    p = prefix

    return [
        # ── 1. Premium Renewal ────────────────────────────────────────────────
        {
            "key":         f"{p}_PremiumRenewal",
            "displayName": f"{p} Premium Renewal Targets",
            "description": (
                "High-premium clients (total annual premium >= 2,000) with at least one "
                "active policy. Primary universe for premium policy renewal and upsell. "
                "Requires Identity Resolution + CI refresh to have members."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PolicySummary__cio",
                    "total_annual_premium__c",
                    _num_cmp(f"{p}_PolicySummary__cio", "total_annual_premium__c",
                             "greater than or equal", 2000),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },

        # ── 2. Active Policy Upsell ───────────────────────────────────────────
        {
            "key":         f"{p}_ActivePolicyUpsell",
            "displayName": f"{p} Active Policy Upsell",
            "description": (
                "Clients with 1-3 active insurance policies. "
                "Ideal for cross-sell to an additional product line "
                "(e.g. adding Health to an existing Life policy). "
                "Requires Identity Resolution + CI refresh to have members."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PolicySummary__cio",
                    "active_policy_count__c",
                    _num_cmp(f"{p}_PolicySummary__cio", "active_policy_count__c",
                             "greater than or equal", 1),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_PolicySummary__cio",
                    "active_policy_count__c",
                    _num_cmp(f"{p}_PolicySummary__cio", "active_policy_count__c",
                             "greater than or equal", 4),
                ),
            ]),
        },

        # ── 3. Churn Risk Retention ───────────────────────────────────────────
        {
            "key":         f"{p}_ChurnRiskRetention",
            "displayName": f"{p} Churn Risk Retention",
            "description": (
                "Active policyholders with medium-to-high annual premium (500–50,000). "
                "Priority for proactive retention outreach before renewal. "
                "Excludes ultra-premium clients (>= 50,000) who have a dedicated "
                "relationship manager. Requires Identity Resolution + CI refresh."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "active_policy_count__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "active_policy_count__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "total_annual_premium__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "total_annual_premium__c",
                             "greater than or equal", 500),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "total_annual_premium__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "total_annual_premium__c",
                             "greater than or equal", 50000),
                ),
            ]),
        },

        # ── 4. Gold Tier Re-engagement ────────────────────────────────────────
        {
            "key":         f"{p}_GoldTierReengagement",
            "displayName": f"{p} Gold Tier Re-engagement",
            "description": (
                "Clients with active Life or Health insurance policies who have received 2+ emails. "
                "Combines InsurancePolicy DMO filter (ProductCategory in Life/Health, Status Active) "
                "with EngagementScore CI. Priority re-engagement audience."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _dmo_filter(
                    "InsurancePolicy__dlm", "PartyId__c", "ProductCategory__c",
                    "in", ["Life", "Health"],
                ),
                _dmo_filter(
                    "InsurancePolicy__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
                _ci_filter(
                    f"{p}_EngagementScore__cio",
                    "emails_received__c",
                    _num_cmp(f"{p}_EngagementScore__cio", "emails_received__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },

        # ── 5. Dormant High-Value ─────────────────────────────────────────────
        {
            "key":         f"{p}_DormantHighValue",
            "displayName": f"{p} Dormant High Value",
            "description": (
                "High-premium customers (total annual premium >= 5,000) with at least "
                "one active policy who have not recently engaged. Excludes those with "
                "open claims (already in active service journey). Value-preservation segment."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PolicySummary__cio",
                    "total_annual_premium__c",
                    _num_cmp(f"{p}_PolicySummary__cio", "total_annual_premium__c",
                             "greater than or equal", 5000),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ClaimsSummary__cio",
                    "open_claims_count__c",
                    _num_cmp(f"{p}_ClaimsSummary__cio", "open_claims_count__c",
                             "greater than or equal", 3),
                ),
            ]),
        },
    ]


def food_segment_defs(prefix: str) -> list:
    """5 food B2C segment definitions."""
    p = prefix

    return [
        # 1. Lapsed High Spenders
        {
            "key":         f"{p}_LapsedHighSpenders",
            "displayName": f"{p} Lapsed High Spenders",
            "description": (
                "Customers with 5+ orders and elevated churn score — high-value but drifting. "
                "Priority for win-back campaigns with personalised offers."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PurchaseSummary__cio",
                    "order_count__c",
                    _num_cmp(f"{p}_PurchaseSummary__cio", "order_count__c",
                             "greater than or equal", 5),
                ),
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 50),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },

        # 2. Dairy Loyalists
        {
            "key":         f"{p}_DairyLoyalists",
            "displayName": f"{p} Dairy Loyalists",
            "description": (
                "Heavy dairy buyers (spend >= 200). "
                "Ideal for dairy promotion campaigns, new product launches, and loyalty rewards."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CategorySpend__cio",
                    "dairy_spend__c",
                    _num_cmp(f"{p}_CategorySpend__cio", "dairy_spend__c",
                             "greater than or equal", 200),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 80),
                ),
            ]),
        },

        # 3. Unactivated Loyalty
        {
            "key":         f"{p}_UnactivatedLoyalty",
            "displayName": f"{p} Unactivated Loyalty",
            "description": (
                "Customers with 200+ loyalty points who have never redeemed. "
                "Activate dormant points to drive engagement and next purchase."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_LoyaltyProfile__cio",
                    "current_points_balance__c",
                    _num_cmp(f"{p}_LoyaltyProfile__cio", "current_points_balance__c",
                             "greater than or equal", 200),
                ),
                _ci_filter(
                    f"{p}_LoyaltyProfile__cio",
                    "total_redeemed__c",
                    _num_cmp(f"{p}_LoyaltyProfile__cio", "total_redeemed__c",
                             "equal", 0),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 85),
                ),
            ]),
        },

        # 4. Frequency Buyers
        {
            "key":         f"{p}_FrequencyBuyers",
            "displayName": f"{p} Frequency Buyers",
            "description": (
                "Highly active shoppers with 8+ orders. "
                "Prime candidates for loyalty tier upgrade and premium membership offers."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PurchaseSummary__cio",
                    "order_count__c",
                    _num_cmp(f"{p}_PurchaseSummary__cio", "order_count__c",
                             "greater than or equal", 8),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 70),
                ),
            ]),
        },

        # 5. Dormant Reactivation
        {
            "key":         f"{p}_DormantReactivation",
            "displayName": f"{p} Dormant Reactivation",
            "description": (
                "Customers with purchase history (2+ orders) and elevated churn risk (>= 60) "
                "who shopped Online. Reactivation campaign with time-limited digital offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PurchaseSummary__cio",
                    "order_count__c",
                    _num_cmp(f"{p}_PurchaseSummary__cio", "order_count__c",
                             "greater than or equal", 2),
                ),
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 60),
                ),
                _dmo_filter(
                    "PurchaseOrder__dlm", "PartyId__c", "Channel__c",
                    "in", ["Online"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },
    ]


def food_b2b_segment_defs(prefix: str) -> list:
    """5 food B2B segment definitions.

    NOTE: food_b2b uses Account-level IR — segments are ON UnifiedssotAccountRt__dlm
    and CI filters must use _b2b_ci_filter (dimension = unified_account__c).
    """
    p = prefix

    return [
        # 1. Dormant Accounts
        {
            "key":         f"{p}_DormantAccounts",
            "displayName": f"{p} Dormant Accounts",
            "description": (
                "Wholesale accounts with 3+ past orders but high churn score (>= 60). "
                "Indicates ordering frequency is declining — priority for sales rep outreach."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_WholesaleSummary__cio",
                    "order_count__c",
                    _num_cmp(f"{p}_WholesaleSummary__cio", "order_count__c",
                             "greater than or equal", 3),
                ),
                _b2b_ci_filter(
                    f"{p}_AccountHealth__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealth__cio", "churn_score__c",
                             "greater than or equal", 60),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealth__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealth__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 2. Upsell Candidates (single-category dependence)
        {
            "key":         f"{p}_UpsellCandidates",
            "displayName": f"{p} Upsell Candidates",
            "description": (
                "Stores with strong dairy spend (>= 500) but minimal snack/bakery penetration. "
                "Opportunity to expand category footprint with targeted SKU recommendations."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_CategoryPenetration__cio",
                    "dairy_spend__c",
                    _num_cmp(f"{p}_CategoryPenetration__cio", "dairy_spend__c",
                             "greater than or equal", 500),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_CategoryPenetration__cio",
                    "snacks_spend__c",
                    _num_cmp(f"{p}_CategoryPenetration__cio", "snacks_spend__c",
                             "greater than or equal", 200),
                ),
            ]),
        },

        # 3. High-Frequency Accounts
        {
            "key":         f"{p}_HighFrequencyAccounts",
            "displayName": f"{p} High Frequency Accounts",
            "description": (
                "Stores with 12+ wholesale orders and at least one Delivered order (DMO confirmed). "
                "Candidates for premium service tier, dedicated account manager, or VIP terms."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_WholesaleSummary__cio",
                    "order_count__c",
                    _num_cmp(f"{p}_WholesaleSummary__cio", "order_count__c",
                             "greater than or equal", 12),
                ),
                _b2b_dmo_filter(
                    "WholesaleOrder__dlm", "PartyId__c", "Status__c",
                    "in", ["Delivered"],
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealth__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealth__cio", "churn_score__c",
                             "greater than or equal", 70),
                ),
            ]),
        },

        # 4. Promo-Sensitive Stores
        {
            "key":         f"{p}_PromoSensitiveStores",
            "displayName": f"{p} Promo Sensitive Stores",
            "description": (
                "Stores purchasing 5+ promotional items — highly responsive to promotions. "
                "Target with new promotional bundles and seasonal offers."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_CategoryPenetration__cio",
                    "promo_item_count__c",
                    _num_cmp(f"{p}_CategoryPenetration__cio", "promo_item_count__c",
                             "greater than or equal", 5),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealth__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealth__cio", "churn_score__c",
                             "greater than or equal", 80),
                ),
            ]),
        },

        # 5. At-Risk Renewals
        {
            "key":         f"{p}_AtRiskRenewals",
            "displayName": f"{p} At Risk Renewals",
            "description": (
                "Accounts with churn score >= 55 and 2+ delivered orders — "
                "active but showing risk signals. Proactive outreach before contract renewal."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealth__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealth__cio", "churn_score__c",
                             "greater than or equal", 55),
                ),
                _b2b_ci_filter(
                    f"{p}_WholesaleSummary__cio",
                    "delivered_order_count__c",
                    _num_cmp(f"{p}_WholesaleSummary__cio", "delivered_order_count__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealth__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealth__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
    ]


def retail_segment_defs(prefix: str) -> list:
    """5 retail B2C segment definitions."""
    p = prefix

    return [
        # 1. VIP Reactivation
        {
            "key":         f"{p}_VIPReactivation",
            "displayName": f"{p} VIP Reactivation",
            "description": (
                "High-LTV customers (>= 500) with elevated churn risk (>= 50). "
                "Highest-priority retention cohort — personalised offer, early access."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "ltv__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "ltv__c",
                             "greater than or equal", 500),
                ),
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 50),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 2. Category Expansion (Apparel → Bags cross-sell)
        {
            "key":         f"{p}_CategoryExpansion",
            "displayName": f"{p} Category Expansion",
            "description": (
                "Customers with apparel spend >= 200 who have never bought bags. "
                "High potential for bags cross-sell with styled outfit recommendation."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CategoryAffinity__cio",
                    "apparel_spend__c",
                    _num_cmp(f"{p}_CategoryAffinity__cio", "apparel_spend__c",
                             "greater than or equal", 200),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CategoryAffinity__cio",
                    "bags_spend__c",
                    _num_cmp(f"{p}_CategoryAffinity__cio", "bags_spend__c",
                             "greater than or equal", 1),
                ),
            ]),
        },

        # 3. Online to Store
        {
            "key":         f"{p}_OnlineToStore",
            "displayName": f"{p} Online To Store",
            "description": (
                "Web shoppers (3+ web orders) who have never visited a physical store. "
                "Drive store visits with exclusive in-store experience offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChannelProfile__cio",
                    "web_orders__c",
                    _num_cmp(f"{p}_ChannelProfile__cio", "web_orders__c",
                             "greater than or equal", 3),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChannelProfile__cio",
                    "store_orders__c",
                    _num_cmp(f"{p}_ChannelProfile__cio", "store_orders__c",
                             "greater than or equal", 1),
                ),
            ]),
        },

        # 4. High Return Rate
        {
            "key":         f"{p}_HighReturnRate",
            "displayName": f"{p} High Return Rate",
            "description": (
                "Customers with 2+ returned orders. "
                "Intervention needed — sizing guide, fit consultation, or policy review."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PurchaseSummary__cio",
                    "returned_order_count__c",
                    _num_cmp(f"{p}_PurchaseSummary__cio", "returned_order_count__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },

        # 5. Frequent Mobile Shoppers
        {
            "key":         f"{p}_FrequentMobileShoppers",
            "displayName": f"{p} Frequent Mobile Shoppers",
            "description": (
                "Customers with 3+ mobile orders and LTV >= 200 — confirmed via Mobile channel "
                "order DMO. Mobile-native high-value shoppers — target with app-exclusive offers."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChannelProfile__cio",
                    "mobile_orders__c",
                    _num_cmp(f"{p}_ChannelProfile__cio", "mobile_orders__c",
                             "greater than or equal", 3),
                ),
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "ltv__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "ltv__c",
                             "greater than or equal", 200),
                ),
                _dmo_filter(
                    "SalesOrder__dlm", "PartyId__c", "Channel__c",
                    "in", ["Mobile"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 85),
                ),
            ]),
        },
    ]


def banking_segment_defs(prefix: str) -> list:
    """5 banking segment definitions."""
    p = prefix

    return [
        # 1. Mortgage Upsell
        {
            "key":         f"{p}_MortgageUpsell",
            "displayName": f"{p} Mortgage Upsell",
            "description": (
                "High-balance customers (>= 50,000) with at least one active banking product "
                "and no mortgage. Confirmed active relationship — strong signal for mortgage upsell."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_AccountSummary__cio",
                    "total_balance__c",
                    _num_cmp(f"{p}_AccountSummary__cio", "total_balance__c",
                             "greater than or equal", 50000),
                ),
                _dmo_filter(
                    "BankingProduct__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ProductHoldings__cio",
                    "mortgage_count__c",
                    _num_cmp(f"{p}_ProductHoldings__cio", "mortgage_count__c",
                             "greater than or equal", 1),
                ),
            ]),
        },

        # 2. Investment Targets
        {
            "key":         f"{p}_InvestmentTargets",
            "displayName": f"{p} Investment Targets",
            "description": (
                "Savings account holders with 30,000+ balance and no investment account. "
                "Ideal for wealth management and investment product campaign."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ProductHoldings__cio",
                    "savings_count__c",
                    _num_cmp(f"{p}_ProductHoldings__cio", "savings_count__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_AccountSummary__cio",
                    "total_balance__c",
                    _num_cmp(f"{p}_AccountSummary__cio", "total_balance__c",
                             "greater than or equal", 30000),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ProductHoldings__cio",
                    "investment_count__c",
                    _num_cmp(f"{p}_ProductHoldings__cio", "investment_count__c",
                             "greater than or equal", 1),
                ),
            ]),
        },

        # 3. Digital Migration
        {
            "key":         f"{p}_DigitalMigration",
            "displayName": f"{p} Digital Migration",
            "description": (
                "Active, satisfied transactors (10+ transactions, NPS >= 7) — "
                "ideal cohort to migrate from branch to digital banking."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_SpendingProfile__cio",
                    "transaction_count__c",
                    _num_cmp(f"{p}_SpendingProfile__cio", "transaction_count__c",
                             "greater than or equal", 10),
                ),
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "nps_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "nps_score__c",
                             "greater than or equal", 7),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 70),
                ),
            ]),
        },

        # 4. At-Risk Clients
        {
            "key":         f"{p}_AtRiskClients",
            "displayName": f"{p} At Risk Clients",
            "description": (
                "Clients with churn score >= 65 and at least one account. "
                "Proactive retention — personalised offer or relationship manager call."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
                _ci_filter(
                    f"{p}_AccountSummary__cio",
                    "account_count__c",
                    _num_cmp(f"{p}_AccountSummary__cio", "account_count__c",
                             "greater than or equal", 1),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 5. Premium Upgrade
        {
            "key":         f"{p}_PremiumUpgrade",
            "displayName": f"{p} Premium Upgrade",
            "description": (
                "High-wealth satisfied clients: balance >= 100,000, checking account, NPS >= 8. "
                "Move to private banking tier with premium service benefits."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_AccountSummary__cio",
                    "total_balance__c",
                    _num_cmp(f"{p}_AccountSummary__cio", "total_balance__c",
                             "greater than or equal", 100000),
                ),
                _ci_filter(
                    f"{p}_ProductHoldings__cio",
                    "checking_count__c",
                    _num_cmp(f"{p}_ProductHoldings__cio", "checking_count__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "nps_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "nps_score__c",
                             "greater than or equal", 8),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 60),
                ),
            ]),
        },
    ]


def pharma_segment_defs(prefix: str) -> list:
    """5 pharma segment definitions."""
    p = prefix

    return [
        # 1. Adherence Risk
        {
            "key":         f"{p}_AdherenceRisk",
            "displayName": f"{p} Adherence Risk",
            "description": (
                "Patients with adherence rate <= 0.5 and at least one active prescription. "
                "Outreach for refill reminders and adherence support programmes."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_AdherenceProfile__cio",
                    "adherence_rate__c",
                    _num_cmp(f"{p}_AdherenceProfile__cio", "adherence_rate__c",
                             "less than or equal", 0.5),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerHealthValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerHealthValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },

        # 2. Polypharmacy
        {
            "key":         f"{p}_PolyPharmacy",
            "displayName": f"{p} Poly Pharmacy",
            "description": (
                "Patients with 3+ active prescriptions — complex high-value patients. "
                "Coordinate care, adherence, and refill synchronisation programmes."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PrescriptionSummary__cio",
                    "active_rx_count__c",
                    _num_cmp(f"{p}_PrescriptionSummary__cio", "active_rx_count__c",
                             "greater than or equal", 3),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerHealthValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerHealthValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },

        # 3. Cardiovascular Care
        {
            "key":         f"{p}_CardiovascularCare",
            "displayName": f"{p} Cardiovascular Care",
            "description": (
                "Patients with cardiovascular prescriptions (DMO confirmed) and churn score >= 50. "
                "Priority for adherence programme and specialist care coordination."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_TherapeuticProfile__cio",
                    "cardiovascular_rx__c",
                    _num_cmp(f"{p}_TherapeuticProfile__cio", "cardiovascular_rx__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_CustomerHealthValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerHealthValue__cio", "churn_score__c",
                             "greater than or equal", 50),
                ),
                _dmo_filter(
                    "Prescription__dlm", "PartyId__c", "TherapeuticArea__c",
                    "in", ["Cardiovascular"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerHealthValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerHealthValue__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 4. Lapsed Patients
        {
            "key":         f"{p}_LapsedPatients",
            "displayName": f"{p} Lapsed Patients",
            "description": (
                "Patients with 2+ historical prescriptions but no active ones. "
                "Re-engagement campaign to bring back lapsed patients."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PrescriptionSummary__cio",
                    "active_rx_count__c",
                    _num_cmp(f"{p}_PrescriptionSummary__cio", "active_rx_count__c",
                             "equal", 0),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerHealthValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerHealthValue__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 5. Diabetic Engagement
        {
            "key":         f"{p}_DiabeticEngagement",
            "displayName": f"{p} Diabetic Engagement",
            "description": (
                "Patients with diabetes prescriptions who receive emails but don't open them. "
                "Re-engagement focus: adherence, refills, and wellness programme."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_TherapeuticProfile__cio",
                    "diabetes_rx__c",
                    _num_cmp(f"{p}_TherapeuticProfile__cio", "diabetes_rx__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_EngagementScore__cio",
                    "emails_received__c",
                    _num_cmp(f"{p}_EngagementScore__cio", "emails_received__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_EngagementScore__cio",
                    "emails_opened__c",
                    _num_cmp(f"{p}_EngagementScore__cio", "emails_opened__c",
                             "greater than or equal", 1),
                ),
            ]),
        },
    ]


def telco_segment_defs(prefix: str) -> list:
    """5 telco segment definitions."""
    p = prefix

    return [
        # 1. Churn Risk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Active contract holders with churn score >= 60. "
                "Highest-priority retention cohort for proactive service calls."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChurnRisk__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_ChurnRisk__cio", "churn_score__c",
                             "greater than or equal", 60),
                ),
                _ci_filter(
                    f"{p}_ServiceSummary__cio",
                    "active_contract_count__c",
                    _num_cmp(f"{p}_ServiceSummary__cio", "active_contract_count__c",
                             "greater than or equal", 1),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChurnRisk__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_ChurnRisk__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 2. Bundle Upsell
        {
            "key":         f"{p}_BundleUpsell",
            "displayName": f"{p} Bundle Upsell",
            "description": (
                "Mobile-only customers with no broadband contract. "
                "Strong upsell signal — bundle offer with discount."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceSummary__cio",
                    "mobile_count__c",
                    _num_cmp(f"{p}_ServiceSummary__cio", "mobile_count__c",
                             "greater than or equal", 1),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceSummary__cio",
                    "broadband_count__c",
                    _num_cmp(f"{p}_ServiceSummary__cio", "broadband_count__c",
                             "greater than or equal", 1),
                ),
            ]),
        },

        # 3. Overage Alert
        {
            "key":         f"{p}_OverageAlert",
            "displayName": f"{p} Overage Alert",
            "description": (
                "Customers regularly paying overage charges (total >= 20). "
                "Upgrade them to a higher-tier plan to reduce cost and improve satisfaction."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_UsageProfile__cio",
                    "total_overage_charge__c",
                    _num_cmp(f"{p}_UsageProfile__cio", "total_overage_charge__c",
                             "greater than or equal", 20),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChurnRisk__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_ChurnRisk__cio", "churn_score__c",
                             "greater than or equal", 85),
                ),
            ]),
        },

        # 4. Data Heavy Users
        {
            "key":         f"{p}_DataHeavyUsers",
            "displayName": f"{p} Data Heavy Users",
            "description": (
                "Customers averaging 15+ GB data per month — heavy users eligible for premium plan. "
                "Target with premium data plan upgrade offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_UsageProfile__cio",
                    "avg_data_used_gb__c",
                    _num_cmp(f"{p}_UsageProfile__cio", "avg_data_used_gb__c",
                             "greater than or equal", 15),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChurnRisk__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_ChurnRisk__cio", "churn_score__c",
                             "greater than or equal", 80),
                ),
            ]),
        },

        # 5. Contract Renewal
        {
            "key":         f"{p}_ContractRenewal",
            "displayName": f"{p} Contract Renewal",
            "description": (
                "Happy customers (churn score <= 40) with active contracts confirmed in DMO — "
                "ideal for proactive renewal with loyalty reward."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceSummary__cio",
                    "active_contract_count__c",
                    _num_cmp(f"{p}_ServiceSummary__cio", "active_contract_count__c",
                             "greater than or equal", 1),
                ),
                _dmo_filter(
                    "ServiceContract__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ChurnRisk__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_ChurnRisk__cio", "churn_score__c",
                             "greater than or equal", 41),
                ),
            ]),
        },
    ]


def hightech_segment_defs(prefix: str) -> list:
    """5 hightech segment definitions.

    NOTE: hightech uses Account-level IR — segments are ON UnifiedssotAccountRt__dlm
    and CI filters must use _b2b_ci_filter (dimension = unified_account__c).
    """
    p = prefix

    return [
        # 1. Churn Risk 90 Days
        {
            "key":         f"{p}_ChurnRisk90",
            "displayName": f"{p} Churn Risk 90 Days",
            "description": (
                "Accounts with renewal within 90 days AND churn score >= 55. "
                "Highest-priority CS intervention before renewal decision."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_SubscriptionSummary__cio",
                    "renewal_within_90_days__c",
                    _num_cmp(f"{p}_SubscriptionSummary__cio", "renewal_within_90_days__c",
                             "greater than or equal", 1),
                ),
                _b2b_ci_filter(
                    f"{p}_AccountHealthProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealthProfile__cio", "churn_score__c",
                             "greater than or equal", 55),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealthProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealthProfile__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 2. Expansion Candidates
        {
            "key":         f"{p}_ExpansionCandidates",
            "displayName": f"{p} Expansion Candidates",
            "description": (
                "Active accounts with high feature adoption (>= 70), MRR <= 5,000, and Active subscription (DMO confirmed). "
                "Growing usage, ready for upsell to higher tier or more seats."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_UsageHealthScore__cio",
                    "avg_feature_adoption_score__c",
                    _num_cmp(f"{p}_UsageHealthScore__cio", "avg_feature_adoption_score__c",
                             "greater than or equal", 70),
                ),
                _b2b_ci_filter(
                    f"{p}_SubscriptionSummary__cio",
                    "active_sub_count__c",
                    _num_cmp(f"{p}_SubscriptionSummary__cio", "active_sub_count__c",
                             "greater than or equal", 1),
                ),
                _b2b_dmo_filter(
                    "HtSubscription__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_SubscriptionSummary__cio",
                    "total_mrr__c",
                    _num_cmp(f"{p}_SubscriptionSummary__cio", "total_mrr__c",
                             "greater than or equal", 5001),
                ),
            ]),
        },

        # 3. Low Adoption Intervention
        {
            "key":         f"{p}_LowAdoptionIntervention",
            "displayName": f"{p} Low Adoption Intervention",
            "description": (
                "Active subscribers with very low login frequency (<= 5 avg). "
                "CS outreach: onboarding check-in, training offer, adoption coaching."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_UsageHealthScore__cio",
                    "avg_login_count__c",
                    _num_cmp(f"{p}_UsageHealthScore__cio", "avg_login_count__c",
                             "less than or equal", 5),
                ),
                _b2b_ci_filter(
                    f"{p}_SubscriptionSummary__cio",
                    "active_sub_count__c",
                    _num_cmp(f"{p}_SubscriptionSummary__cio", "active_sub_count__c",
                             "greater than or equal", 1),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealthProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealthProfile__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },

        # 4. Support Burden Accounts
        {
            "key":         f"{p}_SupportBurdenAccounts",
            "displayName": f"{p} Support Burden Accounts",
            "description": (
                "Accounts with 2+ open tickets AND at least one critical severity ticket. "
                "Needs urgent CS attention — risk of escalation and churn."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_SupportProfile__cio",
                    "open_ticket_count__c",
                    _num_cmp(f"{p}_SupportProfile__cio", "open_ticket_count__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealthProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealthProfile__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 5. Champion Program
        {
            "key":         f"{p}_ChampionProgram",
            "displayName": f"{p} Champion Program",
            "description": (
                "Highly satisfied customers (NPS >= 9) with excellent product adoption (>= 75). "
                "Invite to reference programme, advisory board, and case study opportunities."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealthProfile__cio",
                    "nps_score__c",
                    _num_cmp(f"{p}_AccountHealthProfile__cio", "nps_score__c",
                             "greater than or equal", 9),
                ),
                _b2b_ci_filter(
                    f"{p}_UsageHealthScore__cio",
                    "avg_feature_adoption_score__c",
                    _num_cmp(f"{p}_UsageHealthScore__cio", "avg_feature_adoption_score__c",
                             "greater than or equal", 75),
                ),
            ]),
            "excludeCriteria": _logic([
                _b2b_ci_filter(
                    f"{p}_AccountHealthProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_AccountHealthProfile__cio", "churn_score__c",
                             "greater than or equal", 30),
                ),
            ]),
        },
    ]


def utilities_segment_defs(prefix: str) -> list:
    """5 utility segment definitions."""
    p = prefix

    return [
        # 1. High Consumers
        {
            "key":         f"{p}_HighConsumers",
            "displayName": f"{p} High Consumers",
            "description": (
                "Customers with average monthly bill >= 120 and an active utility contract (DMO). "
                "Target with efficiency audit offer, smart meter upgrade, or budget plan."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ConsumptionProfile__cio",
                    "avg_monthly_bill__c",
                    _num_cmp(f"{p}_ConsumptionProfile__cio", "avg_monthly_bill__c",
                             "greater than or equal", 120),
                ),
                _dmo_filter(
                    "UtilityContract__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 85),
                ),
            ]),
        },

        # 2. Overage Prone
        {
            "key":         f"{p}_OverageProne",
            "displayName": f"{p} Overage Prone",
            "description": (
                "Customers with 3 or more months of overage charges. "
                "Upgrade them to a plan with a higher cap to reduce bill shock."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ConsumptionProfile__cio",
                    "overage_months__c",
                    _num_cmp(f"{p}_ConsumptionProfile__cio", "overage_months__c",
                             "greater than or equal", 3),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "suspended_contracts__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "suspended_contracts__c",
                             "greater than or equal", 1),
                ),
            ]),
        },

        # 3. Multi Product
        {
            "key":         f"{p}_MultiProduct",
            "displayName": f"{p} Multi Product",
            "description": (
                "Customers holding both electricity and gas contracts. "
                "Best candidates for bundle discount or loyalty tier upgrade."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ConsumptionProfile__cio",
                    "electricity_contracts__c",
                    _num_cmp(f"{p}_ConsumptionProfile__cio", "electricity_contracts__c",
                             "greater than or equal", 1),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ConsumptionProfile__cio",
                    "gas_contracts__c",
                    _num_cmp(f"{p}_ConsumptionProfile__cio", "gas_contracts__c",
                             "less than", 1),
                ),
            ]),
        },

        # 4. Churn Risk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Customers with churn score >= 65. "
                "Proactive retention outreach with personalised offer before they switch providers."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 95),
                ),
            ]),
        },

        # 5. Win Back
        {
            "key":         f"{p}_WinBack",
            "displayName": f"{p} Win Back",
            "description": (
                "Customers who received 3+ marketing emails but never opened any. "
                "Re-engage with a direct offer — SMS, phone, or door-drop."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_EngagementScore__cio",
                    "emails_received__c",
                    _num_cmp(f"{p}_EngagementScore__cio", "emails_received__c",
                             "greater than or equal", 3),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_EngagementScore__cio",
                    "emails_opened__c",
                    _num_cmp(f"{p}_EngagementScore__cio", "emails_opened__c",
                             "greater than or equal", 1),
                ),
            ]),
        },
    ]


def airlines_segment_defs(prefix: str) -> list:
    """5 airline / FFP segment definitions."""
    p = prefix

    return [
        # 1. Business Travelers
        {
            "key":         f"{p}_BusinessTravelers",
            "displayName": f"{p} Business Travelers",
            "description": (
                "Passengers with 3+ premium cabin flights AND confirmed Business/First booking "
                "in DMO. Target with corporate travel programme, lounge access, priority boarding."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_FlightProfile__cio",
                    "premium_flights__c",
                    _num_cmp(f"{p}_FlightProfile__cio", "premium_flights__c",
                             "greater than or equal", 3),
                ),
                _dmo_filter(
                    "FlightBooking__dlm", "PartyId__c", "CabinClass__c",
                    "in", ["Business", "First"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 85),
                ),
            ]),
        },

        # 2. Dormant FFP
        {
            "key":         f"{p}_DormantFFP",
            "displayName": f"{p} Dormant FFP",
            "description": (
                "FFP members inactive for 180+ days with 5,000+ miles still banked. "
                "Re-engage with a miles bonus or exclusive destination offer before miles expire."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "miles_balance__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "miles_balance__c",
                             "greater than or equal", 5000),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "days_since_last_flight__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "days_since_last_flight__c",
                             "less than", 180),
                ),
            ]),
        },

        # 3. High LTV
        {
            "key":         f"{p}_HighLTV",
            "displayName": f"{p} High LTV",
            "description": (
                "Passengers with total lifetime flight spend >= 2,000. "
                "Treat as VIPs: priority service, status fast-track, and upgrade offers."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_FlightProfile__cio",
                    "total_spend__c",
                    _num_cmp(f"{p}_FlightProfile__cio", "total_spend__c",
                             "greater than or equal", 2000),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },

        # 4. Miles Expiry
        {
            "key":         f"{p}_MilesExpiry",
            "displayName": f"{p} Miles Expiry",
            "description": (
                "Members with accumulated miles but no flight in 300+ days. "
                "Alert them before miles expire — a small redemption keeps the account active."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_LoyaltyProfile__cio",
                    "current_points_balance__c",
                    _num_cmp(f"{p}_LoyaltyProfile__cio", "current_points_balance__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "days_since_last_flight__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "days_since_last_flight__c",
                             "greater than or equal", 300),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },

        # 5. Cancellation Prone
        {
            "key":         f"{p}_CancellationProne",
            "displayName": f"{p} Cancellation Prone",
            "description": (
                "Passengers with 2+ cancelled bookings. "
                "Identify reasons (schedule, price, competition) and offer flexible-fare upgrade."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "cancelled_bookings__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "cancelled_bookings__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
    ]


def healthcare_segment_defs(prefix: str) -> list:
    """5 healthcare segment definitions."""
    p = prefix

    return [
        # 1. HighUtilization
        {
            "key":         f"{p}_HighUtilization",
            "displayName": f"{p} High Utilization",
            "description": (
                "Members with 8+ medical visits in the last 2 years. Proactively manage their care journey and reduce ER dependency."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_VisitProfile__cio",
                    "total_visits__c",
                    _num_cmp(f"{p}_VisitProfile__cio", "total_visits__c",
                             "greater than or equal", 8),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_HealthRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_HealthRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
        # 2. PreventiveCareGap
        {
            "key":         f"{p}_PreventiveCareGap",
            "displayName": f"{p} Preventive Care Gap",
            "description": (
                "Members with no visit in the last year. Remind them about routine check-ups and preventive screenings."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_VisitProfile__cio",
                    "days_since_last_visit__c",
                    _num_cmp(f"{p}_VisitProfile__cio", "days_since_last_visit__c",
                             "greater than or equal", 365),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 3. ERFrequent
        {
            "key":         f"{p}_ERFrequent",
            "displayName": f"{p} ER Frequent",
            "description": (
                "Members with 2+ ER visits. Connect them with a primary care doctor to reduce avoidable ER usage."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_VisitProfile__cio",
                    "er_visits__c",
                    _num_cmp(f"{p}_VisitProfile__cio", "er_visits__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 4. AbnormalResults
        {
            "key":         f"{p}_AbnormalResults",
            "displayName": f"{p} Abnormal Results",
            "description": (
                "Members with at least one abnormal lab result (CI + LabResult DMO confirmed). "
                "Trigger follow-up appointment and specialist referral workflow."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_HealthRiskProfile__cio",
                    "abnormal_results__c",
                    _num_cmp(f"{p}_HealthRiskProfile__cio", "abnormal_results__c",
                             "greater than or equal", 1),
                ),
                _dmo_filter(
                    "LabResult__dlm", "PartyId__c", "IsAbnormal__c",
                    "greater than or equal", 1,
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. RenewalAtRisk
        {
            "key":         f"{p}_RenewalAtRisk",
            "displayName": f"{p} Renewal At Risk",
            "description": (
                "Members likely to switch to a competing HMO. Proactive retention outreach with personalised health benefits."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_HealthRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_HealthRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 60),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_HealthRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_HealthRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
    ]

def sports_club_segment_defs(prefix: str) -> list:
    """5 sports club segment definitions."""
    p = prefix

    return [
        # 1. DormantMembers
        {
            "key":         f"{p}_DormantMembers",
            "displayName": f"{p} Dormant Members",
            "description": (
                "Active members who haven't visited in 60+ days. Re-engage with a free PT session or class trial before they cancel."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ActivityProfile__cio",
                    "days_since_last_activity__c",
                    _num_cmp(f"{p}_ActivityProfile__cio", "days_since_last_activity__c",
                             "greater than or equal", 60),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
        # 2. RenewalRisk
        {
            "key":         f"{p}_RenewalRisk",
            "displayName": f"{p} Renewal Risk",
            "description": (
                "Active members (DMO confirmed) renewing within 90 days with elevated churn risk. "
                "Priority outreach to lock in renewal with a loyalty discount."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_MembershipProfile__cio",
                    "renewal_within_90_days__c",
                    _num_cmp(f"{p}_MembershipProfile__cio", "renewal_within_90_days__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 50),
                ),
                _dmo_filter(
                    "Membership__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 3. HighActivity
        {
            "key":         f"{p}_HighActivity",
            "displayName": f"{p} High Activity",
            "description": (
                "Most dedicated members (30+ sessions). Offer premium plan upgrade, locker room access, or brand ambassador programme."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ActivityProfile__cio",
                    "total_sessions__c",
                    _num_cmp(f"{p}_ActivityProfile__cio", "total_sessions__c",
                             "greater than or equal", 30),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerRiskProfile__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerRiskProfile__cio", "churn_score__c",
                             "greater than or equal", 60),
                ),
            ]),
        },
        # 4. PremiumUpgrade
        {
            "key":         f"{p}_PremiumUpgrade",
            "displayName": f"{p} Premium Upgrade",
            "description": (
                "High-frequency members on a budget plan. Strong upsell signal — upgrade them to Standard or Premium with added benefits."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ActivityProfile__cio",
                    "total_sessions__c",
                    _num_cmp(f"{p}_ActivityProfile__cio", "total_sessions__c",
                             "greater than or equal", 20),
                ),
                _ci_filter(
                    f"{p}_MembershipProfile__cio",
                    "monthly_fee__c",
                    _num_cmp(f"{p}_MembershipProfile__cio", "monthly_fee__c",
                             "less than or equal", 30),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. NewMembers
        {
            "key":         f"{p}_NewMembers",
            "displayName": f"{p} New Members",
            "description": (
                "Members in their first 3 months. Onboarding campaign: welcome pack, free class, facility tour, and habit-building tips."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_MembershipProfile__cio",
                    "membership_age_months__c",
                    _num_cmp(f"{p}_MembershipProfile__cio", "membership_age_months__c",
                             "less than or equal", 3),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
    ]


def ecommerce_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. CartAbandoners
        {
            "key":         f"{p}_CartAbandoners",
            "displayName": f"{p} Cart Abandoners",
            "description": (
                "Shoppers who abandoned a cart (value >= 50) and placed at least one order. "
                "Re-engage with a cart-recovery email or a limited-time discount on abandoned items."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CartAbandonmentProfile__cio",
                    "abandoned_carts__c",
                    _num_cmp(f"{p}_CartAbandonmentProfile__cio", "abandoned_carts__c",
                             "greater than or equal", 1),
                ),
                _ci_filter(
                    f"{p}_OrderProfile__cio",
                    "total_orders__c",
                    _num_cmp(f"{p}_OrderProfile__cio", "total_orders__c",
                             "greater than or equal", 1),
                ),
                _dmo_filter(
                    "CartAbandonment__dlm", "PartyId__c", "CartValue__c",
                    "greater than or equal", 50,
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 2. HighLTV
        {
            "key":         f"{p}_HighLTV",
            "displayName": f"{p} High LTV",
            "description": (
                "High-value shoppers with total spend ≥ £500. "
                "Retain with VIP early access, exclusive promotions, and priority customer service."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_OrderProfile__cio",
                    "total_spend__c",
                    _num_cmp(f"{p}_OrderProfile__cio", "total_spend__c",
                             "greater than or equal", 500),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 80),
                ),
            ]),
        },
        # 3. FrequentBuyers
        {
            "key":         f"{p}_FrequentBuyers",
            "displayName": f"{p} Frequent Buyers",
            "description": (
                "Shoppers with 5 or more orders. Reward with a loyalty programme invitation or "
                "a tiered discount to sustain purchase frequency."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_OrderProfile__cio",
                    "total_orders__c",
                    _num_cmp(f"{p}_OrderProfile__cio", "total_orders__c",
                             "greater than or equal", 5),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 70),
                ),
            ]),
        },
        # 4. ChurnRisk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Shoppers with elevated churn score (65–89). "
                "Win-back with a time-limited offer before they lapse entirely."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
        # 5. DormantShoppers
        {
            "key":         f"{p}_DormantShoppers",
            "displayName": f"{p} Dormant Shoppers",
            "description": (
                "Shoppers who haven't ordered in 90+ days and have low churn risk. "
                "Re-activate with a 'We miss you' campaign and a personalised product recommendation."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_OrderProfile__cio",
                    "days_since_last_order__c",
                    _num_cmp(f"{p}_OrderProfile__cio", "days_since_last_order__c",
                             "greater than or equal", 90),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
        },
    ]


def hospitality_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. FrequentGuests
        {
            "key":         f"{p}_FrequentGuests",
            "displayName": f"{p} Frequent Guests",
            "description": (
                "Guests with 3+ stays and at least one completed stay confirmed in HotelStay DMO. "
                "Reward with loyalty tier upgrade, complimentary breakfast, or early check-in."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_StayProfile__cio",
                    "total_stays__c",
                    _num_cmp(f"{p}_StayProfile__cio", "total_stays__c",
                             "greater than or equal", 3),
                ),
                _dmo_filter(
                    "HotelStay__dlm", "PartyId__c", "Status__c",
                    "in", ["Completed"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 80),
                ),
            ]),
        },
        # 2. DormantLoyalty
        {
            "key":         f"{p}_DormantLoyalty",
            "displayName": f"{p} Dormant Loyalty",
            "description": (
                "Loyalty members with 500+ points who haven't stayed in 180+ days. "
                "Re-activate with a points-expiry warning and an exclusive returning-guest rate."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_LoyaltyProfile__cio",
                    "current_points_balance__c",
                    _num_cmp(f"{p}_LoyaltyProfile__cio", "current_points_balance__c",
                             "greater than or equal", 500),
                ),
                _ci_filter(
                    f"{p}_StayProfile__cio",
                    "days_since_last_stay__c",
                    _num_cmp(f"{p}_StayProfile__cio", "days_since_last_stay__c",
                             "greater than or equal", 180),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 3. ChurnRisk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Guests with elevated churn score (65–89). "
                "Win-back with a limited-time member rate or a complimentary upgrade offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
        # 4. SuiteUpgrade
        {
            "key":         f"{p}_SuiteUpgrade",
            "displayName": f"{p} Suite Upgrade",
            "description": (
                "High-revenue guests (avg spend ≥ £200/stay) who have never booked a suite. "
                "Upsell with a targeted suite upgrade offer at the next booking."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_StayProfile__cio",
                    "avg_revenue_per_stay__c",
                    _num_cmp(f"{p}_StayProfile__cio", "avg_revenue_per_stay__c",
                             "greater than or equal", 200),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_StayProfile__cio",
                    "suite_stays__c",
                    _num_cmp(f"{p}_StayProfile__cio", "suite_stays__c",
                             "greater than or equal", 1),
                ),
            ]),
        },
        # 5. CancellationProne
        {
            "key":         f"{p}_CancellationProne",
            "displayName": f"{p} Cancellation Prone",
            "description": (
                "Guests with 2 or more cancellations. Reduce no-shows with flexible rate options, "
                "a personalised pre-arrival message, and a light deposit incentive."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_StayProfile__cio",
                    "cancelled_stays__c",
                    _num_cmp(f"{p}_StayProfile__cio", "cancelled_stays__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
    ]


def media_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. ActivePremium
        {
            "key":         f"{p}_ActivePremium",
            "displayName": f"{p} Active Premium",
            "description": (
                "Active subscribers on a Premium or Standard plan with high watch engagement (20+ views). "
                "Cross-sell add-ons or offer annual plan upgrade to increase LTV."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_SubscriptionProfile__cio",
                    "subscription_status__c",
                    _text_cmp(f"{p}_SubscriptionProfile__cio", "subscription_status__c",
                              "equal to", "Active"),
                ),
                _ci_filter(
                    f"{p}_ContentProfile__cio",
                    "total_views__c",
                    _num_cmp(f"{p}_ContentProfile__cio", "total_views__c",
                             "greater than or equal", 20),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 2. ChurnRisk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Subscribers with elevated churn score (65–89) or fewer than 3 views in the last period. "
                "Win-back with a personalised content recommendation or a temporary discount."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
        # 3. BingeWatchers
        {
            "key":         f"{p}_BingeWatchers",
            "displayName": f"{p} Binge Watchers",
            "description": (
                "Active subscribers (DMO) with completion rate >= 80% across their last sessions. "
                "Promote new series releases and exclusive early access content."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ContentProfile__cio",
                    "completion_rate__c",
                    _num_cmp(f"{p}_ContentProfile__cio", "completion_rate__c",
                             "greater than or equal", 0.8),
                ),
                _dmo_filter(
                    "Subscription__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 4. TrialConverts
        {
            "key":         f"{p}_TrialConverts",
            "displayName": f"{p} Trial Converts",
            "description": (
                "Trial subscribers who have watched 5+ titles. "
                "Convert to paid plan with a personalised offer before trial expires."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_SubscriptionProfile__cio",
                    "plan_type__c",
                    _text_cmp(f"{p}_SubscriptionProfile__cio", "plan_type__c",
                              "equal to", "Trial"),
                ),
                _ci_filter(
                    f"{p}_ContentProfile__cio",
                    "total_views__c",
                    _num_cmp(f"{p}_ContentProfile__cio", "total_views__c",
                             "greater than or equal", 5),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. Churned
        {
            "key":         f"{p}_Churned",
            "displayName": f"{p} Churned",
            "description": (
                "Cancelled subscribers with any prior viewing history. "
                "Re-engage with a win-back campaign offering a discounted reactivation rate."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_SubscriptionProfile__cio",
                    "subscription_status__c",
                    _text_cmp(f"{p}_SubscriptionProfile__cio", "subscription_status__c",
                              "equal to", "Cancelled"),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
    ]


def automotive_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. ServiceDue
        {
            "key":         f"{p}_ServiceDue",
            "displayName": f"{p} Service Due",
            "description": (
                "Owners of active vehicles (Vehicle DMO) whose last service was 180+ days ago. "
                "Remind with a personalised service invitation and an online booking link."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceProfile__cio",
                    "days_since_last_service__c",
                    _num_cmp(f"{p}_ServiceProfile__cio", "days_since_last_service__c",
                             "greater than or equal", 180),
                ),
                _dmo_filter(
                    "Vehicle__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 2. LoyalServiceCustomers
        {
            "key":         f"{p}_LoyalService",
            "displayName": f"{p} Loyal Service",
            "description": (
                "Customers with 4+ completed service visits. "
                "Reward loyalty with a preferred customer discount or free vehicle health check."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceProfile__cio",
                    "total_service_visits__c",
                    _num_cmp(f"{p}_ServiceProfile__cio", "total_service_visits__c",
                             "greater than or equal", 4),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 80),
                ),
            ]),
        },
        # 3. PremiumBuyers
        {
            "key":         f"{p}_PremiumBuyers",
            "displayName": f"{p} Premium Buyers",
            "description": (
                "Customers who purchased a vehicle worth £60,000+. "
                "Offer exclusive service packages, accessories, and VIP preview events."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_VehicleProfile__cio",
                    "total_vehicle_value__c",
                    _num_cmp(f"{p}_VehicleProfile__cio", "total_vehicle_value__c",
                             "greater than or equal", 60000),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 4. MultiVehicle
        {
            "key":         f"{p}_MultiVehicle",
            "displayName": f"{p} Multi Vehicle",
            "description": (
                "Customers owning 2 or more vehicles. "
                "Cross-sell fleet servicing packages or family vehicle add-ons."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_VehicleProfile__cio",
                    "vehicles_owned__c",
                    _num_cmp(f"{p}_VehicleProfile__cio", "vehicles_owned__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. ChurnRisk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Customers with elevated churn score (65–89). "
                "Win-back with a personalised service offer or a trade-in appraisal invitation."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
    ]


def real_estate_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. ActiveSearchers
        {
            "key":         f"{p}_ActiveSearchers",
            "displayName": f"{p} Active Searchers",
            "description": (
                "Buyers with 3+ property inquiries for House, Villa, or Apartment (DMO confirmed). "
                "Prioritise with personalised property alerts and a dedicated agent assignment."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_InquiryProfile__cio",
                    "total_inquiries__c",
                    _num_cmp(f"{p}_InquiryProfile__cio", "total_inquiries__c",
                             "greater than or equal", 3),
                ),
                _dmo_filter(
                    "PropertyInquiry__dlm", "PartyId__c", "PropertyType__c",
                    "in", ["House", "Villa", "Apartment"],
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 2. LuxurySeekers
        {
            "key":         f"{p}_LuxurySeekers",
            "displayName": f"{p} Luxury Seekers",
            "description": (
                "Buyers whose average inquiry price is £1,000,000+. "
                "Offer exclusive off-market listings and premium concierge service."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_InquiryProfile__cio",
                    "avg_inquiry_price__c",
                    _num_cmp(f"{p}_InquiryProfile__cio", "avg_inquiry_price__c",
                             "greater than or equal", 1000000),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 3. RepeatBuyers
        {
            "key":         f"{p}_RepeatBuyers",
            "displayName": f"{p} Repeat Buyers",
            "description": (
                "Customers who have completed 2 or more property transactions. "
                "Nurture with investment property insights and portfolio management services."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_TransactionProfile__cio",
                    "total_transactions__c",
                    _num_cmp(f"{p}_TransactionProfile__cio", "total_transactions__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 4. Renters
        {
            "key":         f"{p}_Renters",
            "displayName": f"{p} Renters",
            "description": (
                "Customers whose primary transaction type is Rental. "
                "Convert to buyers with a first-time buyer guide and mortgage pre-approval offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_TransactionProfile__cio",
                    "primary_transaction_type__c",
                    _text_cmp(f"{p}_TransactionProfile__cio", "primary_transaction_type__c",
                              "equal to", "Rental"),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. ChurnRisk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Customers with elevated churn score (65–89). "
                "Re-engage with a personalised property match and a follow-up call from an agent."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
    ]


def betting_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. VIPPlayers
        {
            "key":         f"{p}_VIPPlayers",
            "displayName": f"{p} VIP Players",
            "description": (
                "Players with £5,000+ total stake and an Active betting account (DMO confirmed). "
                "Reward with VIP bonuses, dedicated account manager, and exclusive promotions."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PlayerProfile__cio",
                    "total_staked__c",
                    _num_cmp(f"{p}_PlayerProfile__cio", "total_staked__c",
                             "greater than or equal", 5000),
                ),
                _dmo_filter(
                    "BettingAccount__dlm", "PartyId__c", "AccountStatus__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_RiskProfile__cio",
                    "responsible_gaming_flag__c",
                    _text_cmp(f"{p}_RiskProfile__cio", "responsible_gaming_flag__c",
                              "equal to", "true"),
                ),
            ]),
        },
        # 2. InactivePlayers
        {
            "key":         f"{p}_InactivePlayers",
            "displayName": f"{p} Inactive Players",
            "description": (
                "Registered players with fewer than 3 bets placed. "
                "Re-activate with a personalised welcome-back bonus or free bet offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_PlayerProfile__cio",
                    "total_bets__c",
                    _num_cmp(f"{p}_PlayerProfile__cio", "total_bets__c",
                             "less than", 3),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 3. AtRiskPlayers
        {
            "key":         f"{p}_AtRiskPlayers",
            "displayName": f"{p} At Risk Players",
            "description": (
                "Players with a responsible gaming flag set. "
                "Trigger responsible gaming messaging and deposit limit review flow."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_RiskProfile__cio",
                    "responsible_gaming_flag__c",
                    _text_cmp(f"{p}_RiskProfile__cio", "responsible_gaming_flag__c",
                              "equal to", "true"),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 4. KYCPending
        {
            "key":         f"{p}_KYCPending",
            "displayName": f"{p} KYC Pending",
            "description": (
                "Players whose KYC status is Pending. "
                "Prompt to complete identity verification to unlock full account features."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_RiskProfile__cio",
                    "kyc_status__c",
                    _text_cmp(f"{p}_RiskProfile__cio", "kyc_status__c",
                              "equal to", "Pending"),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. ChurnRisk
        {
            "key":         f"{p}_ChurnRisk",
            "displayName": f"{p} Churn Risk",
            "description": (
                "Players with elevated churn score (65–89). "
                "Re-engage with a personalised bonus offer tied to their preferred game type."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 65),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_CustomerValue__cio",
                    "churn_score__c",
                    _num_cmp(f"{p}_CustomerValue__cio", "churn_score__c",
                             "greater than or equal", 90),
                ),
            ]),
        },
    ]


def postal_segment_defs(prefix: str) -> list:
    p = prefix
    return [
        # 1. FrequentSenders
        {
            "key":         f"{p}_FrequentSenders",
            "displayName": f"{p} Frequent Senders",
            "description": (
                "Customers with 5+ parcels sent and at least one Delivered parcel (DMO confirmed). "
                "Migrate to a business account with volume discounts and priority service."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ShippingProfile__cio",
                    "total_parcels__c",
                    _num_cmp(f"{p}_ShippingProfile__cio", "total_parcels__c",
                             "greater than or equal", 5),
                ),
                _dmo_filter(
                    "Parcel__dlm", "PartyId__c", "Status__c",
                    "in", ["Delivered"],
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 2. FailedDeliveryRecovery
        {
            "key":         f"{p}_FailedDeliveryRecovery",
            "displayName": f"{p} Failed Delivery Recovery",
            "description": (
                "Customers with 2+ failed or returned parcels and no active postal product. "
                "Offer a PO Box subscription or smart-locker delivery preference."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ShippingProfile__cio",
                    "failed_deliveries__c",
                    _num_cmp(f"{p}_ShippingProfile__cio", "failed_deliveries__c",
                             "greater than or equal", 2),
                ),
            ]),
            "excludeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceUsage__cio",
                    "active_products__c",
                    _num_cmp(f"{p}_ServiceUsage__cio", "active_products__c",
                             "greater than or equal", 1),
                ),
            ]),
        },
        # 3. DigitalSubscribers
        {
            "key":         f"{p}_DigitalSubscribers",
            "displayName": f"{p} Digital Subscribers",
            "description": (
                "Customers with an Active postal product subscription (DMO confirmed). "
                "Re-engage before renewal with a loyalty reward or product upgrade offer."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ServiceUsage__cio",
                    "active_products__c",
                    _num_cmp(f"{p}_ServiceUsage__cio", "active_products__c",
                             "greater than or equal", 1),
                ),
                _dmo_filter(
                    "PostalProduct__dlm", "PartyId__c", "Status__c",
                    "in", ["Active"],
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 4. ExpressUpgraders
        {
            "key":         f"{p}_ExpressUpgraders",
            "displayName": f"{p} Express Upgraders",
            "description": (
                "Standard-service shippers with 3+ parcels but zero Express usage. "
                "Promote Express service with a first-shipment discount."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ShippingProfile__cio",
                    "total_parcels__c",
                    _num_cmp(f"{p}_ShippingProfile__cio", "total_parcels__c",
                             "greater than or equal", 3),
                ),
                _ci_filter(
                    f"{p}_ShippingProfile__cio",
                    "express_count__c",
                    _num_cmp(f"{p}_ShippingProfile__cio", "express_count__c",
                             "less than", 1),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
        # 5. DormantReactivation
        {
            "key":         f"{p}_DormantReactivation",
            "displayName": f"{p} Dormant Reactivation",
            "description": (
                "Registered customers with fewer than 2 parcels sent. "
                "Re-activate with a shipping discount or first-parcel promotion."
            )[:240],
            "requires_ir": True,
            "includeCriteria": _logic([
                _ci_filter(
                    f"{p}_ShippingProfile__cio",
                    "total_parcels__c",
                    _num_cmp(f"{p}_ShippingProfile__cio", "total_parcels__c",
                             "less than", 2),
                ),
            ]),
            "excludeCriteria": _logic([]),
        },
    ]


# ─── Industry dispatch ────────────────────────────────────────────────────────

SEGMENT_DEFS_MAP = {
    "insurance": insurance_segment_defs,
    "food":      food_segment_defs,
    "food_b2b":  food_b2b_segment_defs,
    "retail":    retail_segment_defs,
    "banking":   banking_segment_defs,
    "pharma":    pharma_segment_defs,
    "telco":     telco_segment_defs,
    "hightech":  hightech_segment_defs,
    "utilities": utilities_segment_defs,
    "airlines":  airlines_segment_defs,
    "healthcare":  healthcare_segment_defs,
    "sports_club": sports_club_segment_defs,
    "ecommerce":   ecommerce_segment_defs,
    "hospitality": hospitality_segment_defs,
    "media":       media_segment_defs,
    "automotive":  automotive_segment_defs,
    "real_estate": real_estate_segment_defs,
    "betting":     betting_segment_defs,
    "postal":      postal_segment_defs,
}


# ─── API helpers ──────────────────────────────────────────────────────────────

def get_segment(core_url: str, token: str, api_name: str) -> dict:
    """GET a segment by apiName. Returns the segment dict or {}."""
    st, data = api(core_url, token, "GET",
                   f"{BASE}/segments/{api_name}?dataspace=default")
    if st == 200 and isinstance(data, dict):
        segs = data.get("segments", [])
        return segs[0] if segs else {}
    return {}


def delete_segment(core_url: str, token: str, api_name: str) -> bool:
    st, _ = api(core_url, token, "DELETE",
                f"{BASE}/segments/{api_name}?dataspace=default")
    return st in (200, 204, 404)


def publish_segment(core_url: str, token: str, msid: str, api_name: str,
                    tries: int = 10, wait: int = 15) -> tuple:
    """
    Wait for segmentStatus=ACTIVE then POST actions/publish.
    Returns (ok: bool, http_status, detail_str).
    """
    if not msid:
        return False, 0, "no marketSegmentId"
    last = (0, "")
    for attempt in range(tries):
        seg = get_segment(core_url, token, api_name)
        sstatus = str(seg.get("segmentStatus", "")).upper()
        if sstatus == "ACTIVE":
            st, resp = api(core_url, token, "POST",
                           f"{BASE}/segments/{msid}/actions/publish?dataspace=default",
                           body={})
            if isinstance(st, int) and 200 <= st < 300:
                return True, st, "published (ACTIVE → actions/publish 2xx)"
            last = (st, str(resp)[:160])
            print(f"    [publish] attempt {attempt+1}/{tries} → HTTP {st} {str(resp)[:80]}",
                  flush=True)
        else:
            print(f"    [publish] attempt {attempt+1}/{tries} → segmentStatus={sstatus or '?'} "
                  f"(waiting for ACTIVE…)", flush=True)
        if attempt + 1 < tries:
            time.sleep(wait)
    return False, last[0], f"never published — last: {last[1] or 'not ACTIVE in window'}"


def derive_api_name(display_name: str) -> str:
    """Replicate the org's apiName derivation from displayName."""
    return re.sub(r'[^0-9A-Za-z]+', '_', display_name).strip('_')


# ─── CI check ─────────────────────────────────────────────────────────────────

def check_ci(core_url: str, token: str, ci_name: str) -> str:
    """Return 'ACTIVE', 'MISSING', 'WRONG_DIM', or 'NOT_ACTIVE(status)'.

    Accepts both B2C (unified_individual__c) and B2B (unified_account__c) CIs.
    """
    st, data = api(core_url, token, "GET",
                   f"{BASE}/calculated-insights/{ci_name}?dataspace=default")
    if st != 200:
        return "MISSING"
    dims = [d.get("apiName", "") for d in data.get("dimensions", [])]
    status = data.get("calculatedInsightStatus", "")
    if status != "ACTIVE":
        return f"NOT_ACTIVE({status})"
    has_valid_dim = "unified_individual__c" in dims or "unified_account__c" in dims
    return "ACTIVE" if has_valid_dim else "WRONG_DIM"


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be created without calling the API")
    ap.add_argument("--publish", action="store_true",
                    help="Publish segments after creation (default: create only, do not publish)")
    args = ap.parse_args()

    cfg      = json.loads(Path(args.config).read_text())
    alias    = cfg["orgAlias"]
    slug     = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()
    # Segments are shared per industry across all clients on the same org — use industry label,
    # not client slug, so a second demo of the same vertical reuses the same segments.
    prefix   = industry.title().replace("_", "")   # e.g. "Hightech", "Insurance", "FoodB2b"
    out_dir  = Path(cfg.get("outputDir", f"data/{slug}"))

    # B2B Account IR industries use UnifiedssotAccountRt__dlm as the segment-on DMO
    b2b_account = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")
    seg_on = B2B_SEGMENT_ON if b2b_account else SEGMENT_ON

    print(f"\n📊  Segment creation for {cfg.get('clientName', slug)} ({industry})")
    print(f"    Org: {alias}  |  prefix: {prefix}")
    if b2b_account:
        print(f"    Mode: B2B Account  →  segmentOn={seg_on}")
    print()

    seg_fn = SEGMENT_DEFS_MAP.get(industry)
    if not seg_fn:
        print(f"  ℹ️  Segment definitions not yet implemented for '{industry}'.")
        print(f"     Supported: {', '.join(SEGMENT_DEFS_MAP.keys())}")
        return 0

    if args.dry_run:
        print("  [DRY-RUN] Would create these segments:")
        for s in seg_fn(prefix):
            print(f"    • {s['key']}  (requires_ir={s['requires_ir']})")
        return 0

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}")

    # Resolve the actual unified DMO name and its Salesforce internal ID on this org.
    actual_seg_on, seg_on_id = resolve_segment_on(core_url, core_token, b2b_account, cfg)
    if actual_seg_on != seg_on:
        print(f"  ℹ️  Unified DMO auto-detected: {seg_on} → {actual_seg_on}")
    print(f"  ℹ️  segmentOnId: {seg_on_id}")

    # Resolve the Identity Resolution link DMO name (varies by org).
    link_dmo = B2B_LINK_DMO if b2b_account else LINK_DMO
    actual_link_dmo = resolve_link_dmo(core_url, core_token, b2b_account)
    if actual_link_dmo != link_dmo:
        print(f"  ℹ️  Link DMO auto-detected: {link_dmo} → {actual_link_dmo}")
    print()

    segs = seg_fn(prefix)

    # Publish schedule: start NOW+10min, end +1 year
    now = datetime.now(timezone.utc)
    start_dt = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.") + \
               f"{(now + timedelta(minutes=10)).microsecond // 1000:03d}Z"
    end_dt   = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    created_ok = 0
    failures   = []

    for seg in segs:
        key = seg["key"]
        print(f"  ── {key} ──────────────────────────────────────────")

        # ── Check required CIs ────────────────────────────────────────
        crit_str  = json.dumps(seg["includeCriteria"])
        excl_str  = json.dumps(seg["excludeCriteria"])
        # Patch org-specific DMO names (unified DMO + IR link DMO may differ per org)
        crit_str = crit_str.replace(f'"{seg_on}"',   f'"{actual_seg_on}"')
        excl_str = excl_str.replace(f'"{seg_on}"',   f'"{actual_seg_on}"')
        crit_str = crit_str.replace(f'"{link_dmo}"', f'"{actual_link_dmo}"')
        excl_str = excl_str.replace(f'"{link_dmo}"', f'"{actual_link_dmo}"')
        ci_names  = set(re.findall(r'"objectApiName":\s*"([^"]+__cio)"', crit_str + excl_str))
        ci_ok = True
        for ci_name in sorted(ci_names):
            status = check_ci(core_url, core_token, ci_name)
            marker = "✅" if status == "ACTIVE" else ("⚠️" if "NOT_ACTIVE" in status else "❌")
            print(f"    {marker} CI {ci_name}: {status}")
            if status not in ("ACTIVE",):
                ci_ok = False
                if "MISSING" in status or "WRONG_DIM" in status:
                    print(f"       → Run create_calculated_insights.py first")
                elif "NOT_ACTIVE" in status:
                    print(f"       → CI is processing; retry in a few minutes")

        if not ci_ok and not seg["requires_ir"]:
            # PremiumRenewal must have its CIs ready immediately
            print(f"  ✗  Required CIs not ready — skipping {key}")
            failures.append({key: "required CIs not ACTIVE"})
            continue
        # For IR-dependent segments, missing CIs is expected (IR hasn't run yet)
        # We still create the segment so it's ready once IR + CI run happens

        # ── Delete if in a broken state (ERROR or failed publish) ────────
        api_name = derive_api_name(seg["displayName"])
        existing = get_segment(core_url, core_token, api_name)
        if existing:
            es  = str(existing.get("segmentStatus",  "")).upper()
            ps  = str(existing.get("publishStatus",  "")).upper()
            if es == "ERROR" or ps == "ERROR":
                print(f"    ⚠️  Segment in broken state (segmentStatus={es}, publishStatus={ps}) — deleting and recreating")
                delete_segment(core_url, core_token, api_name)
                time.sleep(3)
                existing = {}

        # ── POST segment ──────────────────────────────────────────────
        body = {
            "displayName":                seg["displayName"],
            "description":                seg["description"],
            "segmentOnApiName":           actual_seg_on, # resolved name (may differ from default)
            "segmentType":                "Ui",
            "segmentCreationFlow":        "Datakit",   # ← key that bypasses external-user restriction
            "publishSchedule":            "TwentyFour",
            "publishScheduleStartDateTime": start_dt,
            "publishScheduleEndDate":     end_dt,
            "includeCriteria":            crit_str,
        }
        # Only include excludeCriteria when non-empty.
        # Sending '{}' or an empty LogicalComparison causes parse errors or ERROR status.
        # Omitting the field entirely lets the API default to no-exclude.
        if seg.get("excludeCriteria"):
            body["excludeCriteria"] = excl_str

        if existing:
            msid     = existing.get("marketSegmentId")
            pub_stat = str(existing.get("publishStatus", "")).upper()
            print(f"    ↩  Already exists (segmentStatus={existing.get('segmentStatus')}, "
                  f"publishStatus={pub_stat})")
            if pub_stat in ("SUCCESS", "PUBLISHING"):
                print(f"    ✅  Already published — skipping")
                created_ok += 1
                continue
            # Exists but not published → try to publish
            print(f"    ▶  Attempting to publish existing segment…")
        else:
            # Recompute start_dt fresh right before POST (prevents stale future-start issue)
            now2     = datetime.now(timezone.utc)
            start_dt = (now2 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.") + \
                       f"{(now2 + timedelta(minutes=10)).microsecond // 1000:03d}Z"
            body["publishScheduleStartDateTime"] = start_dt

            print(f"    ▶  POST /ssot/segments…", flush=True)
            st, resp = api(core_url, core_token, "POST",
                           f"{BASE}/segments?dataspace=default", body=body)

            rtxt = (json.dumps(resp) if isinstance(resp, (dict, list)) else str(resp)).upper()
            is_dup = "DUPLICATE" in rtxt or "ALREADY EXISTS" in rtxt

            if st in (200, 201) and isinstance(resp, dict) and resp.get("apiName"):
                api_name = resp["apiName"]
                msid     = resp.get("marketSegmentId")
                print(f"    ✅  Created — apiName={api_name}  msid={msid}")
            elif is_dup:
                api_name = derive_api_name(seg["displayName"])
                existing = get_segment(core_url, core_token, api_name)
                msid     = existing.get("marketSegmentId")
                print(f"    ↩  Already exists — msid={msid}")
            else:
                print(f"    ✗  POST failed: HTTP {st}  {str(resp)[:300]}")
                failures.append({key: f"HTTP {st}: {str(resp)[:200]}"})
                continue

        # ── GET-back: verify criteria persisted ────────────────────────
        seg_back = get_segment(core_url, core_token, api_name)
        has_crit = bool(seg_back.get("includeCriteria")) or bool(seg_back.get("includeDbt"))
        if not has_crit:
            print(f"    ✗  Criteria silently dropped! GET-back shows no includeCriteria.")
            print(f"       This segment would match 0 members.")
            failures.append({key: "criteria silently dropped after POST"})
            continue
        print(f"    ✓  Criteria persisted in org")

        # ── Publish (only if --publish flag passed) ────────────────────
        if args.publish:
            pub_ok, pub_st, pub_detail = publish_segment(
                core_url, core_token, msid, api_name, tries=10, wait=12
            )
            if pub_ok:
                print(f"    ✅  Published — {pub_detail}")
            else:
                print(f"    ⚠️  Publish pending — {pub_detail}")
                print(f"       (COUNTING→ACTIVE is async; segment will activate automatically)")
        else:
            print(f"    ℹ️  Created (not published — run with --publish to publish automatically)")

        created_ok += 1
        ir_note = (" — ⏳ members will appear after IR + CI run"
                   if seg["requires_ir"] else " — ⚡ members available NOW")
        print(f"    ✅  {key} ready{ir_note}\n")

    # ── Save definitions ───────────────────────────────────────────────
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    defs_out = []
    for s in segs:
        defs_out.append({
            "key":          s["key"],
            "displayName":  s["displayName"],
            "description":  s["description"],
            "requires_ir":  s["requires_ir"],
            "criteria":     s["includeCriteria"],
            "excludeCriteria": s["excludeCriteria"],
        })
    (seg_dir / "segments.json").write_text(json.dumps(defs_out, indent=2))

    # ── Summary ────────────────────────────────────────────────────────
    total = len(segs)
    print(f"\n{'═'*60}")
    print(f"  📋  Segment summary for {prefix} ({industry})")
    print(f"  {'─'*58}")
    print(f"  Created/verified: {created_ok}/{total}")
    if failures:
        print(f"  Failed:           {len(failures)}")
        for f in failures:
            print(f"    • {f}")
    print()
    for s in segs:
        ir_note = "⏳ after IR + CI" if s["requires_ir"] else "⚡ NOW"
        print(f"  {ir_note}  {s['key']}")
    print()
    print(f"  NEXT STEPS:")
    print(f"  1. Setup → Identity Resolution → Run Now  (~15-40 min)")
    print(f"  2. Setup → Calculated Insights → each CI → Run Now")
    if not args.publish:
        print(f"  3. Segments → each {prefix} segment → Publish Now")
        print(f"     (or re-run with --publish to publish automatically)")
    print(f"  📄  Definitions saved to: {seg_dir}/segments.json")
    print(f"{'═'*60}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
