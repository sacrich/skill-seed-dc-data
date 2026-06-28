#!/usr/bin/env python3
"""
Create industry-specific custom Data Model Objects (DMOs) in Data Cloud.

Usage:
    python3 create_dmos.py --config config.json

Proven format (2026-06-24):
  POST /services/data/v62.0/ssot/data-model-objects
  body:
    - category: "OTHER"       ← UPPERCASE (not "Other" → 500 UNKNOWN_EXCEPTION)
    - dataSpaceName: "default" ← camelCase with capital N (not "dataSpace" → 500)
    - fields: [{name, label, dataType, isPrimaryKey, isDynamicLookup}]
    - name: WITHOUT __dlm suffix (platform appends it automatically)

  POST /ssot/data-model-objects DOES work — the old 500 UNKNOWN_EXCEPTION was caused by
  using wrong field names (dataSpace vs dataSpaceName, "Other" vs "OTHER").

Idempotent: GET first to check if the DMO already exists.

NOTE on web/email engagement standard DMOs:
  ssot__WebsiteEngagement__dlm and ssot__EmailEngagement__dlm are PLATFORM STANDARD DMOs.
  They cannot be created via POST — they exist on every org already (ENGAGEMENT category).
  Custom fields are added via extend_standard_dmo() in main().
  Standard fields used:
    WebsiteEngagement: ssot__Id__c (PK), ssot__IndividualId__c (FK), ssot__SessionId__c,
                       ssot__EngagementDateTm__c, ssot__PageURL__c, ssot__DeviceTypeTxt__c
    EmailEngagement:   ssot__Id__c (PK), ssot__IndividualId__c (FK),
                       ssot__EngagementDateTm__c, ssot__SendtimeEmailAddress__c, ssot__EmailName__c

  MAPPING GOTCHA (proven 2026-06-24): CSVs use 0/1 for opened/clicked/unsubscribed.
  The Data Lake materializes them as Number. Map to Number alias fields (*Count__c).
  EngagementScore CI references OpenedCount__c / ClickedCount__c on ssot__EmailEngagement__dlm.

NOTE on enrichment fields:
  Instead of a separate IndividualProfile custom DMO, enrichment fields are added directly
  to ssot__Individual__dlm (B2C) or ssot__Account__dlm (B2B) via extend_standard_dmo().
  Endpoint: POST /ssot/data-model-objects/{dmo}/fields
  This eliminates the dual-DMO confusion (Individual + IndividualProfile).
  All CIs join ssot__Individual__dlm.ChurnScore__c directly.
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


def field(name: str, label: str, data_type: str, pk: bool = False) -> dict:
    return {
        "name": name,
        "label": label,
        "dataType": data_type,
        "isPrimaryKey": pk,
        "isDynamicLookup": False,
    }


# ─── Enrichment fields added to ssot__Individual__dlm / ssot__Account__dlm ────
# These are PATCHed onto the standard DMO via extend_standard_dmo().
# B2C industries extend ssot__Individual__dlm.
# B2B industries (food_b2b, hightech) extend ssot__Account__dlm.
# All CIs reference these fields directly on the standard DMO (no separate profile DMO).
INDIVIDUAL_CUSTOM_FIELDS = [
    field("ChurnScore__c",           "Churn Score",            "Number"),
    field("LoyaltyTier__c",          "Loyalty Tier",           "Text"),
    field("LoyaltyPointsBalance__c", "Loyalty Points Balance", "Number"),
    field("PointsEarnedYtd__c",      "Points Earned YTD",      "Number"),
    field("PointsRedeemedYtd__c",    "Points Redeemed YTD",    "Number"),
    field("Ltv__c",                  "LTV",                    "Number"),
    field("NpsScore__c",             "NPS Score",              "Number"),
    field("CustomerSince__c",        "Customer Since",         "Date"),
    field("IncomeRange__c",          "Income Range",           "Text"),
    field("Source__c",               "Source",                 "Text"),
    # B2B-only — null for B2C; populated when industry is food_b2b or hightech
    field("NumberOfEmployees__c",    "Number Of Employees",    "Number"),
    field("AnnualRevenue__c",        "Annual Revenue",         "Number"),
    # Enrichment — pre-computed at data generation time; available in all industries
    field("ValueTier__c",             "Value Tier",             "Text"),
    field("DigitalActive__c",         "Digital Active",         "Number"),
    field("PreferredChannel__c",      "Preferred Channel",      "Text"),
    field("AcquisitionChannel__c",    "Acquisition Channel",    "Text"),
    field("DaysSinceLastPurchase__c", "Days Since Last Purchase","Number"),
    field("RfmSegment__c",            "RFM Segment",            "Text"),
    field("PredictedLtv__c",          "Predicted LTV",          "Number"),
    field("ProductAffinity__c",       "Product Affinity",       "Text"),
]


# ─── Standard DMOs — created for EVERY industry ───────────────────────────────
# NOTE: ssot__WebsiteEngagement__dlm and ssot__EmailEngagement__dlm are PLATFORM STANDARD DMOs.
# They exist on every org and cannot be created via POST.
# Custom fields for these are added via extend_standard_dmo() calls in main().
# No custom DMOs need to be created in this list for the engagement streams.
STANDARD_DMOS: list = []

# ─── Custom fields to extend ssot__WebsiteEngagement__dlm ────────────────────
# Added via PATCH /ssot/data-model-objects/ssot__WebsiteEngagement__dlm?dataspace=default
# Standard fields already present: ssot__Id__c (PK), ssot__IndividualId__c (FK → Individual),
#   ssot__SessionId__c, ssot__EngagementDateTm__c, ssot__PageURL__c, ssot__DeviceTypeTxt__c
WEBSITE_ENGAGEMENT_CUSTOM_FIELDS = [
    field("PageCategory__c",    "Page Category",    "Text"),
    field("EventType__c",       "Event Type",       "Text"),
    field("DurationSeconds__c", "Duration Seconds", "Number"),
]

# ─── Custom fields to extend ssot__EmailEngagement__dlm ──────────────────────
# Added via POST /ssot/data-model-objects/ssot__EmailEngagement__dlm/fields
# Standard fields already present: ssot__Id__c (PK), ssot__IndividualId__c (FK → Individual),
#   ssot__EngagementDateTm__c (= eventDateTimeFieldName), ssot__SendtimeEmailAddress__c,
#   ssot__EmailName__c (= campaign name)
# MAPPING GOTCHA: CSVs use 0/1 for opened/clicked → DLO detects as Number.
#   Create Number alias fields (*Count__c) and map to those (not Boolean Opened__c).
#   EngagementScore CI uses OpenedCount__c / ClickedCount__c on ssot__EmailEngagement__dlm.
EMAIL_ENGAGEMENT_CUSTOM_FIELDS = [
    field("OpenedCount__c",       "Opened Count",       "Number"),
    field("ClickedCount__c",      "Clicked Count",      "Number"),
    field("UnsubscribedCount__c", "Unsubscribed Count", "Number"),
    field("CampaignId__c",        "Campaign Id",        "Text"),
]

# ─── Industry-specific DMO definitions ────────────────────────────────────────
# name = WITHOUT __dlm (platform appends it).
# All boolean fields use "Number" to match the 0/1 CSV representation.
# "description" key is PATCHed after creation via patch_dmo_description().
INDUSTRY_DMOS = {
    "insurance": [
        {
            "name": "InsurancePolicy",
            "label": "Insurance Policy",
            "description": (
                "Individual insurance policy record. Links to Individual via PartyId__c. "
                "Tracks product category, premium, coverage, and policy status. "
                "Powers policy summary CIs and renewal targeting segmentation."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",   pk=True),
                field("PartyId__c",         "Party Id",         "Text"),
                field("PolicyNumber__c",    "Policy Number",    "Text"),
                field("ProductName__c",     "Product Name",     "Text"),
                field("ProductCategory__c", "Product Category", "Text"),
                field("PremiumMonthly__c",  "Premium Monthly",  "Number"),
                field("PremiumAnnual__c",   "Premium Annual",   "Number"),
                field("CoverageAmount__c",  "Coverage Amount",  "Number"),
                field("StartDate__c",       "Start Date",       "Date"),
                field("EndDate__c",         "End Date",         "Date"),
                field("Status__c",          "Status",           "Text"),
            ],
        },
        {
            "name": "InsuranceClaim",
            "label": "Insurance Claim",
            "description": (
                "Insurance claim filed against a policy. Links to InsurancePolicy via PolicyId__c "
                "and to Individual via PartyId__c. Tracks claim type, amount, and resolution status. "
                "Powers claims summary CIs and high-risk customer segmentation."
            ),
            "fields": [
                field("Id__c",             "Id",              "Text",   pk=True),
                field("PolicyId__c",       "Policy Id",       "Text"),
                field("PartyId__c",        "Party Id",        "Text"),
                field("ClaimDate__c",      "Claim Date",      "Date"),
                field("ClaimType__c",      "Claim Type",      "Text"),
                field("ClaimAmount__c",    "Claim Amount",    "Number"),
                field("Status__c",         "Status",          "Text"),
                field("ResolutionDate__c", "Resolution Date", "Date"),
            ],
        },
    ],
    "food": [
        {
            "name": "PurchaseOrder",
            "label": "Purchase Order",
            "category": "ENGAGEMENT",   # Immutable purchase event — Transaction Journal pattern
            "description": (
                "Grocery purchase order placed by an individual. "
                "Tracks store, channel, total amount, and loyalty points earned. "
                "Powers purchase summary CIs and recency-based segmentation."
            ),
            "fields": [
                field("Id__c",                  "Id",                    "Text",   pk=True),
                field("PartyId__c",             "Party Id",              "Text"),
                field("OrderDatetime__c",       "Order Datetime",        "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("StoreName__c",           "Store Name",            "Text"),
                field("Channel__c",             "Channel",               "Text"),
                field("TotalAmount__c",         "Total Amount",          "Number"),
                field("LoyaltyPointsEarned__c", "Loyalty Points Earned", "Number"),
            ],
        },
        {
            "name": "OrderLine",
            "label": "Order Line",
            "description": (
                "Line item within a purchase order. "
                "Tracks product SKU, category, quantity, and line total. "
                "Powers category spend CIs and product affinity segmentation."
            ),
            "fields": [
                field("Id__c",          "Id",           "Text",   pk=True),
                field("OrderId__c",     "Order Id",     "Text"),
                field("PartyId__c",     "Party Id",     "Text"),
                field("ProductSku__c",  "Product Sku",  "Text"),
                field("ProductName__c", "Product Name", "Text"),
                field("Category__c",    "Category",     "Text"),
                field("Quantity__c",    "Quantity",     "Number"),
                field("UnitPrice__c",   "Unit Price",   "Number"),
                field("LineTotal__c",   "Line Total",   "Number"),
            ],
        },
        {
            "name": "LoyaltyTransaction",
            "label": "Loyalty Transaction",
            "category": "ENGAGEMENT",   # Earn/redeem events are immutable, ordered in timeline
            "description": (
                "Loyalty points earn or redeem event for an individual. "
                "Tracks transaction type, points delta, and running balance. "
                "Powers loyalty profile CIs and points-based segmentation."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",     pk=True),
                field("PartyId__c",         "Party Id",         "Text"),
                field("EventDatetime__c",   "Event Datetime",   "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("TransactionType__c", "Transaction Type", "Text"),
                field("Points__c",          "Points",           "Number"),
                field("Balance__c",         "Balance",          "Number"),
            ],
        },
    ],
    "retail": [
        {
            "name": "SalesOrder",
            "label": "Sales Order",
            "category": "ENGAGEMENT",   # Immutable purchase event — Transaction Journal pattern
            "description": (
                "Fashion/retail sales order placed by an individual. "
                "Tracks channel, total amount, and order status including returns. "
                "Powers purchase summary and channel profile CIs."
            ),
            "fields": [
                field("Id__c",              "Id",             "Text",   pk=True),
                field("PartyId__c",         "Party Id",       "Text"),
                field("OrderDatetime__c",   "Order Datetime", "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("Channel__c",     "Channel",      "Text"),
                field("TotalAmount__c", "Total Amount", "Number"),
                field("Status__c",      "Status",       "Text"),
            ],
        },
        {
            "name": "OrderLine",
            "label": "Order Line",
            "description": (
                "Line item within a retail sales order. "
                "Tracks product category (Bags, Shoes, Apparel, Accessories), quantity, and price. "
                "Powers category affinity CIs and cross-sell segmentation."
            ),
            "fields": [
                field("Id__c",          "Id",           "Text",   pk=True),
                field("OrderId__c",     "Order Id",     "Text"),
                field("PartyId__c",     "Party Id",     "Text"),
                field("ProductSku__c",  "Product Sku",  "Text"),
                field("ProductName__c", "Product Name", "Text"),
                field("Category__c",    "Category",     "Text"),
                field("Quantity__c",    "Quantity",     "Number"),
                field("UnitPrice__c",   "Unit Price",   "Number"),
                field("LineTotal__c",   "Line Total",   "Number"),
            ],
        },
        {
            "name": "LoyaltyTransaction",
            "label": "Loyalty Transaction",
            "category": "ENGAGEMENT",
            "description": (
                "Loyalty points earn or redeem event for an individual. "
                "Tracks transaction type, points delta, and running balance. "
                "Powers loyalty profile CIs and points-based segmentation."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",     pk=True),
                field("PartyId__c",         "Party Id",         "Text"),
                field("EventDatetime__c",   "Event Datetime",   "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("TransactionType__c", "Transaction Type", "Text"),
                field("Points__c",          "Points",           "Number"),
                field("Balance__c",         "Balance",          "Number"),
            ],
        },
    ],
    "banking": [
        {
            "name": "FinancialAccount",
            "label": "Financial Account",
            "description": (
                "Financial account held by an individual (Checking, Savings, Investment, etc.). "
                "Tracks account type, balance, and status. "
                "Powers account summary and product holdings CIs."
            ),
            "fields": [
                field("Id__c",           "Id",           "Text",   pk=True),
                field("PartyId__c",      "Party Id",     "Text"),
                field("AccountType__c",  "Account Type", "Text"),
                field("Balance__c",      "Balance",      "Number"),
                field("OpenedDate__c",   "Opened Date",  "Date"),
                field("Status__c",       "Status",       "Text"),
            ],
        },
        {
            "name": "Transaction",
            "label": "Transaction",
            "category": "ENGAGEMENT",   # Immutable financial events — "Transaction Journal" in user rule
            "description": (
                "Banking transaction record. Links to financial account and individual. "
                "Tracks spending category and amount for behavioral segmentation."
            ),
            "fields": [
                field("Id__c",           "Id",           "Text",     pk=True),
                field("AccountId__c",    "Account Id",   "Text"),
                field("PartyId__c",      "Party Id",     "Text"),
                field("TxDatetime__c",   "Tx Datetime",  "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("Category__c",     "Category",     "Text"),
                field("Amount__c",       "Amount",       "Number"),
            ],
        },
        {
            "name": "BankingProduct",
            "label": "Banking Product",
            "description": (
                "Banking product held by a customer (credit card, loan, mortgage, etc.). "
                "Tracks product type, amount, interest rate, and status. "
                "Powers product holdings CIs and cross-sell segmentation."
            ),
            "fields": [
                field("Id__c",           "Id",            "Text",   pk=True),
                field("PartyId__c",      "Party Id",      "Text"),
                field("ProductType__c",  "Product Type",  "Text"),
                field("ProductName__c",  "Product Name",  "Text"),
                field("Amount__c",       "Amount",        "Number"),
                field("InterestRate__c", "Interest Rate", "Number"),
                field("Status__c",       "Status",        "Text"),
                field("OpenedDate__c",   "Opened Date",   "Date"),
            ],
        },
        {
            "name": "LoyaltyTransaction",
            "label": "Loyalty Transaction",
            "category": "ENGAGEMENT",
            "description": (
                "Loyalty points earn or redeem event for an individual. "
                "Tracks transaction type, points delta, and running balance. "
                "Powers loyalty profile CIs and points-based segmentation."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",     pk=True),
                field("PartyId__c",         "Party Id",         "Text"),
                field("EventDatetime__c",   "Event Datetime",   "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("TransactionType__c", "Transaction Type", "Text"),
                field("Points__c",          "Points",           "Number"),
                field("Balance__c",         "Balance",          "Number"),
            ],
        },
    ],
    "pharma": [
        {
            "name": "Prescription",
            "label": "Prescription",
            "category": "ENGAGEMENT",   # Immutable fill event — Transaction Journal pattern
            "description": (
                "Prescription fill record for a patient individual. "
                "Tracks drug, therapeutic area, diagnosis, and adherence status. "
                "Powers prescription summary and therapeutic profile CIs."
            ),
            "fields": [
                field("Id__c",               "Id",               "Text",   pk=True),
                field("PartyId__c",          "Party Id",         "Text"),
                field("DrugName__c",         "Drug Name",        "Text"),
                field("TherapeuticArea__c",  "Therapeutic Area", "Text"),
                field("Diagnosis__c",        "Diagnosis",        "Text"),
                field("FillDatetime__c",     "Fill Datetime",    "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName (renamed from PrescribedDate__c)
                field("Status__c",           "Status",           "Text"),
            ],
        },
    ],
    "telco": [
        {
            "name": "ServiceContract",
            "label": "Service Contract",
            "description": (
                "Telco service contract for an individual. "
                "Tracks plan type (Mobile, Broadband, TV, Bundle), monthly fee, and status. "
                "Powers service summary and bundle upsell CIs."
            ),
            "fields": [
                field("Id__c",          "Id",           "Text",   pk=True),
                field("PartyId__c",     "Party Id",     "Text"),
                field("PlanName__c",    "Plan Name",    "Text"),
                field("PlanType__c",    "Plan Type",    "Text"),
                field("MonthlyFee__c",  "Monthly Fee",  "Number"),
                field("StartDate__c",   "Start Date",   "Date"),
                field("Status__c",      "Status",       "Text"),
            ],
        },
        {
            "name": "UsageRecord",
            "label": "Usage Record",
            "description": (
                "Monthly usage metrics per service contract. "
                "Tracks data consumption, voice minutes, and overage charges. "
                "Powers plan upgrade and churn segmentation."
            ),
            "fields": [
                field("Id__c",                 "Id",                   "Text",  pk=True),
                field("ContractId__c",         "Contract Id",          "Text"),
                field("PartyId__c",            "Party Id",             "Text"),
                field("UsageDate__c",           "Usage Date",           "Date"),   # first day of month (YYYY-MM-01) — enables native date range filtering
                field("DataUsedGb__c",         "Data Used Gb",         "Number"),
                field("VoiceMinutesUsed__c",   "Voice Minutes Used",   "Number"),
                field("SmsCount__c",           "Sms Count",            "Number"),
                field("OverageCharge__c",      "Overage Charge",       "Number"),
            ],
        },
    ],
    "food_b2b": [
        {
            "name": "WholesaleOrder",
            "label": "Wholesale Order",
            "category": "ENGAGEMENT",   # Immutable purchase event — Transaction Journal pattern
            "description": (
                "Wholesale order placed by store buyer. "
                "Tracks order value, item count, and payment terms. "
                "Powers B2B sales rep and account management segmentation."
            ),
            "fields": [
                field("Id__c",               "Id",             "Text",  pk=True),
                field("PartyId__c",          "Party Id",       "Text"),
                field("OrderDatetime__c",    "Order Datetime", "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("TotalAmount__c",  "Total Amount",  "Number"),
                field("ItemCount__c",    "Item Count",    "Number"),
                field("Status__c",       "Status",        "Text"),
                field("PaymentTerms__c", "Payment Terms", "Text"),
                field("SalesRep__c",     "Sales Rep",     "Text"),
            ],
        },
        {
            "name": "WholesaleOrderLine",
            "label": "Wholesale Order Line",
            "description": (
                "Line item within a wholesale order. "
                "Tracks product category, quantity, and price. "
                "Powers SKU penetration and category expansion segmentation."
            ),
            "fields": [
                field("Id__c",            "Id",             "Text",  pk=True),
                field("OrderId__c",       "Order Id",       "Text"),
                field("PartyId__c",       "Party Id",       "Text"),
                field("ProductSku__c",    "Product Sku",    "Text"),
                field("ProductName__c",   "Product Name",   "Text"),
                field("Category__c",      "Category",       "Text"),
                field("Quantity__c",      "Quantity",       "Number"),
                field("UnitPrice__c",     "Unit Price",     "Number"),
                field("LineTotal__c",     "Line Total",     "Number"),
                field("IsPromotional__c", "Is Promotional", "Number"),
            ],
        },
        {
            "name": "LoyaltyTransaction",
            "label": "Loyalty Transaction",
            "category": "ENGAGEMENT",
            "description": (
                "Loyalty points earn or redeem event for a B2B account. "
                "Tracks transaction type, points delta, and running balance. "
                "Powers B2B loyalty profile CIs and account tier segmentation."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",     pk=True),
                field("PartyId__c",         "Party Id",         "Text"),
                field("EventDatetime__c",   "Event Datetime",   "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("TransactionType__c", "Transaction Type", "Text"),
                field("Points__c",          "Points",           "Number"),
                field("Balance__c",         "Balance",          "Number"),
            ],
        },
    ],
    "utilities": [
        {
            "name": "UtilityContract",
            "label": "Utility Contract",
            "description": (
                "Utility service contract per customer. Tracks plan type (Electricity/Gas/Water), "
                "monthly fee, and status. Powers product holdings and churn risk CIs."
            ),
            "fields": [
                field("Id__c",         "Id",          "Text",   pk=True),
                field("PartyId__c",    "Party Id",    "Text"),
                field("PlanType__c",   "Plan Type",   "Text"),
                field("PlanName__c",   "Plan Name",   "Text"),
                field("MonthlyFee__c", "Monthly Fee", "Number"),
                field("StartDate__c",  "Start Date",  "Date"),
                field("Status__c",     "Status",      "Text"),
            ],
        },
        {
            "name": "ConsumptionRecord",
            "label": "Consumption Record",
            "description": (
                "Monthly energy/water consumption per contract. Tracks consumption volume, unit, "
                "monthly bill, and overage charges. Powers consumption profile and anomaly CIs."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",   pk=True),
                field("ContractId__c",      "Contract Id",      "Text"),
                field("PartyId__c",         "Party Id",         "Text"),
                field("UsageDate__c",       "Usage Date",       "Date"),    # YYYY-MM-01 for native range filtering
                field("ConsumptionValue__c","Consumption Value","Number"),
                field("ConsumptionUnit__c", "Consumption Unit", "Text"),
                field("MonthlyBill__c",     "Monthly Bill",     "Number"),
                field("OverageCharge__c",   "Overage Charge",   "Number"),
            ],
        },
    ],
    "airlines": [
        {
            "name": "FlightBooking",
            "label": "Flight Booking",
            "category": "ENGAGEMENT",
            "description": (
                "Flight booking event per passenger. Tracks route, cabin class, fare, and miles earned. "
                "Powers flight profile and segment CIs."
            ),
            "fields": [
                field("Id__c",             "Id",              "Text",     pk=True),
                field("PartyId__c",        "Party Id",        "Text"),
                field("BookingDatetime__c","Booking Datetime","DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("Origin__c",         "Origin",          "Text"),
                field("Destination__c",    "Destination",     "Text"),
                field("CabinClass__c",     "Cabin Class",     "Text"),
                field("BaseFare__c",       "Base Fare",       "Number"),
                field("MilesEarned__c",    "Miles Earned",    "Number"),
                field("Status__c",         "Status",          "Text"),
            ],
        },
        {
            "name": "LoyaltyTransaction",
            "label": "Loyalty Transaction",
            "category": "ENGAGEMENT",
            "description": (
                "FFP miles earn or redeem event per passenger. Tracks transaction type, miles delta, "
                "and running balance. Powers loyalty profile and tier upgrade CIs."
            ),
            "fields": [
                field("Id__c",              "Id",               "Text",     pk=True),
                field("PartyId__c",         "Party Id",         "Text"),
                field("EventDatetime__c",   "Event Datetime",   "DateTime"),  # REQUIRED — Engagement eventDateTimeFieldName
                field("TransactionType__c", "Transaction Type", "Text"),
                field("Points__c",          "Points",           "Number"),
                field("Balance__c",         "Balance",          "Number"),
            ],
        },
    ],
    "hightech": [
        {
            "name": "HtSubscription",
            "label": "Ht Subscription",
            "description": (
                "SaaS subscription contract. "
                "Tracks product tier, seat count, MRR, and renewal date. "
                "Powers renewal pipeline and expansion revenue analysis."
            ),
            "fields": [
                field("Id__c",               "Id",                "Text",  pk=True),
                field("PartyId__c",          "Party Id",          "Text"),
                field("ProductName__c",      "Product Name",      "Text"),
                field("Tier__c",             "Tier",              "Text"),
                field("Seats__c",            "Seats",             "Number"),
                field("Mrr__c",              "Mrr",               "Number"),
                field("StartDate__c",        "Start Date",        "Date"),
                field("RenewalDate__c",      "Renewal Date",      "Date"),
                field("Status__c",           "Status",            "Text"),
                field("DaysUntilRenewal__c", "Days Until Renewal","Number"),
            ],
        },
        {
            "name": "HtUsageRecord",
            "label": "Ht Usage Record",
            "description": (
                "Monthly product usage metrics per subscription. "
                "Tracks active users, login frequency, and feature adoption score. "
                "Powers health scoring and churn prediction."
            ),
            "fields": [
                field("Id__c",                    "Id",                     "Text",  pk=True),
                field("SubscriptionId__c",         "Subscription Id",        "Text"),
                field("PartyId__c",               "Party Id",               "Text"),
                field("UsageDate__c",             "Usage Date",             "Date"),   # first day of month (YYYY-MM-01) — enables native date range filtering
                field("ActiveUsers__c",           "Active Users",           "Number"),
                field("LoginCount__c",            "Login Count",            "Number"),
                field("FeatureAdoptionScore__c",  "Feature Adoption Score", "Number"),
                field("DataVolumeGb__c",          "Data Volume Gb",         "Number"),
            ],
        },
        {
            "name": "HtSupportTicket",
            "label": "Ht Support Ticket",
            "description": (
                "Customer support case. "
                "Tracks severity, resolution time, and CSAT score. "
                "Powers support burden and customer experience analysis."
            ),
            "fields": [
                field("Id__c",             "Id",              "Text",  pk=True),
                field("PartyId__c",        "Party Id",        "Text"),
                field("CreatedDate__c",    "Created Date",    "Date"),
                field("Category__c",       "Category",        "Text"),
                field("Severity__c",       "Severity",        "Text"),
                field("Status__c",         "Status",          "Text"),
                field("ResolutionDays__c", "Resolution Days", "Number"),
                field("CsatScore__c",       "Csat Score",       "Number"),
                field("DaysSinceOpened__c", "Days Since Opened","Number"),  # pre-computed at gen time — used in SupportProfile CI for "no ticket in 2 months" segment
            ],
        },
    ],
    "healthcare": [
        {
            "name": "MedicalVisit__dlm",
            "label": "Medical Visit",
            "description": (
                "Doctor and specialist visits per HMO member. Tracks specialty, visit type, copay, and diagnosis. Powers preventive care gap and high-utilisation segmentation."
            ),
            "category": "Other",
            "fields": [
                field("Id__c",           "Id",             "Text"),
                field("PartyId__c",      "Party Id",       "Text"),
                field("VisitDate__c",    "Visit Date",     "Date"),
                field("Specialty__c",    "Specialty",      "Text"),
                field("VisitType__c",    "Visit Type",     "Text"),
                field("CopayAmount__c",  "Copay Amount",   "Number"),
                field("DiagnosisCode__c","Diagnosis Code", "Text"),
            ],
        },
        {
            "name": "LabResult__dlm",
            "label": "Lab Result",
            "description": (
                "Laboratory test results per HMO member. Flags abnormal or critical results. Powers health-risk and preventive-care segmentation."
            ),
            "category": "Other",
            "fields": [
                field("Id__c",            "Id",            "Text"),
                field("PartyId__c",       "Party Id",      "Text"),
                field("TestDate__c",      "Test Date",     "Date"),
                field("TestType__c",      "Test Type",     "Text"),
                field("ResultStatus__c",  "Result Status", "Text"),
                field("IsAbnormal__c",    "Is Abnormal",   "Number"),
            ],
        },
    ],
    "sports_club": [
        {
            "name": "Membership__dlm",
            "label": "Membership",
            "description": (
                "Club membership plan per member. Tracks plan type, monthly fee, renewal date, and status. Powers renewal-risk and upgrade segmentation."
            ),
            "category": "Other",
            "fields": [
                field("Id__c",               "Id",                  "Text"),
                field("PartyId__c",          "Party Id",            "Text"),
                field("PlanType__c",         "Plan Type",           "Text"),
                field("MonthlyFee__c",       "Monthly Fee",         "Number"),
                field("StartDate__c",        "Start Date",          "Date"),
                field("RenewalDate__c",      "Renewal Date",        "Date"),
                field("RenewingSoon__c",     "Renewing Soon",       "Number"),
                field("MembershipAgeMonths__c","Membership Age Months","Number"),
                field("Status__c",           "Status",              "Text"),
                field("Tier__c",             "Tier",                "Text"),
            ],
        },
        {
            "name": "ActivityRecord__dlm",
            "label": "Activity Record",
            "description": (
                "Gym visits and fitness activity records per member (ENGAGEMENT stream). Tracks activity type, duration, calories. Powers dormancy and high-activity segmentation."
            ),
            "category": "Engagement",
            "fields": [
                field("Id__c",              "Id",               "Text"),
                field("PartyId__c",         "Party Id",         "Text"),
                field("ActivityDate__c",    "Activity Date",    "DateTime"),
                field("ActivityType__c",    "Activity Type",    "Text"),
                field("DurationMinutes__c", "Duration Minutes", "Number"),
                field("Location__c",        "Location",         "Text"),
                field("CaloriesBurned__c",  "Calories Burned",  "Number"),
            ],
        },
    ],
    "ecommerce": [
        {
            "name": "EcomOrder__dlm",
            "label": "Ecom Order",
            "description": (
                "Online purchase order header. ENGAGEMENT stream — order_datetime is the event datetime. "
                "Tracks channel, total amount, payment method, delivery type, and order status including returns. "
                "Powers order profile and customer value CIs, and LTV / dormancy segmentation."
            ),
            "category": "Engagement",
            "fields": [
                field("Id__c",             "Id",              "Text"),
                field("PartyId__c",        "Party Id",        "Text"),
                field("OrderDateTime__c",  "Order Datetime",  "DateTime"),
                field("TotalAmount__c",    "Total Amount",    "Number"),
                field("ItemCount__c",      "Item Count",      "Number"),
                field("Channel__c",        "Channel",         "Text"),
                field("PaymentMethod__c",  "Payment Method",  "Text"),
                field("DeliveryType__c",   "Delivery Type",   "Text"),
                field("Status__c",         "Status",          "Text"),
            ],
        },
        {
            "name": "EcomOrderLine__dlm",
            "label": "Ecom Order Line",
            "description": (
                "Individual line items within an online order. OTHER stream. "
                "Tracks product SKU, category, quantity, unit price, and line total. "
                "Powers category spend and product affinity analysis."
            ),
            "category": "Other",
            "fields": [
                field("Id__c",           "Id",           "Text"),
                field("OrderId__c",      "Order Id",     "Text"),
                field("PartyId__c",      "Party Id",     "Text"),
                field("ProductSku__c",   "Product Sku",  "Text"),
                field("ProductName__c",  "Product Name", "Text"),
                field("Category__c",     "Category",     "Text"),
                field("Quantity__c",     "Quantity",     "Number"),
                field("UnitPrice__c",    "Unit Price",   "Number"),
                field("LineTotal__c",    "Line Total",   "Number"),
            ],
        },
        {
            "name": "CartAbandonment__dlm",
            "label": "Cart Abandonment",
            "description": (
                "Cart abandonment events per shopper. ENGAGEMENT stream — abandonment_datetime is the event datetime. "
                "Tracks product count, cart value, device type, and session id. "
                "Powers cart abandonment CI and re-engagement segmentation."
            ),
            "category": "Engagement",
            "fields": [
                field("Id__c",                  "Id",                   "Text"),
                field("PartyId__c",             "Party Id",             "Text"),
                field("AbandonmentDatetime__c", "Abandonment Datetime", "DateTime"),
                field("ProductCount__c",        "Product Count",        "Number"),
                field("CartValue__c",           "Cart Value",           "Number"),
                field("DeviceType__c",          "Device Type",          "Text"),
                field("SessionId__c",           "Session Id",           "Text"),
            ],
        },
    ],
    "media": [
        {
            "name": "Subscription__dlm", "label": "Subscription",
            "description": "Streaming or pay-TV subscription plan per member. Tracks plan type, billing cycle, monthly fee, and status. Powers churn and upgrade segmentation.",
            "category": "Other",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("PlanType__c","Plan Type","Text"), field("BillingCycle__c","Billing Cycle","Text"),
                field("MonthlyFee__c","Monthly Fee","Number"), field("StartDate__c","Start Date","Date"),
                field("Status__c","Status","Text"), field("IsAnnual__c","Is Annual","Number"),
                field("MonthsSubscribed__c","Months Subscribed","Number"),
            ],
        },
        {
            "name": "ContentView__dlm", "label": "Content View",
            "description": "Content consumption event per subscriber (ENGAGEMENT). Tracks title, genre, content type, watch duration, and completion rate. Powers engagement and low-activity segmentation.",
            "category": "Engagement",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("ViewDatetime__c","View Datetime","DateTime"),
                field("ContentTitle__c","Content Title","Text"), field("Genre__c","Genre","Text"),
                field("ContentType__c","Content Type","Text"),
                field("DurationMinutes__c","Duration Minutes","Number"),
                field("CompletionRate__c","Completion Rate","Number"),
                field("Device__c","Device","Text"),
            ],
        },
    ],
    "automotive": [
        {
            "name": "Vehicle__dlm", "label": "Vehicle",
            "description": "Owned vehicle record per customer. Tracks make, model, fuel type, year, mileage, and purchase price. Powers EV conversion, service-due, and high-value segmentation.",
            "category": "Other",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("Make__c","Make","Text"), field("Model__c","Model","Text"),
                field("Year__c","Year","Number"), field("Fuel__c","Fuel Type","Text"),
                field("PurchaseDate__c","Purchase Date","Date"),
                field("PurchasePrice__c","Purchase Price","Number"),
                field("Mileage__c","Mileage","Number"),
                field("VehicleAgeYears__c","Vehicle Age Years","Number"),
                field("Color__c","Color","Text"),
            ],
        },
        {
            "name": "ServiceRecord__dlm", "label": "Service Record",
            "description": "Workshop and service visit record per customer. Tracks service type, cost, and mileage. Powers service-due and revenue segmentation.",
            "category": "Other",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("VehicleId__c","Vehicle Id","Text"),
                field("ServiceDate__c","Service Date","Date"),
                field("ServiceType__c","Service Type","Text"),
                field("ServiceCost__c","Service Cost","Number"),
                field("Mileage__c","Mileage","Number"),
            ],
        },
    ],
    "real_estate": [
        {
            "name": "PropertyInquiry__dlm", "label": "Property Inquiry",
            "description": "Property interest/inquiry event per prospect (ENGAGEMENT). Tracks property type, area, price range, and intended action (Buy/Rent). Powers active-buyer and high-budget segmentation.",
            "category": "Engagement",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("InquiryDatetime__c","Inquiry Datetime","DateTime"),
                field("PropertyType__c","Property Type","Text"),
                field("Bedrooms__c","Bedrooms","Number"),
                field("ListingPrice__c","Listing Price","Number"),
                field("Area__c","Area","Text"),
                field("Action__c","Action","Text"),
                field("Status__c","Status","Text"),
                field("IsHighBudget__c","Is High Budget","Number"),
            ],
        },
        {
            "name": "PropertyTransaction__dlm", "label": "Property Transaction",
            "description": "Completed property transaction (purchase or rental) per client. Tracks deal value and property details. Powers repeat-investor and revenue segmentation.",
            "category": "Other",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("TransactionDate__c","Transaction Date","Date"),
                field("PropertyType__c","Property Type","Text"),
                field("Area__c","Area","Text"),
                field("DealValue__c","Deal Value","Number"),
                field("Action__c","Action","Text"),
                field("Bedrooms__c","Bedrooms","Number"),
            ],
        },
    ],
    "betting": [
        {
            "name": "BettingAccount__dlm", "label": "Betting Account",
            "description": "Player betting account record. Tracks preferred game type, KYC status, account status, and registration. Powers player profiling and responsible-gambling detection.",
            "category": "Other",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("AccountStatus__c","Account Status","Text"),
                field("PreferredGameType__c","Preferred Game Type","Text"),
                field("KycStatus__c","Kyc Status","Text"),
                field("RegistrationDate__c","Registration Date","Date"),
            ],
        },
        {
            "name": "BettingTransaction__dlm", "label": "Betting Transaction",
            "description": "Deposit, withdrawal, bet, win, or lottery ticket event per player (ENGAGEMENT). Covers sports betting, casino, lottery, and poker. Powers player-value, churn, and responsible-gambling segmentation.",
            "category": "Engagement",
            "fields": [
                field("Id__c","Id","Text"), field("PartyId__c","Party Id","Text"),
                field("TransactionDatetime__c","Transaction Datetime","DateTime"),
                field("TransactionType__c","Transaction Type","Text"),
                field("Amount__c","Amount","Number"),
                field("GameType__c","Game Type","Text"),
                field("GameName__c","Game Name","Text"),
                field("Outcome__c","Outcome","Text"),
            ],
        },
    ],
    "hospitality": [
        {
            "name": "HotelStay__dlm",
            "label": "Hotel Stay",
            "description": (
                "Hotel stay record per guest. ENGAGEMENT stream — checkin_datetime is the event datetime. "
                "Tracks hotel, city, room type, nights, room and F&B revenue, and loyalty points earned. "
                "Powers stay profile and customer value CIs, and frequency / upgrade segmentation."
            ),
            "category": "Engagement",
            "fields": [
                field("Id__c",                  "Id",                   "Text"),
                field("PartyId__c",             "Party Id",             "Text"),
                field("CheckinDatetime__c",     "Checkin Datetime",     "DateTime"),
                field("CheckoutDate__c",        "Checkout Date",        "Date"),
                field("HotelName__c",           "Hotel Name",           "Text"),
                field("City__c",                "City",                 "Text"),
                field("RoomType__c",            "Room Type",            "Text"),
                field("NightsStayed__c",        "Nights Stayed",        "Number"),
                field("RoomRevenue__c",         "Room Revenue",         "Number"),
                field("FnbRevenue__c",          "Fnb Revenue",          "Number"),
                field("TotalRevenue__c",        "Total Revenue",        "Number"),
                field("Status__c",              "Status",               "Text"),
                field("LoyaltyPointsEarned__c", "Loyalty Points Earned","Number"),
            ],
        },
        {
            "name": "LoyaltyTransaction__dlm",
            "label": "Loyalty Transaction",
            "description": (
                "Hotel loyalty points earn and redeem events per guest. ENGAGEMENT stream. "
                "Reused across industries (food, retail, banking, airlines, hospitality) — "
                "will be skipped if already present on the org. "
                "Tracks transaction type, points delta, running balance, and reference."
            ),
            "category": "Engagement",
            "fields": [
                field("Id__c",              "Id",               "Text"),
                field("PartyId__c",         "Party Id",         "Text"),
                field("EventDatetime__c",   "Event Datetime",   "DateTime"),
                field("TransactionType__c", "Transaction Type", "Text"),
                field("Points__c",          "Points",           "Number"),
                field("Balance__c",         "Balance",          "Number"),
                field("Reference__c",       "Reference",        "Text"),
            ],
        },
    ],
}


def existing_dmos(core_url: str, token: str) -> set:
    """Return set of existing DMO developer names (with __dlm suffix)."""
    names = set()
    url = f"{BASE}/data-model-objects?dataspace=default"
    while url:
        st, data = api(core_url, token, "GET", url)
        if st != 200:
            break
        for dmo in data.get("dataModelObjects", []):
            n = dmo.get("developerName") or dmo.get("name", "")
            if n:
                names.add(n)
        url = data.get("nextPageUrl")
    return names


def create_dmo(core_url: str, token: str, name: str, label: str, fields: list,
               category: str = "OTHER") -> tuple:
    """POST a new custom DMO.

    CRITICAL: use dataSpaceName (capital N) and category UPPERCASE
    ("OTHER" | "ENGAGEMENT" | "PROFILE" — not "Other" → 500 UNKNOWN_EXCEPTION).
    """
    body = {
        "name": name,                   # WITHOUT __dlm
        "label": label,
        "dataSpaceName": "default",     # NOT dataSpace
        "category": category.upper(),   # UPPERCASE
        "fields": fields,
    }
    return api(core_url, token, "POST", f"{BASE}/data-model-objects", body)


def patch_dmo_description(core_url: str, token: str, dmo_name: str, description: str) -> tuple:
    """PATCH description on an existing DMO.

    PATCH /services/data/v62.0/ssot/data-model-objects/{dmo_name}?dataspace=default
    """
    return api(
        core_url, token, "PATCH",
        f"{BASE}/data-model-objects/{dmo_name}?dataspace=default",
        {"description": description},
    )


def extend_standard_dmo(core_url: str, token: str, dmo_name: str, fields: list) -> list:
    """Add custom fields to an existing standard DMO (ssot__Individual__dlm or ssot__Account__dlm).

    Endpoint: PATCH /ssot/data-model-objects/{dmo}?dataspace=default
    Body:     {"fields": [{name, label, dataType, ...}, ...]}
    Idempotent: existing fields are silently updated (no duplicate error).
    Returns list of {field, status} results.
    """
    st, resp = api(core_url, token, "PATCH",
                   f"{BASE}/data-model-objects/{dmo_name}?dataspace=default",
                   {"fields": fields})
    if st != 200:
        return [{"field": f["name"], "status": f"error-{st}",
                 "detail": str(resp)[:80]} for f in fields]
    # PATCH response body varies by DMO type — do a GET to verify fields are present.
    st2, resp2 = api(core_url, token, "GET",
                     f"{BASE}/data-model-objects/{dmo_name}?dataspace=default")
    present = {f["name"] for f in (resp2.get("fields") or [])} if isinstance(resp2, dict) else set()
    return [{"field": f["name"],
             "status": "ok" if f["name"] in present else "error-not-in-response"}
            for f in fields]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    slug = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()

    # B2B Account model: food_b2b and hightech map contacts to ssot__Account__dlm
    b2b_account = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")

    print(f"\n🏗️   Creating custom DMOs for {cfg.get('clientName', alias)} ({industry})")
    if b2b_account:
        print(f"    Model: B2B Account (enrichment → ssot__Account__dlm)")
    print(f"    Org: {alias}\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}")

    dmo_specs = STANDARD_DMOS + INDUSTRY_DMOS.get(industry, [])
    if not dmo_specs:
        print(f"  ℹ️  No DMOs defined for industry '{industry}' — skipping")
        return

    # Check which DMOs already exist
    print(f"  Checking existing DMOs...")
    existing = existing_dmos(core_url, core_token)

    results = []
    for spec in dmo_specs:
        name = spec["name"]
        full_name = f"{name}__dlm"  # platform appends __dlm

        if full_name in existing:
            print(f"  ↩  {full_name}  (already exists)")
            results.append({"dmo": full_name, "status": "existing"})
            continue

        print(f"  →  {full_name} ...", end=" ", flush=True)

        if args.dry_run:
            print("[dry-run]")
            results.append({"dmo": full_name, "status": "dry-run"})
            continue

        cat = spec.get("category", "OTHER")
        st, resp = create_dmo(core_url, core_token, name, spec["label"], spec["fields"], cat)
        if st in (200, 201):
            print("✓", end="")
            results.append({"dmo": full_name, "status": "created"})
            # PATCH description if provided
            desc = spec.get("description", "").strip()
            if desc:
                time.sleep(0.3)
                dst, _ = patch_dmo_description(core_url, core_token, full_name, desc)
                print(f"  (desc {'✓' if dst in (200, 201, 204) else '⚠'})")
            else:
                print()
        elif "DUPLICATE" in str(resp).upper() or "already exists" in str(resp).lower():
            print("↩  (duplicate)")
            results.append({"dmo": full_name, "status": "duplicate"})
        else:
            print(f"✗  ({st}: {str(resp)[:120]})")
            results.append({"dmo": full_name, "status": f"error-{st}", "detail": str(resp)[:200]})

        time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] in ("created", "existing", "duplicate"))
    print(f"\n✅  {ok}/{len(results)} DMOs ready")

    # ── Extend standard Individual / Account DMO with custom enrichment fields ──
    # Instead of a separate IndividualProfile DMO, we add custom fields directly
    # to ssot__Individual__dlm (B2C) or ssot__Account__dlm (B2B).
    target_dmo = "ssot__Account__dlm" if b2b_account else "ssot__Individual__dlm"
    print(f"\n  ⬆  Extending {target_dmo} with custom enrichment fields…")
    if args.dry_run:
        print(f"     [dry-run] would POST {len(INDIVIDUAL_CUSTOM_FIELDS)} fields")
    else:
        ext_results = extend_standard_dmo(core_url, core_token, target_dmo, INDIVIDUAL_CUSTOM_FIELDS)
        ok_ext   = sum(1 for r in ext_results if r["status"] == "ok")
        err_ext  = [r for r in ext_results if r["status"].startswith("error")]
        print(f"     {ok_ext}/{len(ext_results)} fields OK")
        for e in err_ext:
            print(f"     ⚠️  {e['field']}: {e.get('detail', e['status'])}")
        results.extend([{"dmo": f"{target_dmo}.{r['field']}", "status": r["status"]} for r in ext_results])

    # ── Extend ssot__WebsiteEngagement__dlm with custom fields ──────────────────
    # Standard DMO — cannot be created, only extended.
    # Adds: PageCategory__c, EventType__c, DurationSeconds__c
    print(f"\n  ⬆  Extending ssot__WebsiteEngagement__dlm with custom fields…")
    if args.dry_run:
        print(f"     [dry-run] would POST {len(WEBSITE_ENGAGEMENT_CUSTOM_FIELDS)} fields")
    else:
        web_results = extend_standard_dmo(
            core_url, core_token, "ssot__WebsiteEngagement__dlm", WEBSITE_ENGAGEMENT_CUSTOM_FIELDS
        )
        ok_web  = sum(1 for r in web_results if r["status"] == "ok")
        err_web = [r for r in web_results if r["status"].startswith("error")]
        print(f"     {ok_web}/{len(web_results)} fields OK")
        for e in err_web:
            print(f"     ⚠️  {e['field']}: {e.get('detail', e['status'])}")
        results.extend([{"dmo": f"ssot__WebsiteEngagement__dlm.{r['field']}", "status": r["status"]} for r in web_results])

    # ── Extend ssot__EmailEngagement__dlm with custom fields ────────────────────
    # Standard DMO — cannot be created, only extended.
    # Adds: OpenedCount__c, ClickedCount__c, UnsubscribedCount__c, CampaignId__c
    print(f"\n  ⬆  Extending ssot__EmailEngagement__dlm with custom fields…")
    if args.dry_run:
        print(f"     [dry-run] would POST {len(EMAIL_ENGAGEMENT_CUSTOM_FIELDS)} fields")
    else:
        email_results = extend_standard_dmo(
            core_url, core_token, "ssot__EmailEngagement__dlm", EMAIL_ENGAGEMENT_CUSTOM_FIELDS
        )
        ok_email  = sum(1 for r in email_results if r["status"] == "ok")
        err_email = [r for r in email_results if r["status"].startswith("error")]
        print(f"     {ok_email}/{len(email_results)} fields OK")
        for e in err_email:
            print(f"     ⚠️  {e['field']}: {e.get('detail', e['status'])}")
        results.extend([{"dmo": f"ssot__EmailEngagement__dlm.{r['field']}", "status": r["status"]} for r in email_results])

    # Persist
    out_dir = Path(cfg.get("outputDir", f"data/{slug}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dmo_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
