#!/usr/bin/env python3
"""
Create Calculated Insights (CIs) for seeded demo data.

Usage:
    python3 create_calculated_insights.py --config config.json [--dry-run]

Proven POST body format (Data Cloud API v62.0, 2026-06-24):
  apiName                 — e.g. "Migdal_PolicySummary__cio"  (MUST end with __cio)
  displayName             — human label
  description             — optional
  definitionType          — "CALCULATED_METRIC"
  publishScheduleInterval — "SIX"  (every 6 hours — DAILY/HOURLY/NOT_SCHEDULED all rejected)
                            "SYSTEM_MANAGED" also works but gives no refresh guarantee.
  expression              — full SQL (full DMO names, no table aliases)
  → dimensions / measures NOT in POST body — auto-derived from expression
  → dataSpace NOT in POST body — passed as ?dataspace=default query param

CRITICAL — Unified Individual join pattern (required for use in Segment Builder):
  All CIs that should be usable in segments MUST group by
  UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c
  and join through UnifiedLinkssotIndividualRt__dlm:

    FROM UnifiedssotIndividualRt__dlm
    JOIN UnifiedLinkssotIndividualRt__dlm
      ON UnifiedssotIndividualRt__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c
    JOIN <CustomDMO>
      ON <CustomDMO>.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c
    GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c

  The link table fields (proven 2026-06-24):
    UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c  → unified profile ID
    UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c   → raw source individual ID
                                                          (= PartyId__c FK in custom DMOs)

Behavior: if a CI with the same apiName exists, it is DELETED then RECREATED to ensure
  the SQL (and therefore the unified_individual__c dimension) is up to date.

Fallback: if POST fails, SQL files are written to <outputDir>/calculated_insights/*.sql
for manual creation via Data Cloud Setup → Calculated Insights → New.
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

# ─── CI Definitions ────────────────────────────────────────────────────────────
# SQL rules (proven to work):
#   - Use full DMO names (e.g. InsurancePolicy__dlm.PartyId__c) — NO table aliases
#   - Column aliases MUST end with __c  (e.g. AS policy_count__c)
#   - Aggregates: COUNT(), SUM(), AVG(), CASE WHEN ... END all supported
#   - MAX() / MIN() only on Number and Date, NOT on Text fields
#   - SUM(CASE WHEN field = 'X' THEN 1 ELSE 0 END) for conditional counts
#   - NEVER use DATEDIFF or date math functions
#
# "demo_use" is printed at the end and saved in the SQL file header — not sent to API

# Unified individual join pattern (proven 2026-06-24):
#   FROM UnifiedssotIndividualRt__dlm
#   JOIN UnifiedLinkssotIndividualRt__dlm
#     ON UnifiedssotIndividualRt__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c
#   JOIN <DMO> ON <DMO>.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c
#   GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c
# This makes the CI usable in Segment Builder (requires unified_individual__c dimension).
_UNIFIED_JOINS = (
    "FROM UnifiedssotIndividualRt__dlm\n"
    "JOIN UnifiedLinkssotIndividualRt__dlm\n"
    "    ON UnifiedssotIndividualRt__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c\n"
)

# B2B Account join pattern (food_b2b and hightech).
# configurationType="account" creates UnifiedssotAccountRt__dlm (not UnifiedIndividual).
# All B2B CIs MUST group by UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c.
_B2B_UNIFIED_JOINS = (
    "FROM UnifiedssotAccountRt__dlm\n"
    "JOIN UnifiedLinkssotAccountRt__dlm\n"
    "    ON UnifiedssotAccountRt__dlm.ssot__Id__c = UnifiedLinkssotAccountRt__dlm.UnifiedRecordId__c\n"
)

# ─── EngagementScore SQL template (identical for all industries) ───────────────
# Uses ssot__EmailEngagement__dlm (platform standard DMO) with ssot__IndividualId__c FK.
# Custom fields OpenedCount__c / ClickedCount__c are added via extend_standard_dmo().
_ENGAGEMENT_SQL = (
    "SELECT\n"
    "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
    "    COUNT(ssot__EmailEngagement__dlm.ssot__Id__c) AS emails_received__c,\n"
    "    SUM(ssot__EmailEngagement__dlm.OpenedCount__c) AS emails_opened__c,\n"
    "    SUM(ssot__EmailEngagement__dlm.ClickedCount__c) AS emails_clicked__c\n"
    + _UNIFIED_JOINS +
    "JOIN ssot__EmailEngagement__dlm\n"
    "    ON ssot__EmailEngagement__dlm.ssot__IndividualId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
    "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
)

_ENGAGEMENT_SQL_FALLBACK = (
    "SELECT\n"
    "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
    "    COUNT(ssot__EmailEngagement__dlm.ssot__Id__c) AS emails_received__c\n"
    + _UNIFIED_JOINS +
    "JOIN ssot__EmailEngagement__dlm\n"
    "    ON ssot__EmailEngagement__dlm.ssot__IndividualId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
    "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
)

_ENGAGEMENT_CI = {
    "key":         "EngagementScore",
    "displayName": "{prefix} Email Engagement",
    "description": (
        "Email engagement per unified individual: emails received, opened, clicked. "
        "Find unreachable customers for reactivation."
    ),
    "sql":          _ENGAGEMENT_SQL,
    "sql_fallback": _ENGAGEMENT_SQL_FALLBACK,
    "demo_use": (
        "Unreachable: emails_received__c > 0 AND emails_opened__c = 0  ·  "
        "Win-back: emails_received__c >= 5 AND emails_opened__c = 0  ·  "
        "VIP engaged: emails_opened__c / emails_received__c > 0.6"
    ),
}


# B2B Account engagement CI — same metric shape as _ENGAGEMENT_CI but grouped by Account.
# Uses ssot__EmailEngagement__dlm.ssot__IndividualId__c which stores account source record ID in B2B.
_B2B_ENGAGEMENT_SQL = (
    "SELECT\n"
    "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
    "    COUNT(ssot__EmailEngagement__dlm.ssot__Id__c) AS emails_received__c,\n"
    "    SUM(ssot__EmailEngagement__dlm.OpenedCount__c) AS emails_opened__c,\n"
    "    SUM(ssot__EmailEngagement__dlm.ClickedCount__c) AS emails_clicked__c\n"
    + _B2B_UNIFIED_JOINS +
    "JOIN ssot__EmailEngagement__dlm\n"
    "    ON ssot__EmailEngagement__dlm.ssot__IndividualId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
    "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
)

_B2B_ENGAGEMENT_SQL_FALLBACK = (
    "SELECT\n"
    "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
    "    COUNT(ssot__EmailEngagement__dlm.ssot__Id__c) AS emails_received__c\n"
    + _B2B_UNIFIED_JOINS +
    "JOIN ssot__EmailEngagement__dlm\n"
    "    ON ssot__EmailEngagement__dlm.ssot__IndividualId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
    "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
)

_B2B_ENGAGEMENT_CI = {
    "key":         "EngagementScore",
    "displayName": "{prefix} Email Engagement",
    "description": (
        "Email engagement per unified B2B account: emails received, opened, clicked. "
        "Find unreachable accounts for reactivation."
    ),
    "sql":          _B2B_ENGAGEMENT_SQL,
    "sql_fallback": _B2B_ENGAGEMENT_SQL_FALLBACK,
    "demo_use": (
        "Unreachable accounts: emails_received__c > 0 AND emails_opened__c = 0  ·  "
        "Win-back: emails_received__c >= 5 AND emails_opened__c = 0"
    ),
}


# ─── LoyaltyProfile CI templates ─────────────────────────────────────────────
# Reused for food B2C, banking, and retail (B2C Individual model).
_LOYALTY_PROFILE_CI = {
    "key":         "LoyaltyProfile",
    "displayName": "{prefix} Loyalty Profile",
    "description": (
        "Loyalty points balance, earn/redeem history. "
        "Powers tier-based loyalty campaign segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(LoyaltyTransaction__dlm.Balance__c) AS current_points_balance__c,\n"
        "    SUM(CASE WHEN LoyaltyTransaction__dlm.TransactionType__c = 'earn' THEN LoyaltyTransaction__dlm.Points__c ELSE 0 END) AS total_earned__c,\n"
        "    SUM(CASE WHEN LoyaltyTransaction__dlm.TransactionType__c = 'redeem' THEN 1 ELSE 0 END) AS total_redeemed__c,\n"
        "    COUNT(LoyaltyTransaction__dlm.Id__c) AS transaction_count__c\n"
        + _UNIFIED_JOINS +
        "JOIN LoyaltyTransaction__dlm\n"
        "    ON LoyaltyTransaction__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": "Unactivated: current_points_balance__c >= 200 AND total_redeemed__c = 0",
}

# B2B (food_b2b) version — groups by Account not Individual.
_B2B_LOYALTY_PROFILE_CI = {
    "key":         "LoyaltyProfile",
    "displayName": "{prefix} Loyalty Profile",
    "description": (
        "Loyalty points balance and earn/redeem history per B2B account. "
        "Powers account-level loyalty program and tier segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
        "    MAX(LoyaltyTransaction__dlm.Balance__c) AS current_points_balance__c,\n"
        "    SUM(CASE WHEN LoyaltyTransaction__dlm.TransactionType__c = 'earn' THEN LoyaltyTransaction__dlm.Points__c ELSE 0 END) AS total_earned__c,\n"
        "    SUM(CASE WHEN LoyaltyTransaction__dlm.TransactionType__c = 'redeem' THEN 1 ELSE 0 END) AS total_redeemed__c,\n"
        "    COUNT(LoyaltyTransaction__dlm.Id__c) AS transaction_count__c\n"
        + _B2B_UNIFIED_JOINS +
        "JOIN LoyaltyTransaction__dlm\n"
        "    ON LoyaltyTransaction__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
    ),
    "demo_use": "High-value accounts: current_points_balance__c >= 500 AND total_redeemed__c = 0",
}


# ─── INSURANCE CIs ────────────────────────────────────────────────────────────

# Fallback for CustomerRiskProfile when ssot__Individual__dlm enrichment fields
# (ChurnScore__c, Ltv__c, NpsScore__c) are not yet mapped/ingested on the org.
# Happens on orgs where the full pipeline was run before Phase 2 field additions.
# Fresh orgs (create_dmos → create_mappings → upload → create_cis) won't need this.
_INSURANCE_RISK_FALLBACK = (
    "SELECT\n"
    "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
    "    COUNT(InsurancePolicy__dlm.Id__c) AS policy_count__c,\n"
    "    SUM(CASE WHEN InsurancePolicy__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_policy_count__c,\n"
    "    SUM(InsurancePolicy__dlm.PremiumAnnual__c) AS total_annual_premium__c\n"
    + _UNIFIED_JOINS +
    "JOIN InsurancePolicy__dlm\n"
    "    ON InsurancePolicy__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
    "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
)

INSURANCE_CIS = [
    {
        "key":         "PolicySummary",
        "displayName": "{prefix} Policy Summary",
        "description": (
            "Policy count, total annual premium, and coverage per unified individual. "
            "Powers value-based segmentation and upsell targeting."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(InsurancePolicy__dlm.Id__c) AS policy_count__c,\n"
            "    SUM(CASE WHEN InsurancePolicy__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_policy_count__c,\n"
            "    SUM(InsurancePolicy__dlm.PremiumAnnual__c) AS total_annual_premium__c,\n"
            "    AVG(InsurancePolicy__dlm.PremiumAnnual__c) AS avg_annual_premium__c,\n"
            "    SUM(InsurancePolicy__dlm.CoverageAmount__c) AS total_coverage_amount__c\n"
            + _UNIFIED_JOINS +
            "JOIN InsurancePolicy__dlm\n"
            "    ON InsurancePolicy__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": (
            "Segment: 'Premium customers' = total_annual_premium__c > 15,000  ·  "
            "Upsell: active_policy_count__c = 1 AND loyalty_tier = Gold"
        ),
    },
    {
        "key":         "ClaimsSummary",
        "displayName": "{prefix} Claims Summary",
        "description": (
            "Claims count, open claims, and amounts per unified individual. "
            "Identify high-risk customers and proactively manage claims experience."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(InsuranceClaim__dlm.Id__c) AS claims_count__c,\n"
            "    SUM(CASE WHEN InsuranceClaim__dlm.Status__c = 'Open' THEN 1 ELSE 0 END) AS open_claims_count__c,\n"
            "    SUM(InsuranceClaim__dlm.ClaimAmount__c) AS total_claimed_amount__c,\n"
            "    AVG(InsuranceClaim__dlm.ClaimAmount__c) AS avg_claim_amount__c\n"
            + _UNIFIED_JOINS +
            "JOIN InsuranceClaim__dlm\n"
            "    ON InsuranceClaim__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": (
            "Risk flag: open_claims_count__c >= 2  ·  "
            "High-cost: total_claimed_amount__c > 50,000  ·  "
            "Loss ratio: join with PolicySummary for claimed/premium ratio"
        ),
    },
    _ENGAGEMENT_CI,
    {
        "key":         "CustomerRiskProfile",
        "displayName": "{prefix} Customer Risk Profile",
        "description": (
            "Master retention CI: combines policy data with churn score, LTV, and NPS "
            "from IndividualProfile. Unified individual dimension — usable in Segment Builder."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,\n"
            "    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,\n"
            "    COUNT(InsurancePolicy__dlm.Id__c) AS policy_count__c,\n"
            "    SUM(CASE WHEN InsurancePolicy__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_policy_count__c,\n"
            "    SUM(InsurancePolicy__dlm.PremiumAnnual__c) AS total_annual_premium__c\n"
            + _UNIFIED_JOINS +
            "JOIN InsurancePolicy__dlm\n"
            "    ON InsurancePolicy__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Individual__dlm\n"
            "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "sql_fallback": _INSURANCE_RISK_FALLBACK,
        "demo_use": (
            "THE retention segment: churn_score__c > 60 "
            "AND active_policy_count__c >= 2 AND total_annual_premium__c > 10000 "
            "→ proactive call before renewal"
        ),
    },
    {
        "key":         "PolicyTypeBreakdown",
        "displayName": "{prefix} Policy Type Breakdown",
        "description": (
            "Policy count and premium by product category per unified individual. "
            "Two-dimension CI — enables cross-sell by missing product category."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    InsurancePolicy__dlm.ProductCategory__c AS product_category__c,\n"
            "    COUNT(InsurancePolicy__dlm.Id__c) AS policy_count__c,\n"
            "    SUM(InsurancePolicy__dlm.PremiumAnnual__c) AS total_premium_by_category__c\n"
            + _UNIFIED_JOINS +
            "JOIN InsurancePolicy__dlm\n"
            "    ON InsurancePolicy__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c, InsurancePolicy__dlm.ProductCategory__c"
        ),
        "demo_use": (
            "Cross-sell: customer has Life but NOT Health  ·  "
            "Segment: product_category__c = 'Vehicle' to promote telematics add-on"
        ),
    },
]


# ─── FOOD B2C CIs ────────────────────────────────────────────────────────────

FOOD_B2C_CIS = [
    {
        "key":         "PurchaseSummary",
        "displayName": "{prefix} Purchase Summary",
        "description": (
            "Order count, total spend, and average basket per individual. "
            "Powers recency-based and value-based segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(PurchaseOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(PurchaseOrder__dlm.TotalAmount__c) AS total_spend__c,\n"
            "    AVG(PurchaseOrder__dlm.TotalAmount__c) AS avg_basket__c,\n"
            "    SUM(PurchaseOrder__dlm.LoyaltyPointsEarned__c) AS total_points_earned__c\n"
            + _UNIFIED_JOINS +
            "JOIN PurchaseOrder__dlm\n"
            "    ON PurchaseOrder__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Frequent buyers: order_count__c >= 8  ·  High-value: total_spend__c > 500",
    },
    {
        "key":         "CategorySpend",
        "displayName": "{prefix} Category Spend Profile",
        "description": (
            "Spend by product category (Dairy, Meat, Bakery, Produce, Beverages, Snacks). "
            "Powers dietary preference and cross-category promotion segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Dairy' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS dairy_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Meat' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS meat_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Bakery' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS bakery_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Produce' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS produce_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Beverages' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS beverages_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Snacks' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS snacks_spend__c\n"
            + _UNIFIED_JOINS +
            "JOIN OrderLine__dlm\n"
            "    ON OrderLine__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Dairy loyalists: dairy_spend__c > 200  ·  Cross-sell Snacks to Dairy-only buyers",
    },
    {
        "key":         "LoyaltyProfile",
        "displayName": "{prefix} Loyalty Profile",
        "description": (
            "Loyalty points balance, earn/redeem history. "
            "Powers tier-based loyalty campaign segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(LoyaltyTransaction__dlm.Balance__c) AS current_points_balance__c,\n"
            "    SUM(CASE WHEN LoyaltyTransaction__dlm.TransactionType__c = 'earn' THEN LoyaltyTransaction__dlm.Points__c ELSE 0 END) AS total_earned__c,\n"
            "    SUM(CASE WHEN LoyaltyTransaction__dlm.TransactionType__c = 'redeem' THEN 1 ELSE 0 END) AS total_redeemed__c,\n"
            "    COUNT(LoyaltyTransaction__dlm.Id__c) AS transaction_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN LoyaltyTransaction__dlm\n"
            "    ON LoyaltyTransaction__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Unactivated: current_points_balance__c >= 200 AND total_redeemed__c = 0",
    },
    {
        "key":         "CustomerValue",
        "displayName": "{prefix} Customer Value",
        "description": (
            "Combines churn score, LTV, NPS from profile with purchase metrics. "
            "The master retention CI for food B2C."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,\n"
            "    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,\n"
            "    COUNT(PurchaseOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(PurchaseOrder__dlm.TotalAmount__c) AS total_spend__c\n"
            + _UNIFIED_JOINS +
            "JOIN PurchaseOrder__dlm\n"
            "    ON PurchaseOrder__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Individual__dlm\n"
            "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Lapsed high-value: order_count__c >= 5 AND churn_score__c >= 50",
    },
    _ENGAGEMENT_CI,
]


# ─── RETAIL CIs ───────────────────────────────────────────────────────────────

RETAIL_CIS = [
    {
        "key":         "PurchaseSummary",
        "displayName": "{prefix} Purchase Summary",
        "description": (
            "Order count, total spend, and return rate per individual. "
            "Powers recency and value segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(SalesOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(SalesOrder__dlm.TotalAmount__c) AS total_spend__c,\n"
            "    AVG(SalesOrder__dlm.TotalAmount__c) AS avg_order_value__c,\n"
            "    SUM(CASE WHEN SalesOrder__dlm.Status__c = 'Returned' THEN 1 ELSE 0 END) AS returned_order_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN SalesOrder__dlm\n"
            "    ON SalesOrder__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "High return rate: returned_order_count__c >= 2  ·  High-value: total_spend__c > 1000",
    },
    {
        "key":         "CategoryAffinity",
        "displayName": "{prefix} Category Affinity",
        "description": (
            "Spend by fashion category. "
            "Powers cross-sell and personalized product recommendation segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Bags' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS bags_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Shoes' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS shoes_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Apparel' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS apparel_spend__c,\n"
            "    SUM(CASE WHEN OrderLine__dlm.Category__c = 'Accessories' THEN OrderLine__dlm.LineTotal__c ELSE 0 END) AS accessories_spend__c\n"
            + _UNIFIED_JOINS +
            "JOIN OrderLine__dlm\n"
            "    ON OrderLine__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Cross-sell: apparel_spend__c >= 200 AND bags_spend__c = 0",
    },
    {
        "key":         "ChannelProfile",
        "displayName": "{prefix} Channel Profile",
        "description": (
            "Purchase channel breakdown. "
            "Powers omnichannel experience and store-to-digital migration segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    SUM(CASE WHEN SalesOrder__dlm.Channel__c = 'Web' THEN 1 ELSE 0 END) AS web_orders__c,\n"
            "    SUM(CASE WHEN SalesOrder__dlm.Channel__c = 'Store' THEN 1 ELSE 0 END) AS store_orders__c,\n"
            "    SUM(CASE WHEN SalesOrder__dlm.Channel__c = 'Mobile' THEN 1 ELSE 0 END) AS mobile_orders__c\n"
            + _UNIFIED_JOINS +
            "JOIN SalesOrder__dlm\n"
            "    ON SalesOrder__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Online-only: web_orders__c >= 3 AND store_orders__c = 0 → invite to in-store event",
    },
    {
        "key":         "CustomerValue",
        "displayName": "{prefix} Customer Value",
        "description": (
            "Master retention CI: combines churn risk, LTV, NPS with purchase history."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,\n"
            "    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,\n"
            "    COUNT(SalesOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(SalesOrder__dlm.TotalAmount__c) AS total_spend__c\n"
            + _UNIFIED_JOINS +
            "JOIN SalesOrder__dlm\n"
            "    ON SalesOrder__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Individual__dlm\n"
            "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "VIP at-risk: ltv__c >= 500 AND churn_score__c >= 50",
    },
    _LOYALTY_PROFILE_CI,
    _ENGAGEMENT_CI,
]


# ─── BANKING CIs ──────────────────────────────────────────────────────────────

BANKING_CIS = [
    {
        "key":         "AccountSummary",
        "displayName": "{prefix} Account Summary",
        "description": (
            "Account portfolio summary: count, active accounts, total and average balance. "
            "Powers wealth segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(FinancialAccount__dlm.Id__c) AS account_count__c,\n"
            "    SUM(CASE WHEN FinancialAccount__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_account_count__c,\n"
            "    SUM(FinancialAccount__dlm.Balance__c) AS total_balance__c,\n"
            "    AVG(FinancialAccount__dlm.Balance__c) AS avg_balance__c\n"
            + _UNIFIED_JOINS +
            "JOIN FinancialAccount__dlm\n"
            "    ON FinancialAccount__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "High-wealth: total_balance__c >= 100000  ·  Multi-account: account_count__c >= 3",
    },
    {
        "key":         "ProductHoldings",
        "displayName": "{prefix} Product Holdings",
        "description": (
            "Financial product holdings per individual. "
            "Powers cross-sell (e.g. no mortgage → mortgage campaign)."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    SUM(CASE WHEN FinancialAccount__dlm.AccountType__c = 'Checking' THEN 1 ELSE 0 END) AS checking_count__c,\n"
            "    SUM(CASE WHEN FinancialAccount__dlm.AccountType__c = 'Savings' THEN 1 ELSE 0 END) AS savings_count__c,\n"
            "    SUM(CASE WHEN FinancialAccount__dlm.AccountType__c = 'Investment' THEN 1 ELSE 0 END) AS investment_count__c,\n"
            "    SUM(CASE WHEN FinancialAccount__dlm.AccountType__c = 'Credit' THEN 1 ELSE 0 END) AS credit_count__c,\n"
            "    SUM(CASE WHEN FinancialAccount__dlm.AccountType__c = 'Mortgage' THEN 1 ELSE 0 END) AS mortgage_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN FinancialAccount__dlm\n"
            "    ON FinancialAccount__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Mortgage upsell: savings_count__c >= 1 AND mortgage_count__c = 0 AND total_balance >= 30000",
    },
    {
        "key":         "SpendingProfile",
        "displayName": "{prefix} Spending Profile",
        "description": (
            "Transaction-level spending profile by category. "
            "Powers behavioral and merchant-category segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(Transaction__dlm.Id__c) AS transaction_count__c,\n"
            "    SUM(CASE WHEN Transaction__dlm.Amount__c < 0 THEN Transaction__dlm.Amount__c ELSE 0 END) AS total_spend__c,\n"
            "    AVG(Transaction__dlm.Amount__c) AS avg_transaction__c,\n"
            "    SUM(CASE WHEN Transaction__dlm.Category__c = 'Groceries' THEN 1 ELSE 0 END) AS groceries_spend__c,\n"
            "    SUM(CASE WHEN Transaction__dlm.Category__c = 'Dining' THEN 1 ELSE 0 END) AS dining_spend__c\n"
            + _UNIFIED_JOINS +
            "JOIN Transaction__dlm\n"
            "    ON Transaction__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Active transactors: transaction_count__c >= 10  ·  Dining spend for dining rewards offer",
    },
    {
        "key":         "CustomerRiskProfile",
        "displayName": "{prefix} Customer Risk Profile",
        "description": (
            "Master retention CI: combines churn risk, LTV, NPS with account balance data."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,\n"
            "    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,\n"
            "    SUM(FinancialAccount__dlm.Balance__c) AS total_balance__c,\n"
            "    COUNT(FinancialAccount__dlm.Id__c) AS account_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN FinancialAccount__dlm\n"
            "    ON FinancialAccount__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Individual__dlm\n"
            "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "At-risk clients: churn_score__c >= 65 AND account_count__c >= 1",
    },
    _LOYALTY_PROFILE_CI,
    _ENGAGEMENT_CI,
]


# ─── PHARMA CIs ───────────────────────────────────────────────────────────────

PHARMA_CIS = [
    {
        "key":         "PrescriptionSummary",
        "displayName": "{prefix} Prescription Summary",
        "description": (
            "Prescription portfolio: total, active, discontinued, and expired prescriptions per patient. "
            "Powers adherence and re-engagement segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(Prescription__dlm.Id__c) AS rx_count__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_rx_count__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.Status__c = 'Discontinued' THEN 1 ELSE 0 END) AS discontinued_rx_count__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.Status__c = 'Expired' THEN 1 ELSE 0 END) AS expired_rx_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN Prescription__dlm\n"
            "    ON Prescription__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Lapsed patients: rx_count__c >= 2 AND active_rx_count__c = 0",
    },
    {
        "key":         "TherapeuticProfile",
        "displayName": "{prefix} Therapeutic Profile",
        "description": (
            "Therapeutic area breakdown. "
            "Powers condition-specific campaign and cross-therapy opportunity segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.TherapeuticArea__c = 'Cardiovascular' THEN 1 ELSE 0 END) AS cardiovascular_rx__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.TherapeuticArea__c = 'Diabetes' THEN 1 ELSE 0 END) AS diabetes_rx__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.TherapeuticArea__c = 'Respiratory' THEN 1 ELSE 0 END) AS respiratory_rx__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.TherapeuticArea__c = 'Pain Relief' THEN 1 ELSE 0 END) AS pain_rx__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.TherapeuticArea__c = 'Psychiatry' THEN 1 ELSE 0 END) AS psychiatry_rx__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.TherapeuticArea__c = 'Gastroenterology' THEN 1 ELSE 0 END) AS gastro_rx__c\n"
            + _UNIFIED_JOINS +
            "JOIN Prescription__dlm\n"
            "    ON Prescription__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Cardiovascular patients: cardiovascular_rx__c >= 1  ·  Diabetic segment: diabetes_rx__c >= 1",
    },
    {
        "key":         "AdherenceProfile",
        "displayName": "{prefix} Adherence Profile",
        "description": (
            "Prescription adherence metrics. "
            "Key indicator for pharmacy loyalty and patient outcome programs."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    AVG(CASE WHEN Prescription__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS adherence_rate__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_rx_count__c,\n"
            "    COUNT(Prescription__dlm.Id__c) AS total_rx__c\n"
            + _UNIFIED_JOINS +
            "JOIN Prescription__dlm\n"
            "    ON Prescription__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Low adherence: adherence_rate__c <= 0.5 AND active_rx_count__c >= 1",
    },
    {
        "key":         "CustomerHealthValue",
        "displayName": "{prefix} Customer Health Value",
        "description": (
            "Master patient value CI: combines loyalty risk, LTV, NPS with prescription history."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,\n"
            "    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,\n"
            "    COUNT(Prescription__dlm.Id__c) AS rx_count__c,\n"
            "    SUM(CASE WHEN Prescription__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_rx_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN Prescription__dlm\n"
            "    ON Prescription__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Individual__dlm\n"
            "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Cardiovascular churn: cardiovascular_rx >= 1 AND churn_score__c >= 50",
    },
    _ENGAGEMENT_CI,
]


# ─── TELCO CIs ────────────────────────────────────────────────────────────────

TELCO_CIS = [
    {
        "key":         "ServiceSummary",
        "displayName": "{prefix} Service Summary",
        "description": (
            "Service contract portfolio: active contracts, total MRR, and product type breakdown. "
            "Powers bundle upsell segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    COUNT(ServiceContract__dlm.Id__c) AS contract_count__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_contract_count__c,\n"
            "    SUM(ServiceContract__dlm.MonthlyFee__c) AS total_monthly_fee__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.PlanType__c = 'Mobile' THEN 1 ELSE 0 END) AS mobile_count__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.PlanType__c = 'Broadband' THEN 1 ELSE 0 END) AS broadband_count__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.PlanType__c = 'TV' THEN 1 ELSE 0 END) AS tv_count__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.PlanType__c = 'Bundle' THEN 1 ELSE 0 END) AS bundle_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN ServiceContract__dlm\n"
            "    ON ServiceContract__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Bundle upsell: mobile_count__c >= 1 AND broadband_count__c = 0",
    },
    {
        "key":         "UsageProfile",
        "displayName": "{prefix} Usage Profile",
        "description": (
            "Monthly usage aggregates: data, voice, SMS, and overage. "
            "Powers plan upgrade and overage alert segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    AVG(UsageRecord__dlm.DataUsedGb__c) AS avg_data_used_gb__c,\n"
            "    AVG(UsageRecord__dlm.VoiceMinutesUsed__c) AS avg_voice_minutes__c,\n"
            "    AVG(UsageRecord__dlm.SmsCount__c) AS avg_sms_count__c,\n"
            "    SUM(UsageRecord__dlm.OverageCharge__c) AS total_overage_charge__c\n"
            + _UNIFIED_JOINS +
            "JOIN UsageRecord__dlm\n"
            "    ON UsageRecord__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Overage payers: total_overage_charge__c >= 20 → plan upgrade campaign",
    },
    {
        "key":         "ChurnRisk",
        "displayName": "{prefix} Churn Risk",
        "description": (
            "Telco churn risk profile: combines churn score, NPS, and contract data for proactive retention."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,\n"
            "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_contract_count__c,\n"
            "    SUM(ServiceContract__dlm.MonthlyFee__c) AS total_monthly_fee__c\n"
            + _UNIFIED_JOINS +
            "JOIN ServiceContract__dlm\n"
            "    ON ServiceContract__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Individual__dlm\n"
            "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "At-risk: churn_score__c >= 60 AND active_contract_count__c >= 1",
    },
    {
        "key":         "ProductBundle",
        "displayName": "{prefix} Product Bundle",
        "description": (
            "Product bundle indicator per individual. "
            "Powers bundle completeness scoring and upsell targeting."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
            "    MAX(CASE WHEN ServiceContract__dlm.PlanType__c = 'Mobile' THEN 1 ELSE 0 END) AS has_mobile__c,\n"
            "    MAX(CASE WHEN ServiceContract__dlm.PlanType__c = 'Broadband' THEN 1 ELSE 0 END) AS has_broadband__c,\n"
            "    MAX(CASE WHEN ServiceContract__dlm.PlanType__c = 'TV' THEN 1 ELSE 0 END) AS has_tv__c,\n"
            "    SUM(CASE WHEN ServiceContract__dlm.PlanType__c = 'Bundle' THEN 1 ELSE 0 END) AS bundle_count__c\n"
            + _UNIFIED_JOINS +
            "JOIN ServiceContract__dlm\n"
            "    ON ServiceContract__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Add broadband: has_mobile__c = 1 AND has_broadband__c = 0",
    },
    _ENGAGEMENT_CI,
]


# ─── FOOD B2B CIs ─────────────────────────────────────────────────────────────
# IMPORTANT: food_b2b uses Account-level IR (configurationType="account").
# All CIs MUST group by UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c
# and join through UnifiedLinkssotAccountRt__dlm — NOT the Individual tables.

FOOD_B2B_CIS = [
    {
        "key":         "WholesaleSummary",
        "displayName": "{prefix} Wholesale Summary",
        "description": (
            "Wholesale order summary per store account. "
            "Powers account-level revenue segmentation and dormancy detection."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    COUNT(WholesaleOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(WholesaleOrder__dlm.TotalAmount__c) AS total_revenue__c,\n"
            "    AVG(WholesaleOrder__dlm.TotalAmount__c) AS avg_order_value__c,\n"
            "    SUM(CASE WHEN WholesaleOrder__dlm.Status__c = 'Delivered' THEN 1 ELSE 0 END) AS delivered_order_count__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN WholesaleOrder__dlm\n"
            "    ON WholesaleOrder__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Dormant accounts: order_count__c >= 3 AND churn_score >= 60",
    },
    {
        "key":         "CategoryPenetration",
        "displayName": "{prefix} Category Penetration",
        "description": (
            "Product category penetration per store account. "
            "Powers SKU gap analysis and promotional effectiveness segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    SUM(CASE WHEN WholesaleOrderLine__dlm.Category__c = 'Dairy' THEN WholesaleOrderLine__dlm.LineTotal__c ELSE 0 END) AS dairy_spend__c,\n"
            "    SUM(CASE WHEN WholesaleOrderLine__dlm.Category__c = 'Bakery' THEN WholesaleOrderLine__dlm.LineTotal__c ELSE 0 END) AS bakery_spend__c,\n"
            "    SUM(CASE WHEN WholesaleOrderLine__dlm.Category__c = 'Meat' THEN WholesaleOrderLine__dlm.LineTotal__c ELSE 0 END) AS meat_spend__c,\n"
            "    SUM(CASE WHEN WholesaleOrderLine__dlm.Category__c = 'Produce' THEN WholesaleOrderLine__dlm.LineTotal__c ELSE 0 END) AS produce_spend__c,\n"
            "    SUM(CASE WHEN WholesaleOrderLine__dlm.Category__c = 'Snacks' THEN WholesaleOrderLine__dlm.LineTotal__c ELSE 0 END) AS snacks_spend__c,\n"
            "    SUM(WholesaleOrderLine__dlm.IsPromotional__c) AS promo_item_count__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN WholesaleOrderLine__dlm\n"
            "    ON WholesaleOrderLine__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Upsell: dairy_spend__c >= 500 AND snacks_spend__c = 0 → expand category",
    },
    {
        "key":         "AccountHealth",
        "displayName": "{prefix} Account Health",
        "description": (
            "B2B account health: combines churn risk, LTV, and order history. "
            "The master retention CI for wholesale accounts."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    MAX(ssot__Account__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Account__dlm.Ltv__c) AS ltv__c,\n"
            "    MAX(ssot__Account__dlm.NpsScore__c) AS nps_score__c,\n"
            "    COUNT(WholesaleOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(WholesaleOrder__dlm.TotalAmount__c) AS total_revenue__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN WholesaleOrder__dlm\n"
            "    ON WholesaleOrder__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Account__dlm\n"
            "    ON ssot__Account__dlm.ssot__Id__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "At-risk renewals: churn_score__c >= 55 AND delivered_order_count >= 2",
    },
    {
        "key":         "OrderFrequency",
        "displayName": "{prefix} Order Frequency",
        "description": (
            "Order frequency and fulfilment quality per store account. "
            "Powers at-risk and high-frequency account segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    COUNT(WholesaleOrder__dlm.Id__c) AS order_count__c,\n"
            "    SUM(CASE WHEN WholesaleOrder__dlm.Status__c = 'Delivered' THEN 1 ELSE 0 END) AS delivered_count__c,\n"
            "    SUM(CASE WHEN WholesaleOrder__dlm.Status__c = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled_count__c,\n"
            "    SUM(WholesaleOrder__dlm.ItemCount__c) AS total_items__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN WholesaleOrder__dlm\n"
            "    ON WholesaleOrder__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "High-frequency: order_count__c >= 12  ·  Promo-sensitive: promo_item_count >= 5",
    },
    _B2B_LOYALTY_PROFILE_CI,
    _B2B_ENGAGEMENT_CI,
]


# ─── HIGHTECH CIs ─────────────────────────────────────────────────────────────
# IMPORTANT: hightech uses Account-level IR (configurationType="account").
# All CIs MUST group by UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c
# and join through UnifiedLinkssotAccountRt__dlm — NOT the Individual tables.

HIGHTECH_CIS = [
    {
        "key":         "SubscriptionSummary",
        "displayName": "{prefix} Subscription Summary",
        "description": (
            "Subscription portfolio: active subscriptions, total MRR, seats, and near-term renewals. "
            "Powers renewal pipeline and expansion revenue segmentation."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    SUM(CASE WHEN HtSubscription__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_sub_count__c,\n"
            "    SUM(CASE WHEN HtSubscription__dlm.Status__c = 'Active' THEN HtSubscription__dlm.Mrr__c ELSE 0 END) AS total_mrr__c,\n"
            "    SUM(HtSubscription__dlm.Seats__c) AS total_seats__c,\n"
            "    SUM(CASE WHEN HtSubscription__dlm.DaysUntilRenewal__c > 0 AND HtSubscription__dlm.DaysUntilRenewal__c <= 90 THEN 1 ELSE 0 END) AS renewal_within_90_days__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN HtSubscription__dlm\n"
            "    ON HtSubscription__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Renewal pipeline: renewal_within_90_days__c >= 1  ·  Expansion: total_mrr__c <= 5000",
    },
    {
        "key":         "UsageHealthScore",
        "displayName": "{prefix} Usage Health Score",
        "description": (
            "Product usage health metrics per account. "
            "Powers health scoring, churn prediction, and expansion opportunity identification."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    AVG(HtUsageRecord__dlm.ActiveUsers__c) AS avg_active_users__c,\n"
            "    AVG(HtUsageRecord__dlm.LoginCount__c) AS avg_login_count__c,\n"
            "    AVG(HtUsageRecord__dlm.FeatureAdoptionScore__c) AS avg_feature_adoption_score__c,\n"
            "    SUM(HtUsageRecord__dlm.DataVolumeGb__c) AS total_data_volume_gb__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN HtUsageRecord__dlm\n"
            "    ON HtUsageRecord__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Low adoption: avg_login_count__c <= 5 AND active_sub_count >= 1",
    },
    {
        "key":         "SupportProfile",
        "displayName": "{prefix} Support Profile",
        "description": (
            "Support case profile per account. "
            "Powers experience risk identification and customer success prioritization."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    COUNT(HtSupportTicket__dlm.Id__c) AS total_tickets__c,\n"
            "    SUM(CASE WHEN HtSupportTicket__dlm.Status__c = 'Open' THEN 1 ELSE 0 END) AS open_ticket_count__c,\n"
            "    SUM(CASE WHEN HtSupportTicket__dlm.Severity__c = 'Critical' THEN 1 ELSE 0 END) AS critical_ticket_count__c,\n"
            "    AVG(HtSupportTicket__dlm.CsatScore__c) AS avg_csat_score__c,\n"
            "    AVG(HtSupportTicket__dlm.ResolutionDays__c) AS avg_resolution_days__c,\n"
            "    SUM(CASE WHEN HtSupportTicket__dlm.DaysSinceOpened__c <= 60 THEN 1 ELSE 0 END) AS recent_ticket_count__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN HtSupportTicket__dlm\n"
            "    ON HtSupportTicket__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": (
            "Support burden: open_ticket_count__c >= 2 AND critical_ticket_count__c >= 1  ·  "
            "No recent ticket (2 months): recent_ticket_count__c = 0 → proactive outreach"
        ),
    },
    {
        "key":         "AccountHealthProfile",
        "displayName": "{prefix} Account Health Profile",
        "description": (
            "Master account health CI: combines churn risk, NPS, LTV with subscription data "
            "for proactive customer success."
        ),
        "sql": (
            "SELECT\n"
            "    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,\n"
            "    MAX(ssot__Account__dlm.ChurnScore__c) AS churn_score__c,\n"
            "    MAX(ssot__Account__dlm.NpsScore__c) AS nps_score__c,\n"
            "    MAX(ssot__Account__dlm.Ltv__c) AS ltv__c,\n"
            "    SUM(CASE WHEN HtSubscription__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_sub_count__c,\n"
            "    SUM(CASE WHEN HtSubscription__dlm.Status__c = 'Active' THEN HtSubscription__dlm.Mrr__c ELSE 0 END) AS total_mrr__c\n"
            + _B2B_UNIFIED_JOINS +
            "JOIN HtSubscription__dlm\n"
            "    ON HtSubscription__dlm.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "JOIN ssot__Account__dlm\n"
            "    ON ssot__Account__dlm.ssot__Id__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c\n"
            "GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c"
        ),
        "demo_use": "Churn risk 90d: renewal_within_90_days >= 1 AND churn_score__c >= 55",
    },
    _B2B_ENGAGEMENT_CI,
]


# ─── UTILITIES CIs ───────────────────────────────────────────────────────────

_CONSUMPTION_PROFILE_CI = {
    "key":         "ConsumptionProfile",
    "displayName": "{prefix} Consumption Profile",
    "description": (
        "Energy/water consumption profile per unified individual. "
        "Tracks contract count, plan type breakdown, avg monthly bill, and overage history. "
        "Powers high-consumption and multi-product segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(DISTINCT UtilityContract__dlm.Id__c) AS contract_count__c,\n"
        "    SUM(CASE WHEN UtilityContract__dlm.PlanType__c = 'Electricity' THEN 1 ELSE 0 END) AS electricity_contracts__c,\n"
        "    SUM(CASE WHEN UtilityContract__dlm.PlanType__c = 'Gas' THEN 1 ELSE 0 END) AS gas_contracts__c,\n"
        "    AVG(ConsumptionRecord__dlm.MonthlyBill__c) AS avg_monthly_bill__c,\n"
        "    SUM(ConsumptionRecord__dlm.MonthlyBill__c) AS total_annual_bill__c,\n"
        "    SUM(ConsumptionRecord__dlm.OverageCharge__c) AS total_overage__c,\n"
        "    SUM(CASE WHEN ConsumptionRecord__dlm.OverageCharge__c > 0 THEN 1 ELSE 0 END) AS overage_months__c\n"
        + _UNIFIED_JOINS +
        "JOIN UtilityContract__dlm\n"
        "    ON UtilityContract__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN ConsumptionRecord__dlm\n"
        "    ON ConsumptionRecord__dlm.ContractId__c = UtilityContract__dlm.Id__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "High consumption: avg_monthly_bill__c > 150  ·  "
        "Overage-prone: overage_months__c >= 3  ·  "
        "Multi-product: electricity_contracts__c >= 1 AND gas_contracts__c >= 1"
    ),
}

_UTILS_RISK_CI = {
    "key":         "CustomerRiskProfile",
    "displayName": "{prefix} Customer Risk Profile",
    "description": (
        "Utility customer risk profile: combines churn score, suspended contracts, "
        "and overage patterns for proactive retention."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    SUM(CASE WHEN ConsumptionRecord__dlm.OverageCharge__c > 0 THEN 1 ELSE 0 END) AS overage_months__c,\n"
        "    MAX(ConsumptionRecord__dlm.OverageCharge__c) AS max_overage__c,\n"
        "    AVG(ConsumptionRecord__dlm.MonthlyBill__c) AS avg_bill__c,\n"
        "    SUM(CASE WHEN UtilityContract__dlm.Status__c = 'Suspended' THEN 1 ELSE 0 END) AS suspended_contracts__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_payment__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN UtilityContract__dlm\n"
        "    ON UtilityContract__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN ConsumptionRecord__dlm\n"
        "    ON ConsumptionRecord__dlm.ContractId__c = UtilityContract__dlm.Id__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.ChurnScore__c, ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "At-risk: churn_score__c >= 65  ·  "
        "Payment issues: suspended_contracts__c >= 1  ·  "
        "High overage: overage_months__c >= 4 AND max_overage__c > 30"
    ),
}

UTILITIES_CIS = [
    _CONSUMPTION_PROFILE_CI,
    _UTILS_RISK_CI,
    _ENGAGEMENT_CI,
]


# ─── AIRLINES CIs ─────────────────────────────────────────────────────────────

_FLIGHT_PROFILE_CI = {
    "key":         "FlightProfile",
    "displayName": "{prefix} Flight Profile",
    "description": (
        "Flight booking profile per unified individual. "
        "Tracks flight count, cabin class distribution, total spend, and miles earned. "
        "Powers frequent flyer and premium traveler segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(FlightBooking__dlm.Id__c) AS total_flights__c,\n"
        "    COUNT(DISTINCT CASE WHEN FlightBooking__dlm.Status__c = 'Completed' THEN FlightBooking__dlm.Id__c END) AS completed_flights__c,\n"
        "    SUM(CASE WHEN FlightBooking__dlm.Status__c = 'Completed' THEN FlightBooking__dlm.BaseFare__c ELSE 0 END) AS total_spend__c,\n"
        "    AVG(CASE WHEN FlightBooking__dlm.Status__c = 'Completed' THEN FlightBooking__dlm.BaseFare__c END) AS avg_fare__c,\n"
        "    SUM(CASE WHEN FlightBooking__dlm.CabinClass__c IN ('Business','First') THEN 1 ELSE 0 END) AS premium_flights__c,\n"
        "    SUM(FlightBooking__dlm.MilesEarned__c) AS total_miles_earned__c\n"
        + _UNIFIED_JOINS +
        "JOIN FlightBooking__dlm\n"
        "    ON FlightBooking__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Business travelers: premium_flights__c >= 3  ·  "
        "High LTV: total_spend__c > 3000  ·  "
        "Frequent flyers: completed_flights__c >= 8"
    ),
}

_AIRLINE_RISK_CI = {
    "key":         "CustomerRiskProfile",
    "displayName": "{prefix} Customer Risk Profile",
    "description": (
        "Airline customer risk profile: combines churn score, cancellation rate, "
        "and dormant FFP miles for proactive re-engagement."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(FlightBooking__dlm.Id__c) AS total_bookings__c,\n"
        "    SUM(CASE WHEN FlightBooking__dlm.Status__c = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled_bookings__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.LoyaltyPointsBalance__c) AS miles_balance__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_flight__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN FlightBooking__dlm\n"
        "    ON FlightBooking__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.ChurnScore__c, ssot__Individual__dlm.LoyaltyPointsBalance__c,\n"
        "         ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "Dormant with miles: days_since_last_flight__c > 180 AND miles_balance__c > 5000  ·  "
        "High churn risk: churn_score__c >= 70  ·  "
        "Cancellation-prone: cancelled_bookings__c >= 2"
    ),
}

AIRLINES_CIS = [
    _FLIGHT_PROFILE_CI,
    _LOYALTY_PROFILE_CI,
    _AIRLINE_RISK_CI,
    _ENGAGEMENT_CI,
]


# ─── HEALTHCARE CIs ───────────────────────────────────────────────────────────

_VISIT_PROFILE_CI = {
    "key":         "VisitProfile",
    "displayName": "{prefix} Visit Profile",
    "description": (
        "Medical visit profile per HMO member. "
        "Tracks total visits, ER frequency, telemedicine usage, and copay history. "
        "Powers preventive care gap and high-utilisation segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(MedicalVisit__dlm.Id__c) AS total_visits__c,\n"
        "    SUM(CASE WHEN MedicalVisit__dlm.VisitType__c = 'Emergency' THEN 1 ELSE 0 END) AS er_visits__c,\n"
        "    SUM(CASE WHEN MedicalVisit__dlm.VisitType__c = 'Telemedicine' THEN 1 ELSE 0 END) AS telemedicine_visits__c,\n"
        "    AVG(MedicalVisit__dlm.CopayAmount__c) AS avg_copay__c,\n"
        "    SUM(MedicalVisit__dlm.CopayAmount__c) AS total_copay__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_visit__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN MedicalVisit__dlm\n"
        "    ON MedicalVisit__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "High utilisation: total_visits__c >= 8  ·  "
        "Preventive care gap: days_since_last_visit__c >= 365  ·  "
        "ER frequent: er_visits__c >= 2"
    ),
}

_HEALTH_RISK_CI = {
    "key":         "HealthRiskProfile",
    "displayName": "{prefix} Health Risk Profile",
    "description": (
        "Health risk indicators per HMO member: abnormal lab results, churn score, "
        "and days since last visit. Powers at-risk member retention campaigns."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(LabResult__dlm.Id__c) AS total_tests__c,\n"
        "    SUM(LabResult__dlm.IsAbnormal__c) AS abnormal_results__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_visit__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN LabResult__dlm\n"
        "    ON LabResult__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.ChurnScore__c, ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "Abnormal results: abnormal_results__c >= 1  ·  "
        "Renewal at risk: churn_score__c >= 60  ·  "
        "Long gap: days_since_last_visit__c >= 365"
    ),
}

HEALTHCARE_CIS = [
    _VISIT_PROFILE_CI,
    _HEALTH_RISK_CI,
    _ENGAGEMENT_CI,
]

# ─── SPORTS CLUB CIs ──────────────────────────────────────────────────────────

_MEMBERSHIP_PROFILE_CI = {
    "key":         "MembershipProfile",
    "displayName": "{prefix} Membership Profile",
    "description": (
        "Club membership profile per member. "
        "Tracks plan type, monthly fee, age of membership, and upcoming renewals. "
        "Powers renewal-risk and upgrade-ready segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(Membership__dlm.Id__c) AS total_memberships__c,\n"
        "    SUM(CASE WHEN Membership__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_memberships__c,\n"
        "    MAX(Membership__dlm.MonthlyFee__c) AS monthly_fee__c,\n"
        "    MAX(Membership__dlm.MembershipAgeMonths__c) AS membership_age_months__c,\n"
        "    SUM(Membership__dlm.RenewingSoon__c) AS renewal_within_90_days__c\n"
        + _UNIFIED_JOINS +
        "JOIN Membership__dlm\n"
        "    ON Membership__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "New members: membership_age_months__c <= 3  ·  "
        "Renewal risk: renewal_within_90_days__c >= 1  ·  "
        "Budget plan: monthly_fee__c <= 30"
    ),
}

_ACTIVITY_PROFILE_CI = {
    "key":         "ActivityProfile",
    "displayName": "{prefix} Activity Profile",
    "description": (
        "Gym / sports activity profile per member. "
        "Tracks total sessions, total active minutes, and days since last visit. "
        "Powers dormancy detection and high-activity upsell segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(ActivityRecord__dlm.Id__c) AS total_sessions__c,\n"
        "    SUM(ActivityRecord__dlm.DurationMinutes__c) AS total_minutes__c,\n"
        "    AVG(ActivityRecord__dlm.DurationMinutes__c) AS avg_session_minutes__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_activity__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN ActivityRecord__dlm\n"
        "    ON ActivityRecord__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "Dormant: days_since_last_activity__c >= 60  ·  "
        "High activity: total_sessions__c >= 30  ·  "
        "Upgrade ready: total_sessions__c >= 20 AND monthly_fee <= 30"
    ),
}

_SPORTS_RISK_CI = {
    "key":         "CustomerRiskProfile",
    "displayName": "{prefix} Customer Risk Profile",
    "description": (
        "Churn risk profile for sports club members: combines churn score, "
        "days inactive, and upcoming renewal to prioritise retention outreach."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_activity__c,\n"
        "    SUM(Membership__dlm.RenewingSoon__c) AS renewal_within_90_days__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN Membership__dlm\n"
        "    ON Membership__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.ChurnScore__c, ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "Churn risk: churn_score__c >= 60  ·  "
        "Renewal + churn: renewal_within_90_days__c >= 1 AND churn_score__c >= 50  ·  "
        "Long inactive: days_since_last_activity__c >= 90"
    ),
}

SPORTS_CLUB_CIS = [
    _MEMBERSHIP_PROFILE_CI,
    _ACTIVITY_PROFILE_CI,
    _SPORTS_RISK_CI,
    _ENGAGEMENT_CI,
]


# ─── ECOMMERCE CIs ────────────────────────────────────────────────────────────

_ORDER_PROFILE_CI = {
    "key":         "OrderProfile",
    "displayName": "{prefix} Order Profile",
    "description": (
        "Online purchase profile per shopper: total orders, total spend, average basket size, "
        "and days since last order. Powers LTV and dormancy segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(EcomOrder__dlm.Id__c) AS total_orders__c,\n"
        "    SUM(EcomOrder__dlm.TotalAmount__c) AS total_spend__c,\n"
        "    AVG(EcomOrder__dlm.TotalAmount__c) AS avg_basket_size__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_order__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN EcomOrder__dlm\n"
        "    ON EcomOrder__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "High LTV: total_spend__c >= 500  ·  "
        "Frequent: total_orders__c >= 5  ·  "
        "Dormant: days_since_last_order__c >= 90"
    ),
}

_CART_ABANDONMENT_CI = {
    "key":         "CartAbandonmentProfile",
    "displayName": "{prefix} Cart Abandonment Profile",
    "description": (
        "Cart abandonment behaviour per shopper: abandoned carts count, average cart value, "
        "total abandoned value. Powers re-engagement and cart-recovery segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(CartAbandonment__dlm.Id__c) AS abandoned_carts__c,\n"
        "    AVG(CartAbandonment__dlm.CartValue__c) AS avg_cart_value__c,\n"
        "    SUM(CartAbandonment__dlm.CartValue__c) AS total_abandoned_value__c\n"
        + _UNIFIED_JOINS +
        "JOIN CartAbandonment__dlm\n"
        "    ON CartAbandonment__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Cart abandoners: abandoned_carts__c >= 1  ·  "
        "High-value abandoned: avg_cart_value__c >= 100"
    ),
}

_ECOM_CUSTOMER_VALUE_CI = {
    "key":         "CustomerValue",
    "displayName": "{prefix} Customer Value",
    "description": (
        "Customer value profile per shopper: churn score and predicted LTV from enrichment fields. "
        "Powers win-back and high-value retention segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.PredictedLtv__c) AS predicted_ltv__c,\n"
        "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Churn risk: churn_score__c >= 65  ·  "
        "High predicted LTV: predicted_ltv__c >= 800"
    ),
}

ECOMMERCE_CIS = [
    _ORDER_PROFILE_CI,
    _CART_ABANDONMENT_CI,
    _ECOM_CUSTOMER_VALUE_CI,
    _ENGAGEMENT_CI,
]


# ─── HOSPITALITY CIs ──────────────────────────────────────────────────────────

_STAY_PROFILE_CI = {
    "key":         "StayProfile",
    "displayName": "{prefix} Stay Profile",
    "description": (
        "Hotel stay profile per guest: total stays, total revenue, average revenue per stay, "
        "suite stays, cancelled stays, and days since last stay. "
        "Powers frequency, upgrade, and cancellation segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(HotelStay__dlm.Id__c) AS total_stays__c,\n"
        "    SUM(HotelStay__dlm.TotalRevenue__c) AS total_revenue__c,\n"
        "    AVG(HotelStay__dlm.TotalRevenue__c) AS avg_revenue_per_stay__c,\n"
        "    SUM(CASE WHEN HotelStay__dlm.RoomType__c = 'Suite' THEN 1 ELSE 0 END) AS suite_stays__c,\n"
        "    SUM(CASE WHEN HotelStay__dlm.Status__c = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled_stays__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_stay__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN HotelStay__dlm\n"
        "    ON HotelStay__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "Frequent guests: total_stays__c >= 3  ·  "
        "Suite upgrade candidates: avg_revenue_per_stay__c >= 200 AND suite_stays__c = 0  ·  "
        "Cancellation prone: cancelled_stays__c >= 2"
    ),
}

_HOSP_CUSTOMER_VALUE_CI = {
    "key":         "CustomerValue",
    "displayName": "{prefix} Customer Value",
    "description": (
        "Customer value profile per guest: churn score and predicted LTV from enrichment fields. "
        "Powers win-back and high-value retention segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.PredictedLtv__c) AS predicted_ltv__c,\n"
        "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Churn risk: churn_score__c >= 65  ·  "
        "High predicted LTV: predicted_ltv__c >= 800"
    ),
}

HOSPITALITY_CIS = [
    _STAY_PROFILE_CI,
    _LOYALTY_PROFILE_CI,
    _HOSP_CUSTOMER_VALUE_CI,
    _ENGAGEMENT_CI,
]


# ─── MEDIA CIs ────────────────────────────────────────────────────────────────

_SUBSCRIPTION_PROFILE_CI = {
    "key":         "SubscriptionProfile",
    "displayName": "{prefix} Subscription Profile",
    "description": (
        "Subscription profile per subscriber: plan name, plan type, monthly fee, status, "
        "and start date. Powers plan-tier and churn-risk segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(Subscription__dlm.PlanName__c) AS plan_name__c,\n"
        "    MAX(Subscription__dlm.PlanType__c) AS plan_type__c,\n"
        "    MAX(Subscription__dlm.MonthlyFee__c) AS monthly_fee__c,\n"
        "    MAX(Subscription__dlm.Status__c) AS subscription_status__c,\n"
        "    MIN(Subscription__dlm.StartDate__c) AS subscription_start__c\n"
        + _UNIFIED_JOINS +
        "JOIN Subscription__dlm\n"
        "    ON Subscription__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Active premium: subscription_status__c = 'Active' AND plan_type__c = 'Premium'  ·  "
        "Churned: subscription_status__c = 'Cancelled'  ·  "
        "Trial users: plan_type__c = 'Trial'"
    ),
}

_CONTENT_PROFILE_CI = {
    "key":         "ContentProfile",
    "displayName": "{prefix} Content Profile",
    "description": (
        "Content consumption profile per subscriber: total views, total watch minutes, "
        "completion rate, and top genre over last 720 days. "
        "Powers engagement and recommendation segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(ContentView__dlm.Id__c) AS total_views__c,\n"
        "    SUM(ContentView__dlm.DurationMinutes__c) AS total_watch_minutes__c,\n"
        "    AVG(CASE WHEN ContentView__dlm.Completed__c = 'true' THEN 1.0 ELSE 0.0 END) AS completion_rate__c,\n"
        "    MAX(ContentView__dlm.Genre__c) AS top_genre__c\n"
        + _UNIFIED_JOINS +
        "JOIN ContentView__dlm\n"
        "    ON ContentView__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "   AND ContentView__dlm.ViewDatetime__c >= DATEADD(DAY, -720, CURRENT_TIMESTAMP())\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Heavy viewers: total_views__c >= 20  ·  "
        "Binge watchers: completion_rate__c >= 0.8  ·  "
        "Low engagement: total_watch_minutes__c < 120"
    ),
}

_MEDIA_RISK_CI = {
    "key":         "CustomerValue",
    "displayName": "{prefix} Customer Value",
    "description": (
        "Customer value profile per subscriber: churn score and predicted LTV from enrichment fields. "
        "Powers win-back and high-value retention segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.PredictedLtv__c) AS predicted_ltv__c,\n"
        "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Churn risk: churn_score__c >= 65  ·  "
        "High predicted LTV: predicted_ltv__c >= 800"
    ),
}

MEDIA_CIS = [
    _SUBSCRIPTION_PROFILE_CI,
    _CONTENT_PROFILE_CI,
    _MEDIA_RISK_CI,
    _ENGAGEMENT_CI,
]


# ─── AUTOMOTIVE CIs ───────────────────────────────────────────────────────────

_VEHICLE_PROFILE_CI = {
    "key":         "VehicleProfile",
    "displayName": "{prefix} Vehicle Profile",
    "description": (
        "Vehicle ownership profile per customer: number of vehicles owned, makes, models, "
        "total purchase value, and latest purchase date. "
        "Powers upsell and conquest segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(Vehicle__dlm.Id__c) AS vehicles_owned__c,\n"
        "    MAX(Vehicle__dlm.Make__c) AS primary_make__c,\n"
        "    MAX(Vehicle__dlm.Model__c) AS primary_model__c,\n"
        "    SUM(Vehicle__dlm.PurchasePrice__c) AS total_vehicle_value__c,\n"
        "    MAX(Vehicle__dlm.PurchaseDate__c) AS latest_purchase_date__c\n"
        + _UNIFIED_JOINS +
        "JOIN Vehicle__dlm\n"
        "    ON Vehicle__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Multi-vehicle owners: vehicles_owned__c >= 2  ·  "
        "Premium buyers: total_vehicle_value__c >= 60000  ·  "
        "Conquest targets: primary_make__c != 'BrandName'"
    ),
}

_SERVICE_PROFILE_CI = {
    "key":         "ServiceProfile",
    "displayName": "{prefix} Service Profile",
    "description": (
        "Vehicle service history profile per customer: total service visits, total spend, "
        "average cost per visit, and days since last service. "
        "Powers service retention and upsell segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(ServiceRecord__dlm.Id__c) AS total_service_visits__c,\n"
        "    SUM(ServiceRecord__dlm.TotalCost__c) AS total_service_spend__c,\n"
        "    AVG(ServiceRecord__dlm.TotalCost__c) AS avg_service_cost__c,\n"
        "    MAX(ssot__Individual__dlm.DaysSinceLastPurchase__c) AS days_since_last_service__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN ServiceRecord__dlm\n"
        "    ON ServiceRecord__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c,\n"
        "         ssot__Individual__dlm.DaysSinceLastPurchase__c"
    ),
    "demo_use": (
        "Loyal service customers: total_service_visits__c >= 4  ·  "
        "Overdue for service: days_since_last_service__c >= 180  ·  "
        "High-spend: total_service_spend__c >= 2000"
    ),
}

_AUTO_CUSTOMER_VALUE_CI = {
    "key":         "CustomerValue",
    "displayName": "{prefix} Customer Value",
    "description": (
        "Customer value profile per buyer: churn score and predicted LTV from enrichment fields. "
        "Powers win-back and high-value retention segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.PredictedLtv__c) AS predicted_ltv__c,\n"
        "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Churn risk: churn_score__c >= 65  ·  "
        "High predicted LTV: predicted_ltv__c >= 800"
    ),
}

AUTOMOTIVE_CIS = [
    _VEHICLE_PROFILE_CI,
    _SERVICE_PROFILE_CI,
    _AUTO_CUSTOMER_VALUE_CI,
    _ENGAGEMENT_CI,
]


# ─── REAL ESTATE CIs ──────────────────────────────────────────────────────────

_INQUIRY_PROFILE_CI = {
    "key":         "InquiryProfile",
    "displayName": "{prefix} Inquiry Profile",
    "description": (
        "Property inquiry profile per buyer/renter: total inquiries, average listing price, "
        "preferred property type, preferred city, and latest inquiry date over last 720 days. "
        "Powers targeting by property preference."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(PropertyInquiry__dlm.Id__c) AS total_inquiries__c,\n"
        "    AVG(PropertyInquiry__dlm.ListingPrice__c) AS avg_inquiry_price__c,\n"
        "    MAX(PropertyInquiry__dlm.PropertyType__c) AS preferred_property_type__c,\n"
        "    MAX(PropertyInquiry__dlm.City__c) AS preferred_city__c,\n"
        "    MAX(PropertyInquiry__dlm.InquiryDatetime__c) AS latest_inquiry_date__c\n"
        + _UNIFIED_JOINS +
        "JOIN PropertyInquiry__dlm\n"
        "    ON PropertyInquiry__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "   AND PropertyInquiry__dlm.InquiryDatetime__c >= DATEADD(DAY, -720, CURRENT_TIMESTAMP())\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Active searchers: total_inquiries__c >= 3  ·  "
        "Luxury seekers: avg_inquiry_price__c >= 1000000  ·  "
        "Apartment hunters: preferred_property_type__c = 'Apartment'"
    ),
}

_TRANSACTION_PROFILE_CI = {
    "key":         "TransactionProfile",
    "displayName": "{prefix} Transaction Profile",
    "description": (
        "Property transaction profile per customer: total transactions, total sale value, "
        "average sale price, and transaction types. "
        "Powers past-buyer and investor segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(PropertyTransaction__dlm.Id__c) AS total_transactions__c,\n"
        "    SUM(PropertyTransaction__dlm.SalePrice__c) AS total_sale_value__c,\n"
        "    AVG(PropertyTransaction__dlm.SalePrice__c) AS avg_sale_price__c,\n"
        "    MAX(PropertyTransaction__dlm.TransactionType__c) AS primary_transaction_type__c,\n"
        "    MAX(PropertyTransaction__dlm.CloseDate__c) AS latest_close_date__c\n"
        + _UNIFIED_JOINS +
        "JOIN PropertyTransaction__dlm\n"
        "    ON PropertyTransaction__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Repeat buyers: total_transactions__c >= 2  ·  "
        "High-value: avg_sale_price__c >= 800000  ·  "
        "Renters: primary_transaction_type__c = 'Rental'"
    ),
}

_RE_CUSTOMER_VALUE_CI = {
    "key":         "CustomerValue",
    "displayName": "{prefix} Customer Value",
    "description": (
        "Customer value profile per buyer/renter: churn score and predicted LTV. "
        "Powers high-value and win-back segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,\n"
        "    MAX(ssot__Individual__dlm.PredictedLtv__c) AS predicted_ltv__c,\n"
        "    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "Churn risk: churn_score__c >= 65  ·  "
        "High predicted LTV: predicted_ltv__c >= 800"
    ),
}

REAL_ESTATE_CIS = [
    _INQUIRY_PROFILE_CI,
    _TRANSACTION_PROFILE_CI,
    _RE_CUSTOMER_VALUE_CI,
    _ENGAGEMENT_CI,
]


# ─── BETTING CIs ──────────────────────────────────────────────────────────────

_PLAYER_PROFILE_CI = {
    "key":         "PlayerProfile",
    "displayName": "{prefix} Player Profile",
    "description": (
        "Betting activity profile per player: total bets, total staked, total payout, "
        "net result, and win rate over last 720 days. "
        "Powers high-value player and reactivation segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    COUNT(BettingTransaction__dlm.Id__c) AS total_bets__c,\n"
        "    SUM(BettingTransaction__dlm.Stake__c) AS total_staked__c,\n"
        "    SUM(BettingTransaction__dlm.Payout__c) AS total_payout__c,\n"
        "    SUM(BettingTransaction__dlm.NetResult__c) AS net_result__c,\n"
        "    AVG(CASE WHEN BettingTransaction__dlm.Payout__c > 0 THEN 1.0 ELSE 0.0 END) AS win_rate__c\n"
        + _UNIFIED_JOINS +
        "JOIN BettingTransaction__dlm\n"
        "    ON BettingTransaction__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "   AND BettingTransaction__dlm.TransactionDatetime__c >= DATEADD(DAY, -720, CURRENT_TIMESTAMP())\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "VIP players: total_staked__c >= 5000  ·  "
        "Inactive players: total_bets__c < 3  ·  "
        "Consistent winners: win_rate__c >= 0.6"
    ),
}

_RISK_PROFILE_CI = {
    "key":         "RiskProfile",
    "displayName": "{prefix} Risk Profile",
    "description": (
        "Responsible gaming risk profile per player: account balance, deposit limit, "
        "KYC status, responsible gaming flag, and churn score. "
        "Powers compliance and at-risk player segmentation."
    ),
    "sql": (
        "SELECT\n"
        "    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,\n"
        "    MAX(BettingAccount__dlm.Balance__c) AS account_balance__c,\n"
        "    MAX(BettingAccount__dlm.DepositLimit__c) AS deposit_limit__c,\n"
        "    MAX(BettingAccount__dlm.KycStatus__c) AS kyc_status__c,\n"
        "    MAX(BettingAccount__dlm.ResponsibleGamingFlag__c) AS responsible_gaming_flag__c,\n"
        "    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c\n"
        + _UNIFIED_JOINS +
        "JOIN ssot__Individual__dlm\n"
        "    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "JOIN BettingAccount__dlm\n"
        "    ON BettingAccount__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c\n"
        "GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c"
    ),
    "demo_use": (
        "At-risk players: responsible_gaming_flag__c = 'true'  ·  "
        "KYC pending: kyc_status__c = 'Pending'  ·  "
        "Churn risk: churn_score__c >= 65"
    ),
}

BETTING_CIS = [
    _PLAYER_PROFILE_CI,
    _RISK_PROFILE_CI,
    _ENGAGEMENT_CI,
]


# ─── Industry CI map ──────────────────────────────────────────────────────────

INDUSTRY_CI_MAP = {
    "insurance": INSURANCE_CIS,
    "food":      FOOD_B2C_CIS,
    "retail":    RETAIL_CIS,
    "banking":   BANKING_CIS,
    "pharma":    PHARMA_CIS,
    "telco":     TELCO_CIS,
    "utilities": UTILITIES_CIS,
    "airlines":  AIRLINES_CIS,
    "food_b2b":  FOOD_B2B_CIS,
    "hightech":  HIGHTECH_CIS,
    "healthcare":  HEALTHCARE_CIS,
    "sports_club": SPORTS_CLUB_CIS,
    "ecommerce":   ECOMMERCE_CIS,
    "hospitality": HOSPITALITY_CIS,
    "media":       MEDIA_CIS,
    "automotive":  AUTOMOTIVE_CIS,
    "real_estate": REAL_ESTATE_CIS,
    "betting":     BETTING_CIS,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def list_existing_cis(core_url: str, token: str) -> set:
    """Return set of existing CI apiNames (handles pagination)."""
    url = f"{BASE}/calculated-insights?dataspace=default"
    names = set()
    while url:
        st, data = api(core_url, token, "GET", url)
        if st != 200:
            break
        col = data.get("collection") or {}
        for item in col.get("items") or []:
            names.add(item.get("apiName", ""))
        url = col.get("nextPageUrl")
    return names


def delete_ci(core_url: str, token: str, api_name: str) -> bool:
    """DELETE a CI by apiName. Returns True on success (204) or if not found (404)."""
    st, _ = api(core_url, token, "DELETE",
                f"{BASE}/calculated-insights/{api_name}?dataspace=default")
    return st in (204, 404)


def trigger_ci_run(core_url: str, token: str, api_name: str) -> tuple:
    """POST .../actions/run to kick an on-demand CI compute NOW.

    A SYSTEM_MANAGED CI is ACTIVE after POST but lastRunDateTime=None — its first compute
    waits for the engine schedule (30-60 min+). This triggers the compute immediately so
    segments can use it within minutes.

    Endpoint: POST /ssot/calculated-insights/{apiName}/actions/run
    Returns {"success": true} or ALREADY_IN_PROCESS (both = fine).
    Best-effort: a failure here is non-fatal (the scheduled run still happens).
    """
    st, resp = api(core_url, token, "POST",
                   f"{BASE}/calculated-insights/{api_name}/actions/run",
                   body={})
    return st, resp


def create_ci(core_url: str, token: str,
              api_name: str, display_name: str, description: str,
              sql: str) -> tuple:
    """POST one Calculated Insight.

    publishScheduleStartDateTime is required when publishScheduleInterval != NOT_SCHEDULED.
    Format: yyyy-MM-dd'T'HH:mm  (confirmed against Data Cloud REST API v62.0)
    dataSpace is NOT in the body, only in the ?dataspace=default query param.
    """
    import datetime
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00")

    body = {
        "apiName":                        api_name,
        "displayName":                    display_name,
        "description":                    description,
        "definitionType":                 "CALCULATED_METRIC",
        "publishScheduleInterval":        "SIX",
        "publishScheduleStartDateTime":   tomorrow,
        "expression":                     sql,
    }
    return api(core_url, token, "POST", f"{BASE}/calculated-insights?dataspace=default", body)


def save_sql_fallback(output_dir: Path, api_name: str,
                      display_name: str, sql: str, demo_use: str = ""):
    """Write CI SQL to a .sql file for manual creation via Data Cloud UI."""
    ci_dir = output_dir / "calculated_insights"
    ci_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"-- ┌─ Calculated Insight ─────────────────────────────────────────────┐",
        f"-- │ Display Name:  {display_name}",
        f"-- │ API Name:      {api_name}",
        f"-- │ Schedule:      SIX (every 6 hours)",
    ]
    if demo_use:
        lines.append(f"-- │ Demo use:      {demo_use}")
    lines += [
        f"-- └──────────────────────────────────────────────────────────────────┘",
        "",
        "-- HOW TO CREATE IN UI:",
        "-- Data Cloud Setup → Calculated Insights → New → SOQL/SQL → paste below",
        "",
        sql,
        "",
    ]
    (ci_dir / f"{api_name}.sql").write_text("\n".join(lines))


# ─── Unified DLO discovery ────────────────────────────────────────────────────

# Hardcoded names are only used as last-resort fallback — never as primary source.
_FALLBACK_UNIFIED = {
    "individual": ("UnifiedssotIndividualRt__dlm", "UnifiedLinkssotIndividualRt__dlm"),
    "account":    ("UnifiedssotAccountRt__dlm",    "UnifiedLinkssotAccountRt__dlm"),
}

# Ordered candidate pairs to probe via GET /data-model-objects/{name}.
# The probe returns HTTP 200 if the DMO exists — definitive, no guessing.
# Most common patterns first so the right one is found with fewest API calls.
_UNIFIED_CANDIDATE_PAIRS = {
    "individual": [
        # No ssot prefix, no short-ID suffix (default when no developerName specified)
        ("UnifiedIndividual__dlm",         "IndividualIdentityLink__dlm"),
        # ssot prefix + short Ruleset ID = "Rt" (e.g. orgs where IR was named with ID "RT")
        ("UnifiedssotIndividualRt__dlm",   "UnifiedLinkssotIndividualRt__dlm"),
        # ssot prefix, no suffix
        ("Unifiedssot__Individual__dlm",   "UnifiedLinkssot__Individual__dlm"),
    ],
    "account": [
        ("UnifiedAccount__dlm",            "AccountIdentityLink__dlm"),
        ("UnifiedssotAccountRt__dlm",      "UnifiedLinkssotAccountRt__dlm"),
        ("Unifiedssot__Account__dlm",      "UnifiedLinkssot__Account__dlm"),
    ],
}

# All placeholder names used in hardcoded SQL — patched out before posting.
# Order matters: more specific patterns first so substring replacement doesn't conflict.
_ALL_PLACEHOLDER_NAMES = [
    # Pattern: Ruleset ID = "RT" (ssot prefix + Rt suffix)
    "UnifiedssotIndividualRt__dlm", "UnifiedLinkssotIndividualRt__dlm",
    "UnifiedssotAccountRt__dlm",    "UnifiedLinkssotAccountRt__dlm",
    # Pattern: ssot prefix, no suffix (some orgs)
    "Unifiedssot__Individual__dlm", "UnifiedLinkssot__Individual__dlm",
    "Unifiedssot__Account__dlm",    "UnifiedLinkssot__Account__dlm",
    # Pattern: no ssot prefix, no suffix (e.g. blank Ruleset ID)
    "UnifiedIndividual__dlm",       "IndividualIdentityLink__dlm",
    "UnifiedAccount__dlm",          "AccountIdentityLink__dlm",
]


def discover_unified_dlos(core_url: str, token: str, b2b_account: bool, cfg: dict) -> tuple:
    """Return (unified_dmo, unified_link_dmo) using the names Salesforce actually created on this org.

    Priority:
      1. Config override (explicit)
      2. IR API — the IR response knows what unified DLO it created (ground truth)
      3. Scan all DLOs for objects starting with 'Unified' matching the IR type
      4. Hardcoded fallback (last resort)
    """
    ir_type = "account" if b2b_account else "individual"

    # 1. Config override
    if b2b_account:
        u = cfg.get("unifiedAccountDlo")
        l = cfg.get("unifiedAccountLinkDlo")
    else:
        u = cfg.get("unifiedIndividualDlo")
        l = cfg.get("unifiedLinkDlo")
    if u and l:
        return u, l

    # 2. IR API — read reconciliationRules; each rule declares its unifiedDmoName + linkDmoName.
    #    The rule whose entityName matches the primary entity DMO has the answer.
    entity_dmo = "ssot__Account__dlm" if b2b_account else "ssot__Individual__dlm"
    st, data = api(core_url, token, "GET",
                   f"{BASE}/identity-resolutions?dataspace=default")
    if st == 200 and isinstance(data, dict):
        for ir in data.get("identityResolutions", []):
            if (ir.get("configurationType") or "individual").lower() != ir_type:
                continue
            for rule in (ir.get("reconciliationRules") or []):
                if rule.get("entityName") == entity_dmo:
                    u = rule.get("unifiedDmoName")
                    l = rule.get("linkDmoName")
                    if u and l:
                        return u, l

    # 3. Probe known candidate pairs — GET /data-model-objects/{name} returns 200 if it exists.
    #    This is the most reliable method: definitive 200/404, no parsing or guessing.
    for u_cand, l_cand in _UNIFIED_CANDIDATE_PAIRS[ir_type]:
        su, _ = api(core_url, token, "GET",
                    f"{BASE}/data-model-objects/{u_cand}?dataspace=default")
        sl, _ = api(core_url, token, "GET",
                    f"{BASE}/data-model-objects/{l_cand}?dataspace=default")
        if su == 200 and sl == 200:
            return u_cand, l_cand

    # 4. Hardcoded fallback
    print(f"  ⚠️  Could not auto-detect unified DLO names from IR API — using fallback names.")
    print(f"     If CIs fail with 'Cannot find type', add to config:")
    print(f"       \"unifiedIndividualDlo\": \"<actual name>\"")
    print(f"       \"unifiedLinkDlo\": \"<actual link name>\"")
    return _FALLBACK_UNIFIED[ir_type]


def patch_sql(sql: str, actual_unified: str, actual_link: str) -> str:
    """Replace ALL known placeholder unified DLO names in SQL with the actual names on this org."""
    for name in _ALL_PLACEHOLDER_NAMES:
        if "Link" in name:
            sql = sql.replace(name, actual_link)
        else:
            sql = sql.replace(name, actual_unified)
    return sql


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg      = json.loads(Path(args.config).read_text())
    alias    = cfg["orgAlias"]
    slug     = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()
    prefix   = slug.replace("-", "_").title().replace("_", "")   # e.g. "Migdal"
    out_dir  = Path(cfg.get("outputDir", f"data/{slug}"))
    b2b_account = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")

    print(f"\n📊  Creating Calculated Insights for {cfg.get('clientName', slug)} ({industry})")
    print(f"    Org: {alias}\n")

    ci_defs = INDUSTRY_CI_MAP.get(industry)
    if not ci_defs:
        print(f"  ℹ️  No CI definitions for industry '{industry}' — skipping")
        print(f"     Supported: {', '.join(INDUSTRY_CI_MAP.keys())}")
        return

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}")

    # Auto-discover actual unified DLO names for this org
    actual_unified, actual_link = discover_unified_dlos(core_url, core_token, b2b_account, cfg)
    ir_type = "account" if b2b_account else "individual"
    default_unified, default_link = _FALLBACK_UNIFIED[ir_type]
    if actual_unified != default_unified:
        print(f"  ℹ️  Unified DLO names auto-detected (differ from default):")
        print(f"       {default_unified} → {actual_unified}")
        print(f"       {default_link} → {actual_link}")
    else:
        print(f"  ✓  Unified DLO: {actual_unified}")

    existing = list_existing_cis(core_url, core_token)
    print(f"  ℹ️  Found {len(existing)} existing CIs\n")

    # Pre-flight: check if custom enrichment fields exist on the individual/account DMO.
    # These are added by create_dmos.py (extend_standard_dmo). CIs that reference them
    # are skipped with a clear message if missing — the SE must run create_dmos.py first.
    enrich_dmo = "ssot__Account__dlm" if b2b_account else "ssot__Individual__dlm"
    _st, _resp = api(core_url, core_token, "GET",
                     f"{BASE}/data-model-objects/{enrich_dmo}?dataspace=default")
    _existing_fields = {f["name"] for f in (_resp.get("fields") or [])} \
                       if isinstance(_resp, dict) else set()
    enrichment_ok = "ChurnScore__c" in _existing_fields
    if not enrichment_ok:
        print(f"  ⚠️  Enrichment fields missing on {enrich_dmo} (ChurnScore__c not found).")
        print(f"     Run create_dmos.py first to add them, or the CustomerRisk CI will be skipped.\n")

    results    = []
    api_failed = False

    for ci in ci_defs:
        api_name     = f"{prefix}_{ci['key']}__cio"
        display_name = ci["displayName"].replace("{prefix}", prefix)
        demo_use     = ci.get("demo_use", "")

        # Patch unified DLO names to match this org's actual names
        ci_sql          = patch_sql(ci["sql"],          actual_unified, actual_link)
        ci_sql_fallback = patch_sql(ci["sql_fallback"], actual_unified, actual_link) \
                          if ci.get("sql_fallback") else None

        # Always write SQL fallback (useful for manual creation or reference)
        save_sql_fallback(out_dir, api_name, display_name, ci_sql, demo_use)

        # Skip CIs that need enrichment fields not yet present on this org
        if not enrichment_ok and "ChurnScore__c" in ci_sql:
            print(f"  ⚠  {api_name}  SKIPPED — enrichment fields missing (run create_dmos.py first)")
            results.append({"ci": api_name, "status": "skipped-enrichment-missing"})
            continue

        # Delete-then-recreate so the SQL (and unified_individual__c dimension) is always current.
        # Simply skipping existing CIs would leave stale SQLs that can't be used in Segment Builder.
        if api_name in existing:
            if args.dry_run:
                print(f"  ↩  {api_name}  (exists — would delete+recreate in live run)")
                results.append({"ci": api_name, "status": "dry-run"})
                continue
            print(f"  🗑  {api_name}  (deleting stale version) ...", end=" ", flush=True)
            if delete_ci(core_url, core_token, api_name):
                print("deleted", end=" → ", flush=True)
                time.sleep(5.0)  # wait for async delete to propagate before recreating
            else:
                print("delete failed — will try recreate anyway", end=" → ", flush=True)

        print(f"  →  {api_name} ...", end=" ", flush=True) if api_name not in existing else None

        if args.dry_run:
            print("[dry-run]")
            results.append({"ci": api_name, "status": "dry-run"})
            continue

        status, resp = create_ci(
            core_url, core_token,
            api_name, display_name, ci["description"], ci_sql,
        )

        # If primary SQL fails due to unmapped fields, try fallback SQL
        fallback_used = False
        if status not in (200, 201) and ci_sql_fallback:
            err_str = str(resp).upper()
            if "CANNOT FIND TYPE" in err_str or "FIELD NOT FOUND" in err_str or "ENTITY_SAVE_ERROR" in err_str:
                print(f"\n    (primary SQL failed — trying fallback without enrichment fields) ...", end=" ", flush=True)
                status, resp = create_ci(
                    core_url, core_token,
                    api_name, display_name,
                    ci["description"] + " [basic]",
                    ci_sql_fallback,
                )
                fallback_used = True

        if status in (200, 201):
            calc_status = resp.get("calculatedInsightStatus", "PROCESSING")
            suffix = " [fallback]" if fallback_used else ""
            print(f"✓  ({calc_status}){suffix}")
            results.append({"ci": api_name, "status": "created", "fallback": fallback_used})
        elif "DUPLICATE" in str(resp).upper():
            print("↩  (duplicate)")
            results.append({"ci": api_name, "status": "duplicate"})
        else:
            print(f"✗  ({status})")
            print(f"     {str(resp)[:250]}")
            results.append({"ci": api_name,
                             "status": f"error-{status}",
                             "detail": str(resp)[:400]})
            api_failed = True

        time.sleep(0.5)

    ok    = sum(1 for r in results if r["status"] in ("created", "existing", "duplicate"))
    total = len(results)
    ci_dir = out_dir / "calculated_insights"
    print(f"\n✅  {ok}/{total} CIs OK")
    print(f"  📄  SQL files: {ci_dir}/")

    # ── Trigger Run Now on all CIs ──────────────────────────────────────────
    # A freshly created SYSTEM_MANAGED CI is ACTIVE but has never computed (lastRunDateTime=None).
    # Without an explicit run trigger the first compute can take 30-60+ min (engine schedule).
    # POST .../actions/run kicks an on-demand job immediately → segments get data within minutes.
    if not args.dry_run and ok > 0:
        print(f"\n  ▶  Triggering Run Now on {ok} CI(s)…")
        run_names = [r["ci"] for r in results if r["status"] in ("created", "existing", "duplicate")]
        run_ok = 0
        for ci_name in run_names:
            rst, rrsp = trigger_ci_run(core_url, core_token, ci_name)
            success = rst in (200, 201, 202)
            already = "ALREADY_IN_PROCESS" in str(rrsp).upper() or "AlreadyRunning" in str(rrsp)
            marker = "✅" if (success or already) else "⚠️"
            note   = "(already running)" if already else (f"HTTP {rst}" if not success else "")
            print(f"    {marker}  {ci_name}  {note}")
            if success or already:
                run_ok += 1
        print(f"  ▶  {run_ok}/{len(run_names)} run jobs accepted")

    if api_failed:
        print(f"\n  ⚠️  Some CIs could not be created via API.")
        print(f"     Create them manually:")
        print(f"     Data Cloud Setup → Calculated Insights → New")
        print(f"     Paste the SQL from: {ci_dir}/<name>.sql\n")

    # Persist results
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ci_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
