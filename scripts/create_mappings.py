#!/usr/bin/env python3
"""
Create DLO → DMO mappings for all seeded streams.

Usage:
    python3 create_mappings.py --config config.json

Standard mappings (always created):
  <Prefix>_Contacts           → ssot__Individual__dlm     (id + ALL enrichment fields direct on Individual)
  <Prefix>_Contact_Emails     → ssot__ContactPointEmail__dlm
  <Prefix>_Email_Engagement   → ssot__EmailEngagement__dlm      (every industry — platform standard)
  <Prefix>_Web_Engagement     → ssot__WebsiteEngagement__dlm   (every industry — platform standard ENGAGEMENT)

Note: Phone and address streams are NOT generated (IR uses email+name only).
Note: Enrichment fields (churn_score, ltv, value_tier, etc.) are mapped directly to
  ssot__Individual__dlm (B2C) or ssot__Account__dlm (B2B). No separate IndividualProfile DMO.

Industry-specific mapping (custom DMO):
  insurance:  <Prefix>_Insurance_Policies  → InsurancePolicy__dlm
              <Prefix>_Insurance_Claims    → InsuranceClaim__dlm
  food:       <Prefix>_Purchase_Orders     → PurchaseOrder__dlm
              <Prefix>_Order_Lines         → OrderLine__dlm
              <Prefix>_Loyalty_Transactions→ LoyaltyTransaction__dlm
  retail:     <Prefix>_Sales_Orders        → SalesOrder__dlm
              <Prefix>_Order_Lines         → OrderLine__dlm
              <Prefix>_Loyalty_Transactions→ LoyaltyTransaction__dlm
  banking:    <Prefix>_Financial_Accounts  → FinancialAccount__dlm
              <Prefix>_Transactions        → Transaction__dlm       (ENGAGEMENT)
              <Prefix>_Banking_Products    → BankingProduct__dlm
              <Prefix>_Loyalty_Transactions→ LoyaltyTransaction__dlm
  pharma:     <Prefix>_Prescriptions       → Prescription__dlm
  telco:      <Prefix>_Service_Contracts   → ServiceContract__dlm
              <Prefix>_Usage_Records       → UsageRecord__dlm
  food_b2b:   <Prefix>_Wholesale_Orders    → WholesaleOrder__dlm
              <Prefix>_Wholesale_Order_Lines → WholesaleOrderLine__dlm
              <Prefix>_Loyalty_Transactions→ LoyaltyTransaction__dlm
  hightech:   <Prefix>_Ht_Subscriptions   → HtSubscription__dlm
              <Prefix>_Ht_Usage_Records    → HtUsageRecord__dlm
              <Prefix>_Ht_Support_Tickets  → HtSupportTicket__dlm

Idempotent: DUPLICATE_DLO_TO_DMO_MAPPING responses are treated as success.
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

# ─── mapping specs ───────────────────────────────────────────────────────────
# Each entry: (dlo_suffix, dmo_name, [(dlo_field, dmo_field), ...])
# dlo_suffix = the stream name suffix (e.g. "Contacts" → <Prefix>_Contacts)
# dmo_name   = target DMO api name
# fields     = [(source_field, target_field)] — only the key/join fields need explicit mapping;
#              the platform picks up the rest as individual attributes automatically.

STANDARD_MAPPINGS = [
    # Individual — identity fields + ALL enrichment fields mapped directly.
    # Enrichment custom fields are added to ssot__Individual__dlm via create_dmos.extend_standard_dmo().
    # This eliminates the separate IndividualProfile DMO.
    (
        "Contacts",
        "ssot__Individual__dlm",
        [
            ("id",           "ssot__Id__c"),
            ("first_name",   "ssot__FirstName__c"),
            ("last_name",    "ssot__LastName__c"),
            ("birth_date",   "ssot__BirthDate__c"),    # DateTime in CSV → maps OK
            ("created_date", "ssot__CreatedDate__c"),  # DateTime in CSV → maps OK
            ("gender",       "ssot__GenderIdentity__c"),
            # Enrichment — custom fields added by extend_standard_dmo()
            ("churn_score",            "ChurnScore__c"),
            ("loyalty_tier",           "LoyaltyTier__c"),
            ("loyalty_points_balance", "LoyaltyPointsBalance__c"),
            ("points_earned_ytd",      "PointsEarnedYtd__c"),
            ("points_redeemed_ytd",    "PointsRedeemedYtd__c"),
            ("ltv",                    "Ltv__c"),
            ("nps_score",              "NpsScore__c"),
            ("customer_since",         "CustomerSince__c"),
            ("income_range",           "IncomeRange__c"),
            ("source",                 "Source__c"),
            ("value_tier",             "ValueTier__c"),
            ("digital_active",         "DigitalActive__c"),
            ("preferred_channel",      "PreferredChannel__c"),
            ("acquisition_channel",    "AcquisitionChannel__c"),
            ("days_since_last_purchase","DaysSinceLastPurchase__c"),
            ("rfm_segment",            "RfmSegment__c"),
            ("predicted_ltv",          "PredictedLtv__c"),
            ("product_affinity",       "ProductAffinity__c"),
        ],
    ),
    # Contact Point Email
    (
        "Contact_Emails",
        "ssot__ContactPointEmail__dlm",
        [
            ("id",               "ssot__Id__c"),
            ("contact_id",       "ssot__PartyId__c"),
            ("email_address",    "ssot__EmailAddress__c"),
            ("active_from_date", "ssot__ActiveFromDate__c"),  # DateTime in CSV → maps OK
        ],
    ),
    # NOTE: Contact_Phones and Contact_Addresses are intentionally OMITTED.
    # Phone/address streams are not generated — IR uses email + name only.
    # Adding them back here would produce "stream-not-found" warnings on every run.

    # EmailEngagement — standard for ALL B2C industries
    # Target: ssot__EmailEngagement__dlm (platform standard DMO, ENGAGEMENT category)
    # Custom fields added by create_dmos.extend_standard_dmo(): OpenedCount__c, ClickedCount__c,
    #   UnsubscribedCount__c, CampaignId__c.
    # Standard fields: ssot__Id__c (PK), ssot__IndividualId__c (FK → Individual),
    #   ssot__EngagementDateTm__c (= eventDateTimeFieldName), ssot__SendtimeEmailAddress__c,
    #   ssot__EmailName__c.
    (
        "Email_Engagement",
        "ssot__EmailEngagement__dlm",
        [
            ("event_id",      "ssot__Id__c"),
            ("contact_id",    "ssot__IndividualId__c"),
            ("email",         "ssot__SendtimeEmailAddress__c"),
            ("campaign_name", "ssot__EmailName__c"),
            ("sent_date",     "ssot__EngagementDateTm__c"),    # eventDateTimeFieldName for Engagement DLO
            ("campaign_id",   "CampaignId__c"),                # custom field
            ("opened",        "OpenedCount__c"),               # custom Number field (0/1 CSV → Number)
            ("clicked",       "ClickedCount__c"),              # custom Number field
            ("unsubscribed",  "UnsubscribedCount__c"),         # custom Number field
        ],
    ),
    # WebEngagement — standard for ALL B2C industries (ENGAGEMENT DLO + ENGAGEMENT DMO)
    # Target: ssot__WebsiteEngagement__dlm (platform standard DMO, ENGAGEMENT category)
    # Custom fields added by create_dmos.extend_standard_dmo(): PageCategory__c, EventType__c,
    #   DurationSeconds__c.
    # Standard fields: ssot__Id__c (PK), ssot__IndividualId__c (FK → Individual),
    #   ssot__SessionId__c, ssot__EngagementDateTm__c, ssot__PageURL__c, ssot__DeviceTypeTxt__c.
    (
        "Web_Engagement",
        "ssot__WebsiteEngagement__dlm",
        [
            ("event_id",         "ssot__Id__c"),
            ("contact_id",       "ssot__IndividualId__c"),
            ("session_id",       "ssot__SessionId__c"),
            ("event_datetime",   "ssot__EngagementDateTm__c"),  # eventDateTimeFieldName for Engagement DLO
            ("page_url",         "ssot__PageURL__c"),
            ("device_type",      "ssot__DeviceTypeTxt__c"),
            ("page_category",    "PageCategory__c"),            # custom field
            ("event_type",       "EventType__c"),               # custom field
            ("duration_seconds", "DurationSeconds__c"),         # custom field
        ],
    ),
]

# B2B Account standard mappings — used instead of STANDARD_MAPPINGS for food_b2b / hightech.
# Contacts map to ssot__Account__dlm (company-level entity) rather than ssot__Individual__dlm.
# Contact_Emails map to ssot__AccountEmailAddress__dlm (FK = ssot__AccountId__c, not ssot__PartyId__c).
B2B_STANDARD_MAPPINGS = [
    # Account — company-level identity entity + ALL enrichment fields mapped directly.
    # Enrichment custom fields are added to ssot__Account__dlm via create_dmos.extend_standard_dmo().
    (
        "Contacts",
        "ssot__Account__dlm",
        [
            ("id",           "ssot__Id__c"),
            ("company_name", "ssot__Name__c"),            # Company name is the B2B identity field
            ("email",        "ssot__PrimaryEmailAddress__c"),
            ("city",         "ssot__BillingCity__c"),
            ("country",      "ssot__BillingCountry__c"),
            # Enrichment — custom fields added by extend_standard_dmo()
            ("churn_score",            "ChurnScore__c"),
            ("loyalty_tier",           "LoyaltyTier__c"),
            ("loyalty_points_balance", "LoyaltyPointsBalance__c"),
            ("points_earned_ytd",      "PointsEarnedYtd__c"),
            ("points_redeemed_ytd",    "PointsRedeemedYtd__c"),
            ("ltv",                    "Ltv__c"),
            ("nps_score",              "NpsScore__c"),
            ("customer_since",         "CustomerSince__c"),
            ("income_range",           "IncomeRange__c"),
            ("source",                 "Source__c"),
            # B2B-only fields
            ("number_of_employees",    "NumberOfEmployees__c"),
            ("annual_revenue",         "AnnualRevenue__c"),
            ("value_tier",             "ValueTier__c"),
            ("digital_active",         "DigitalActive__c"),
            ("preferred_channel",      "PreferredChannel__c"),
            ("acquisition_channel",    "AcquisitionChannel__c"),
            ("days_since_last_purchase","DaysSinceLastPurchase__c"),
            ("rfm_segment",            "RfmSegment__c"),
            ("predicted_ltv",          "PredictedLtv__c"),
            ("product_affinity",       "ProductAffinity__c"),
        ],
    ),
    # AccountEmailAddress — B2B equivalent of ContactPointEmail
    # FK = ssot__AccountId__c (NOT ssot__PartyId__c — that field does not exist on this DMO)
    (
        "Contact_Emails",
        "ssot__AccountEmailAddress__dlm",
        [
            ("id",               "ssot__Id__c"),
            ("contact_id",       "ssot__AccountId__c"),   # FK → Account.ssot__Id__c
            ("email_address",    "ssot__EmailAddress__c"),
            ("active_from_date", "ssot__ActiveFromDate__c"),
        ],
    ),
    # EmailEngagement — B2B (ssot__IndividualId__c stores the account source record ID)
    (
        "Email_Engagement",
        "ssot__EmailEngagement__dlm",
        [
            ("event_id",      "ssot__Id__c"),
            ("contact_id",    "ssot__IndividualId__c"),  # account source record ID goes here
            ("email",         "ssot__SendtimeEmailAddress__c"),
            ("campaign_name", "ssot__EmailName__c"),
            ("sent_date",     "ssot__EngagementDateTm__c"),
            ("campaign_id",   "CampaignId__c"),
            ("opened",        "OpenedCount__c"),
            ("clicked",       "ClickedCount__c"),
            ("unsubscribed",  "UnsubscribedCount__c"),
        ],
    ),
    # WebEngagement — B2B (ssot__IndividualId__c stores the account source record ID)
    (
        "Web_Engagement",
        "ssot__WebsiteEngagement__dlm",
        [
            ("event_id",         "ssot__Id__c"),
            ("contact_id",       "ssot__IndividualId__c"),  # account source record ID
            ("session_id",       "ssot__SessionId__c"),
            ("event_datetime",   "ssot__EngagementDateTm__c"),
            ("page_url",         "ssot__PageURL__c"),
            ("device_type",      "ssot__DeviceTypeTxt__c"),
            ("page_category",    "PageCategory__c"),
            ("event_type",       "EventType__c"),
            ("duration_seconds", "DurationSeconds__c"),
        ],
    ),
]

# Industry-specific custom DMO mappings.
# Each entry: (dlo_suffix, dmo_name, [(dlo_field, dmo_field), ...])
# Multiple DMOs per industry — one per logical table. Relationships between
# them are registered separately in create_relationships.py.
#
# NOTE: EmailEngagement is NO LONGER listed here — it lives in STANDARD_MAPPINGS.
INDUSTRY_CUSTOM_MAPPINGS = {
    "insurance": [
        # InsurancePolicy — N:1 with Individual (via PartyId__c)
        (
            "Insurance_Policies",
            "InsurancePolicy__dlm",
            [
                ("policy_id",         "Id__c"),
                ("contact_id",        "PartyId__c"),   # FK → Individual.ssot__Id__c
                ("policy_number",     "PolicyNumber__c"),
                ("product_name",      "ProductName__c"),
                ("product_category",  "ProductCategory__c"),
                ("premium_monthly",   "PremiumMonthly__c"),
                ("premium_annual",    "PremiumAnnual__c"),
                ("coverage_amount",   "CoverageAmount__c"),
                ("start_date",        "StartDate__c"),
                ("end_date",          "EndDate__c"),
                ("status",            "Status__c"),
            ],
        ),
        # InsuranceClaim — N:1 with InsurancePolicy (via PolicyId__c) AND N:1 with Individual
        (
            "Insurance_Claims",
            "InsuranceClaim__dlm",
            [
                ("claim_id",       "Id__c"),
                ("policy_id",      "PolicyId__c"),     # FK → InsurancePolicy.Id__c
                ("contact_id",     "PartyId__c"),      # FK → Individual.ssot__Id__c (for direct join)
                ("claim_date",     "ClaimDate__c"),
                ("claim_type",     "ClaimType__c"),
                ("claim_amount",   "ClaimAmount__c"),
                ("status",         "Status__c"),
                ("resolution_date","ResolutionDate__c"),
            ],
        ),
    ],
    "food": [
        # PurchaseOrder — N:1 with Individual
        (
            "Purchase_Orders",
            "PurchaseOrder__dlm",
            [
                ("order_id",               "Id__c"),
                ("contact_id",             "PartyId__c"),
                ("order_datetime",         "OrderDatetime__c"),  # renamed from order_date/OrderDate__c — now DateTime for ENGAGEMENT
                ("store_name",             "StoreName__c"),
                ("channel",                "Channel__c"),
                ("total_amount",           "TotalAmount__c"),
                ("loyalty_points_earned",  "LoyaltyPointsEarned__c"),
            ],
        ),
        # OrderLine — N:1 with PurchaseOrder AND N:1 with Individual
        (
            "Order_Lines",
            "OrderLine__dlm",
            [
                ("line_id",        "Id__c"),
                ("order_id",       "OrderId__c"),    # FK → PurchaseOrder.Id__c
                ("contact_id",     "PartyId__c"),
                ("product_sku",    "ProductSku__c"),
                ("product_name",   "ProductName__c"),
                ("category",       "Category__c"),
                ("quantity",       "Quantity__c"),
                ("unit_price",     "UnitPrice__c"),
                ("line_total",     "LineTotal__c"),
            ],
        ),
        # LoyaltyTransaction — N:1 with Individual (ENGAGEMENT stream — event_datetime required)
        (
            "Loyalty_Transactions",
            "LoyaltyTransaction__dlm",
            [
                ("tx_id",          "Id__c"),
                ("contact_id",     "PartyId__c"),
                ("event_datetime", "EventDatetime__c"),  # was date/TransactionDate__c (Date) — now DateTime
                ("type",           "TransactionType__c"),
                ("points",         "Points__c"),
                ("balance",        "Balance__c"),
            ],
        ),
    ],
    "retail": [
        # SalesOrder — N:1 with Individual
        (
            "Sales_Orders",
            "SalesOrder__dlm",
            [
                ("order_id",       "Id__c"),
                ("contact_id",     "PartyId__c"),
                ("order_datetime", "OrderDatetime__c"),  # renamed from order_date/OrderDate__c — now DateTime for ENGAGEMENT
                ("channel",        "Channel__c"),
                ("total_amount",   "TotalAmount__c"),
                ("status",         "Status__c"),
            ],
        ),
        # OrderLine — N:1 with SalesOrder AND Individual
        (
            "Order_Lines",
            "OrderLine__dlm",
            [
                ("line_id",      "Id__c"),
                ("order_id",     "OrderId__c"),   # FK → SalesOrder.Id__c
                ("contact_id",   "PartyId__c"),
                ("product_sku",  "ProductSku__c"),
                ("product_name", "ProductName__c"),
                ("category",     "Category__c"),
                ("quantity",     "Quantity__c"),
                ("unit_price",   "UnitPrice__c"),
                ("line_total",   "LineTotal__c"),
            ],
        ),
        # LoyaltyTransaction — ENGAGEMENT (earn/redeem events from sales orders)
        (
            "Loyalty_Transactions",
            "LoyaltyTransaction__dlm",
            [
                ("tx_id",          "Id__c"),
                ("contact_id",     "PartyId__c"),
                ("event_datetime", "EventDatetime__c"),
                ("type",           "TransactionType__c"),
                ("points",         "Points__c"),
                ("balance",        "Balance__c"),
            ],
        ),
    ],
    "banking": [
        (
            "Financial_Accounts",
            "FinancialAccount__dlm",
            [
                ("account_id",    "Id__c"),
                ("contact_id",    "PartyId__c"),
                ("account_type",  "AccountType__c"),
                ("balance",       "Balance__c"),
                ("opened_date",   "OpenedDate__c"),
                ("status",        "Status__c"),
            ],
        ),
        (
            "Transactions",
            "Transaction__dlm",
            [
                ("tx_id",        "Id__c"),
                ("account_id",   "AccountId__c"),
                ("contact_id",   "PartyId__c"),
                ("tx_datetime",  "TxDatetime__c"),   # was tx_date/TxDate__c (Date) — now DateTime
                ("category",     "Category__c"),
                ("amount",       "Amount__c"),
            ],
        ),
        # BankingProduct — OTHER stream (mutable product holdings)
        (
            "Banking_Products",
            "BankingProduct__dlm",
            [
                ("product_id",    "Id__c"),
                ("contact_id",    "PartyId__c"),
                ("product_type",  "ProductType__c"),
                ("product_name",  "ProductName__c"),
                ("amount",        "Amount__c"),
                ("interest_rate", "InterestRate__c"),
                ("status",        "Status__c"),
                ("opened_date",   "OpenedDate__c"),
            ],
        ),
        # LoyaltyTransaction — ENGAGEMENT (earn/redeem events from transactions)
        (
            "Loyalty_Transactions",
            "LoyaltyTransaction__dlm",
            [
                ("tx_id",          "Id__c"),
                ("contact_id",     "PartyId__c"),
                ("event_datetime", "EventDatetime__c"),
                ("type",           "TransactionType__c"),
                ("points",         "Points__c"),
                ("balance",        "Balance__c"),
            ],
        ),
    ],
    "pharma": [
        (
            "Prescriptions",
            "Prescription__dlm",
            [
                ("rx_id",              "Id__c"),
                ("contact_id",         "PartyId__c"),
                ("drug_name",          "DrugName__c"),
                ("therapeutic_area",   "TherapeuticArea__c"),
                ("diagnosis",          "Diagnosis__c"),
                ("fill_datetime",      "FillDatetime__c"),  # renamed from prescribed_date/PrescribedDate__c — now DateTime for ENGAGEMENT
                ("status",             "Status__c"),
            ],
        ),
    ],
    "telco": [
        (
            "Service_Contracts",
            "ServiceContract__dlm",
            [
                ("contract_id",   "Id__c"),
                ("contact_id",    "PartyId__c"),
                ("plan_name",     "PlanName__c"),
                ("plan_type",     "PlanType__c"),
                ("monthly_fee",   "MonthlyFee__c"),
                ("start_date",    "StartDate__c"),
                ("status",        "Status__c"),
            ],
        ),
        (
            "Usage_Records",
            "UsageRecord__dlm",
            [
                ("usage_id",            "Id__c"),
                ("contract_id",         "ContractId__c"),
                ("contact_id",          "PartyId__c"),
                ("usage_date",          "UsageDate__c"),   # renamed from month/UsageMonth__c — now Date (YYYY-MM-01) for native range filtering
                ("data_used_gb",        "DataUsedGb__c"),
                ("voice_minutes_used",  "VoiceMinutesUsed__c"),
                ("sms_count",           "SmsCount__c"),
                ("overage_charge",      "OverageCharge__c"),
            ],
        ),
    ],
    "food_b2b": [
        (
            "Wholesale_Orders",
            "WholesaleOrder__dlm",
            [
                ("order_id",        "Id__c"),
                ("contact_id",      "PartyId__c"),
                ("order_datetime",  "OrderDatetime__c"),  # renamed from order_date/OrderDate__c — now DateTime for ENGAGEMENT
                ("total_amount",    "TotalAmount__c"),
                ("item_count",    "ItemCount__c"),
                ("status",        "Status__c"),
                ("payment_terms", "PaymentTerms__c"),
                ("sales_rep",     "SalesRep__c"),
            ],
        ),
        (
            "Wholesale_Order_Lines",
            "WholesaleOrderLine__dlm",
            [
                ("line_id",        "Id__c"),
                ("order_id",       "OrderId__c"),
                ("contact_id",     "PartyId__c"),
                ("product_sku",    "ProductSku__c"),
                ("product_name",   "ProductName__c"),
                ("category",       "Category__c"),
                ("quantity",       "Quantity__c"),
                ("unit_price",     "UnitPrice__c"),
                ("line_total",     "LineTotal__c"),
                ("is_promotional", "IsPromotional__c"),
            ],
        ),
        # LoyaltyTransaction — ENGAGEMENT (earn/redeem events from wholesale orders, B2B Account model)
        (
            "Loyalty_Transactions",
            "LoyaltyTransaction__dlm",
            [
                ("tx_id",          "Id__c"),
                ("contact_id",     "PartyId__c"),   # PartyId__c = account source record ID
                ("event_datetime", "EventDatetime__c"),
                ("type",           "TransactionType__c"),
                ("points",         "Points__c"),
                ("balance",        "Balance__c"),
            ],
        ),
    ],
    "utilities": [
        (
            "Utility_Contracts",
            "UtilityContract__dlm",
            [
                ("contract_id",  "Id__c"),
                ("contact_id",   "PartyId__c"),
                ("plan_type",    "PlanType__c"),
                ("plan_name",    "PlanName__c"),
                ("monthly_fee",  "MonthlyFee__c"),
                ("start_date",   "StartDate__c"),
                ("status",       "Status__c"),
            ],
        ),
        (
            "Consumption_Records",
            "ConsumptionRecord__dlm",
            [
                ("record_id",         "Id__c"),
                ("contract_id",       "ContractId__c"),
                ("contact_id",        "PartyId__c"),
                ("usage_date",        "UsageDate__c"),
                ("consumption_value", "ConsumptionValue__c"),
                ("consumption_unit",  "ConsumptionUnit__c"),
                ("monthly_bill",      "MonthlyBill__c"),
                ("overage_charge",    "OverageCharge__c"),
            ],
        ),
    ],
    "airlines": [
        (
            "Flight_Bookings",
            "FlightBooking__dlm",
            [
                ("booking_id",       "Id__c"),
                ("contact_id",       "PartyId__c"),
                ("booking_datetime", "BookingDatetime__c"),
                ("origin",           "Origin__c"),
                ("destination",      "Destination__c"),
                ("cabin_class",      "CabinClass__c"),
                ("base_fare",        "BaseFare__c"),
                ("miles_earned",     "MilesEarned__c"),
                ("status",           "Status__c"),
            ],
        ),
        (
            "Loyalty_Transactions",
            "LoyaltyTransaction__dlm",
            [
                ("tx_id",          "Id__c"),
                ("contact_id",     "PartyId__c"),
                ("event_datetime", "EventDatetime__c"),
                ("type",           "TransactionType__c"),
                ("points",         "Points__c"),
                ("balance",        "Balance__c"),
            ],
        ),
    ],
    "hightech": [
        (
            "Ht_Subscriptions",
            "HtSubscription__dlm",
            [
                ("sub_id",            "Id__c"),
                ("contact_id",        "PartyId__c"),
                ("product_name",      "ProductName__c"),
                ("tier",              "Tier__c"),
                ("seats",             "Seats__c"),
                ("mrr",               "Mrr__c"),
                ("start_date",        "StartDate__c"),
                ("renewal_date",      "RenewalDate__c"),
                ("status",            "Status__c"),
                ("days_until_renewal","DaysUntilRenewal__c"),
            ],
        ),
        (
            "Ht_Usage_Records",
            "HtUsageRecord__dlm",
            [
                ("usage_id",               "Id__c"),
                ("subscription_id",        "SubscriptionId__c"),
                ("contact_id",             "PartyId__c"),
                ("usage_date",             "UsageDate__c"),   # renamed from usage_month/UsageMonth__c — now Date (YYYY-MM-01)
                ("active_users",           "ActiveUsers__c"),
                ("login_count",            "LoginCount__c"),
                ("feature_adoption_score", "FeatureAdoptionScore__c"),
                ("data_volume_gb",         "DataVolumeGb__c"),
            ],
        ),
        (
            "Ht_Support_Tickets",
            "HtSupportTicket__dlm",
            [
                ("ticket_id",         "Id__c"),
                ("contact_id",        "PartyId__c"),
                ("created_date",      "CreatedDate__c"),
                ("category",          "Category__c"),
                ("severity",          "Severity__c"),
                ("status",            "Status__c"),
                ("resolution_days",   "ResolutionDays__c"),
                ("csat_score",        "CsatScore__c"),
                ("days_since_opened", "DaysSinceOpened__c"),  # pre-computed integer — used in SupportProfile CI
            ],
        ),
    ],
    "healthcare": [
        ("Medical_Visits", "MedicalVisit__dlm", [
            ("visit_id",       "Id__c"),
            ("contact_id",     "PartyId__c"),
            ("visit_date",     "VisitDate__c"),
            ("specialty",      "Specialty__c"),
            ("visit_type",     "VisitType__c"),
            ("copay_amount",   "CopayAmount__c"),
            ("diagnosis_code", "DiagnosisCode__c"),
        ]),
        ("Lab_Results", "LabResult__dlm", [
            ("result_id",      "Id__c"),
            ("contact_id",     "PartyId__c"),
            ("test_date",      "TestDate__c"),
            ("test_type",      "TestType__c"),
            ("result_status",  "ResultStatus__c"),
            ("is_abnormal",    "IsAbnormal__c"),
        ]),
    ],
    "sports_club": [
        ("Memberships", "Membership__dlm", [
            ("membership_id",         "Id__c"),
            ("contact_id",            "PartyId__c"),
            ("plan_type",             "PlanType__c"),
            ("monthly_fee",           "MonthlyFee__c"),
            ("start_date",            "StartDate__c"),
            ("renewal_date",          "RenewalDate__c"),
            ("renewing_soon",         "RenewingSoon__c"),
            ("membership_age_months", "MembershipAgeMonths__c"),
            ("status",                "Status__c"),
            ("tier",                  "Tier__c"),
        ]),
        ("Activity_Records", "ActivityRecord__dlm", [
            ("activity_id",      "Id__c"),
            ("contact_id",       "PartyId__c"),
            ("activity_date",    "ActivityDate__c"),
            ("activity_type",    "ActivityType__c"),
            ("duration_minutes", "DurationMinutes__c"),
            ("location",         "Location__c"),
            ("calories_burned",  "CaloriesBurned__c"),
        ]),
    ],
    "ecommerce": [
        ("Ecom_Orders", "EcomOrder__dlm", [
            ("order_id",        "Id__c"),
            ("contact_id",      "PartyId__c"),
            ("order_datetime",  "OrderDateTime__c"),
            ("total_amount",    "TotalAmount__c"),
            ("item_count",      "ItemCount__c"),
            ("channel",         "Channel__c"),
            ("payment_method",  "PaymentMethod__c"),
            ("delivery_type",   "DeliveryType__c"),
            ("status",          "Status__c"),
        ]),
        ("Ecom_Order_Lines", "EcomOrderLine__dlm", [
            ("line_id",       "Id__c"),
            ("order_id",      "OrderId__c"),
            ("contact_id",    "PartyId__c"),
            ("product_sku",   "ProductSku__c"),
            ("product_name",  "ProductName__c"),
            ("category",      "Category__c"),
            ("quantity",      "Quantity__c"),
            ("unit_price",    "UnitPrice__c"),
            ("line_total",    "LineTotal__c"),
        ]),
        ("Cart_Abandonments", "CartAbandonment__dlm", [
            ("abandonment_id",       "Id__c"),
            ("contact_id",           "PartyId__c"),
            ("abandonment_datetime", "AbandonmentDatetime__c"),
            ("product_count",        "ProductCount__c"),
            ("cart_value",           "CartValue__c"),
            ("device_type",          "DeviceType__c"),
            ("session_id",           "SessionId__c"),
        ]),
    ],
    "hospitality": [
        ("Hotel_Stays", "HotelStay__dlm", [
            ("stay_id",               "Id__c"),
            ("contact_id",            "PartyId__c"),
            ("checkin_datetime",      "CheckinDatetime__c"),
            ("checkout_date",         "CheckoutDate__c"),
            ("hotel_name",            "HotelName__c"),
            ("city",                  "City__c"),
            ("room_type",             "RoomType__c"),
            ("nights_stayed",         "NightsStayed__c"),
            ("room_revenue",          "RoomRevenue__c"),
            ("fnb_revenue",           "FnbRevenue__c"),
            ("total_revenue",         "TotalRevenue__c"),
            ("status",                "Status__c"),
            ("loyalty_points_earned", "LoyaltyPointsEarned__c"),
        ]),
        ("Loyalty_Transactions", "LoyaltyTransaction__dlm", [
            ("tx_id",       "Id__c"),
            ("contact_id",  "PartyId__c"),
            ("event_datetime", "EventDatetime__c"),
            ("type",        "TransactionType__c"),
            ("points",      "Points__c"),
            ("balance",     "Balance__c"),
            ("reference",   "Reference__c"),
        ]),
    ],
    "media": [
        ("Subscriptions", "Subscription__dlm", [
            ("subscription_id", "Id__c"),
            ("contact_id",      "PartyId__c"),
            ("plan_name",       "PlanName__c"),
            ("plan_type",       "PlanType__c"),
            ("monthly_fee",     "MonthlyFee__c"),
            ("start_date",      "StartDate__c"),
            ("status",          "Status__c"),
        ]),
        ("Content_Views", "ContentView__dlm", [
            ("view_id",         "Id__c"),
            ("contact_id",      "PartyId__c"),
            ("view_datetime",   "ViewDatetime__c"),
            ("content_id",      "ContentId__c"),
            ("title",           "Title__c"),
            ("genre",           "Genre__c"),
            ("duration_minutes","DurationMinutes__c"),
            ("device_type",     "DeviceType__c"),
            ("completed",       "Completed__c"),
        ]),
    ],
    "automotive": [
        ("Vehicles", "Vehicle__dlm", [
            ("vehicle_id",   "Id__c"),
            ("contact_id",   "PartyId__c"),
            ("vin",          "Vin__c"),
            ("make",         "Make__c"),
            ("model",        "Model__c"),
            ("year",         "Year__c"),
            ("trim",         "Trim__c"),
            ("color",        "Color__c"),
            ("purchase_date","PurchaseDate__c"),
            ("purchase_price","PurchasePrice__c"),
            ("status",       "Status__c"),
        ]),
        ("Service_Records", "ServiceRecord__dlm", [
            ("service_id",        "Id__c"),
            ("contact_id",        "PartyId__c"),
            ("vehicle_id",        "VehicleId__c"),
            ("service_date",      "ServiceDate__c"),
            ("service_type",      "ServiceType__c"),
            ("mileage",           "Mileage__c"),
            ("labor_cost",        "LaborCost__c"),
            ("parts_cost",        "PartsCost__c"),
            ("total_cost",        "TotalCost__c"),
            ("technician",        "Technician__c"),
        ]),
    ],
    "real_estate": [
        ("Property_Inquiries", "PropertyInquiry__dlm", [
            ("inquiry_id",      "Id__c"),
            ("contact_id",      "PartyId__c"),
            ("inquiry_datetime","InquiryDatetime__c"),
            ("property_id",     "PropertyId__c"),
            ("property_type",   "PropertyType__c"),
            ("listing_price",   "ListingPrice__c"),
            ("bedrooms",        "Bedrooms__c"),
            ("city",            "City__c"),
            ("channel",         "Channel__c"),
        ]),
        ("Property_Transactions", "PropertyTransaction__dlm", [
            ("transaction_id",   "Id__c"),
            ("contact_id",       "PartyId__c"),
            ("property_id",      "PropertyId__c"),
            ("transaction_type", "TransactionType__c"),
            ("close_date",       "CloseDate__c"),
            ("sale_price",       "SalePrice__c"),
            ("property_type",    "PropertyType__c"),
            ("bedrooms",         "Bedrooms__c"),
            ("city",             "City__c"),
            ("agent_name",       "AgentName__c"),
            ("commission",       "Commission__c"),
        ]),
    ],
    "betting": [
        ("Betting_Accounts", "BettingAccount__dlm", [
            ("account_id",        "Id__c"),
            ("contact_id",        "PartyId__c"),
            ("account_type",      "AccountType__c"),
            ("registration_date", "RegistrationDate__c"),
            ("kyc_status",        "KycStatus__c"),
            ("deposit_limit",     "DepositLimit__c"),
            ("balance",           "Balance__c"),
            ("status",            "Status__c"),
            ("responsible_gaming_flag", "ResponsibleGamingFlag__c"),
        ]),
        ("Betting_Transactions", "BettingTransaction__dlm", [
            ("tx_id",               "Id__c"),
            ("contact_id",          "PartyId__c"),
            ("transaction_datetime","TransactionDatetime__c"),
            ("game_type",           "GameType__c"),
            ("game_name",           "GameName__c"),
            ("stake",               "Stake__c"),
            ("payout",              "Payout__c"),
            ("net_result",          "NetResult__c"),
            ("channel",             "Channel__c"),
        ]),
    ],
}


def list_dlos(core_url: str, token: str) -> dict[str, str]:
    """Return {streamName: dloApiName} for all data streams (paginates)."""
    url = f"{BASE}/data-streams?dataspace=default"
    result = {}
    while url:
        status, data = api(core_url, token, "GET", url)
        if status != 200:
            break
        for s in data.get("dataStreams", []):
            # stream name is the path key; DLO name lives in dataLakeObjectInfo.name
            sname = s.get("name", "")
            dlo = (s.get("dataLakeObjectInfo") or {}).get("name") or sname
            if sname:
                result[sname] = dlo
        url = data.get("nextPageUrl")
    return result


def existing_mappings(core_url: str, token: str, dmos: list = None) -> set:
    """Return set of (sourceEntityDeveloperName, targetEntityDeveloperName) already mapped.

    The GET endpoint requires ?dmoDeveloperName=X to filter — no unfiltered list.
    We query each DMO we plan to map to.
    """
    ALL_DMOS = [
        "ssot__Individual__dlm",
        "ssot__ContactPointEmail__dlm",
        "ssot__ContactPointPhone__dlm",
        "ssot__ContactPointAddress__dlm",
        "ssot__Account__dlm",              # B2B Account (food_b2b, hightech)
        "ssot__AccountEmailAddress__dlm",  # B2B AccountEmailAddress
        # NOTE: IndividualProfile__dlm removed — enrichment fields now on ssot__Individual__dlm/Account
        "ssot__EmailEngagement__dlm",      # platform standard engagement (all industries)
        "ssot__WebsiteEngagement__dlm",    # platform standard web engagement (all industries)
        "InsurancePolicy__dlm", "InsuranceClaim__dlm",
        "PurchaseOrder__dlm", "OrderLine__dlm", "LoyaltyTransaction__dlm",
        "SalesOrder__dlm", "FinancialAccount__dlm", "Transaction__dlm",
        "BankingProduct__dlm",             # banking product holdings
        "Prescription__dlm",
        "ServiceContract__dlm", "UsageRecord__dlm",
        "WholesaleOrder__dlm", "WholesaleOrderLine__dlm",
        "HtSubscription__dlm", "HtUsageRecord__dlm", "HtSupportTicket__dlm",
        "UtilityContract__dlm", "ConsumptionRecord__dlm",
        "FlightBooking__dlm",
        "MedicalVisit__dlm", "LabResult__dlm",
        "Membership__dlm", "ActivityRecord__dlm",
        "EcomOrder__dlm", "EcomOrderLine__dlm", "CartAbandonment__dlm",
        "HotelStay__dlm",
        "Subscription__dlm", "ContentView__dlm",
        "Vehicle__dlm", "ServiceRecord__dlm",
        "PropertyInquiry__dlm", "PropertyTransaction__dlm",
        "BettingAccount__dlm", "BettingTransaction__dlm",
    ]
    pairs = set()
    for dmo in (dmos or ALL_DMOS):
        st, data = api(core_url, token, "GET",
                       f"{BASE}/data-model-object-mappings"
                       f"?dmoDeveloperName={dmo}&dataspace=default")
        if st != 200:
            continue
        for m in data.get("objectSourceTargetMaps", []):
            src = m.get("sourceEntityDeveloperName", "")
            tgt = m.get("targetEntityDeveloperName", "")
            if src and tgt:
                pairs.add((src, tgt))
    return pairs


def post_mapping(core_url: str, token: str,
                 dlo_name: str, dmo_name: str,
                 field_pairs: list) -> tuple:
    """POST one DLO → DMO mapping.

    Correct format (proven 2026-06-24):
      - sourceEntityDeveloperName / targetEntityDeveloperName  (not *ApiName / *ObjectApiName)
      - fieldMapping  (SINGULAR — not fieldMappings)
      - sourceFieldDeveloperName carries __c suffix (the DLO materializes fields with __c)
      - dataspace=default is a QUERY PARAM on the URL, not in the body
    """
    body = {
        "sourceEntityDeveloperName": dlo_name,
        "targetEntityDeveloperName": dmo_name,
        "fieldMapping": [
            {
                "sourceFieldDeveloperName": f"{src}__c",
                "targetFieldDeveloperName": tgt,
            }
            for src, tgt in field_pairs
        ],
    }
    return api(core_url, token, "POST",
               f"{BASE}/data-model-object-mappings?dataspace=default", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    alias = cfg["orgAlias"]
    slug = cfg.get("clientSlug", "client")
    industry = cfg.get("industry", "insurance").lower()
    # Stream name prefix — must match what upload_and_stream.py used
    prefix = slug.replace("-", "_").title().replace("_", "")  # e.g. Migdal

    print(f"\n🗺️   Creating DMO mappings for {cfg.get('clientName', slug)} ({industry})")
    print(f"    Org: {alias}\n")

    core_url, core_token, _, _ = get_tokens(alias)
    print(f"  ✓  Authenticated — {core_url}")

    # Get existing DLOs (stream name → DLO API name)
    dlos = list_dlos(core_url, core_token)
    print(f"  ℹ️  Found {len(dlos)} data streams\n")

    # Assemble mappings to create.
    # B2B industries (food_b2b, hightech) use Account-level standard mappings.
    # config.json must have "b2b": true for the Account path to be used.
    b2b_account = cfg.get("b2b", False) and industry in ("food_b2b", "hightech")
    if b2b_account:
        print(f"  ℹ️  B2B Account mode — using Account/AccountEmailAddress standard mappings\n")
        std_maps = B2B_STANDARD_MAPPINGS
    else:
        std_maps = STANDARD_MAPPINGS
    all_mappings = list(std_maps) + INDUSTRY_CUSTOM_MAPPINGS.get(industry, [])

    # Collect DMOs we'll work with and check existing mappings
    target_dmos = list({dmo for _, dmo, _ in all_mappings})
    existing = existing_mappings(core_url, core_token, target_dmos)
    print(f"  ℹ️  Found {len(existing)} existing mappings for these DMOs\n")

    results = []
    for dlo_suffix, dmo_name, field_pairs in all_mappings:
        stream_name = f"{prefix}_{dlo_suffix}"

        # Resolve actual DLO API name from the stream list
        dlo_api_name = dlos.get(stream_name)
        if not dlo_api_name:
            for k, v in dlos.items():
                if k.lower() == stream_name.lower():
                    dlo_api_name = v
                    break

        if not dlo_api_name:
            print(f"  ⚠️  Stream not found: {stream_name} — skipping {dmo_name}")
            results.append({"stream": stream_name, "dmo": dmo_name, "status": "stream-not-found"})
            continue

        # Idempotency check: (dlo_dll_name, dmo_name) pair
        if (dlo_api_name, dmo_name) in existing:
            print(f"  ↩  {stream_name} → {dmo_name}  (already mapped)")
            results.append({"stream": stream_name, "dmo": dmo_name, "status": "existing"})
            continue

        print(f"  →  {stream_name} → {dmo_name} ...", end=" ", flush=True)

        if args.dry_run:
            print("[dry-run]")
            results.append({"stream": stream_name, "dmo": dmo_name, "status": "dry-run"})
            continue

        status, resp = post_mapping(core_url, core_token, dlo_api_name, dmo_name, field_pairs)

        if status in (200, 201):
            print("✓")
            results.append({"stream": stream_name, "dmo": dmo_name, "status": "created"})
        elif "DUPLICATE" in str(resp).upper():
            print("↩  (duplicate)")
            results.append({"stream": stream_name, "dmo": dmo_name, "status": "duplicate"})
        elif "UNABLE TO FIND PRIMARY KEY" in str(resp).upper() or \
             "NOT YET MATERIALIZED" in str(resp).upper() or \
             "PLEASE TRY AGAIN LATER" in str(resp).upper():
            print(f"⚠️  DLO not yet materialized — re-run after ingestion completes")
            results.append({"stream": stream_name, "dmo": dmo_name,
                             "status": "dlo-not-ready", "detail": str(resp)[:200]})
        else:
            print(f"✗  ({status}: {str(resp)[:120]})")
            results.append({"stream": stream_name, "dmo": dmo_name,
                             "status": f"error-{status}", "detail": str(resp)[:300]})

        time.sleep(0.3)

    ok = sum(1 for r in results if r["status"] in ("created", "existing", "duplicate"))
    print(f"\n✅  {ok}/{len(results)} mappings OK")
    if any(r["status"].startswith("error") for r in results):
        print("  ⚠️  Some mappings failed — check Data Cloud Setup → Data Model")

    # Persist
    Path(cfg.get("outputDir", f"data/{slug}")).mkdir(parents=True, exist_ok=True)
    (Path(cfg.get("outputDir", f"data/{slug}")) / "mapping_results.json").write_text(
        json.dumps(results, indent=2)
    )


if __name__ == "__main__":
    main()
