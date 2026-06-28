#!/usr/bin/env python3
"""
Generate industry-specific synthetic CSVs for Data Cloud seeding.

Usage:
    python3 gen_data.py --config config.json [--out data/]

Outputs (industry-dependent):
    contacts.csv             → Individual DMO
    contact_emails.csv       → ContactPointEmail DMO
    <industry>_table1.csv    → custom DMO
    <industry>_table2.csv    → custom DMO (optional)
    email_engagement.csv     → Engagement DMO

NOTE: Phone and address CSVs are NOT generated.
  Identity Resolution uses email + name only — phone/address streams are unnecessary
  and add no IR value while increasing ingestion time and complexity.

Design goals:
  - ≥7 000 individuals minimum (default 10 000)
  - All event/transaction dates within the last 2 years (P2Y Engagement lookback safe)
  - LTV, churn_score (0-100), nps_score (0-10), loyalty_tier on EVERY individual
    → these drive the Contact 360 profile widgets without needing Calculated Insights
  - Industry-appropriate products/policies/transactions — no retail SKUs for insurance
  - Meaningful cross-record relationships (policy references a real contact_id)
  - Email overlap between sources (so IR can unify)
"""
import argparse
import csv
import json
import random
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import sys

# ─── seed for reproducibility ────────────────────────────────────────────────
random.seed(42)

# ─── tiny Faker replacement (no external deps) ───────────────────────────────
FIRST_NAMES_M = ["Avi", "David", "Yossi", "Moshe", "Roni", "Ilan", "Eran", "Gal",
                  "Dani", "Nir", "Alon", "Shai", "Yoav", "Eyal", "Amit", "Guy",
                  "Omer", "Nadav", "Tomer", "Roi", "James", "Oliver", "Noah",
                  "Liam", "Lucas", "Ethan", "Mason", "Logan", "Elijah", "Aiden",
                  "Carlos", "Miguel", "Diego", "Ahmed", "Omar", "Hassan", "Yusuf",
                  "Pierre", "Jean", "Thomas", "Marco", "Luca", "Matteo", "Leon"]
FIRST_NAMES_F = ["Sara", "Noa", "Maya", "Tal", "Michal", "Yael", "Shira", "Lior",
                  "Dana", "Hila", "Tamar", "Gali", "Rina", "Orly", "Avital",
                  "Emma", "Olivia", "Charlotte", "Ava", "Sophia", "Isabella",
                  "Mia", "Camila", "Layla", "Fatima", "Amira", "Lena", "Anna",
                  "Maria", "Clara", "Giulia", "Sofia", "Lucie", "Marie", "Lea"]
LAST_NAMES = ["Cohen", "Levi", "Mizrahi", "Peretz", "Katz", "Friedman", "Rosenthal",
              "Shapiro", "Goldberg", "Stern", "Klein", "Fischer", "Schwartz",
              "Ben-David", "Bar", "Biton", "Dahan", "Eliyahu", "Gabay", "Hason",
              "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Martinez",
              "Anderson", "Taylor", "Thomas", "Nguyen", "Lee", "Wilson", "Moore",
              "Jackson", "Martin", "Thompson", "White", "Harris", "Clark", "Lewis",
              "Robinson", "Walker", "Young", "King", "Wright", "Scott", "Green"]
CITIES_IL = ["Tel Aviv", "Jerusalem", "Haifa", "Beer Sheva", "Petah Tikva",
             "Ashdod", "Netanya", "Rishon LeZion", "Bat Yam", "Holon",
             "Rehovot", "Herzliya", "Ramat Gan", "Kfar Saba", "Modi'in",
             "Ashkelon", "Eilat", "Tiberias", "Nazareth", "Lod"]
CITIES_INTL = ["London", "Paris", "Berlin", "Madrid", "Rome", "Amsterdam",
               "Brussels", "Vienna", "Zurich", "Stockholm", "Oslo", "Copenhagen",
               "New York", "Los Angeles", "Chicago", "Toronto", "Sydney", "Dubai"]
DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
           "walla.co.il", "013.net", "bezeqint.net", "netvision.net.il"]

# ─── market / geography configuration ────────────────────────────────────────
# Keys: "IL" | "ES" | "US" | "UK" | "FR" | "DE" | "GLOBAL"
# The wizard sets cfg["market"] based on what the SE tells us about the demo audience.

CITIES_ES = ["Madrid", "Barcelona", "Valencia", "Seville", "Bilbao",
             "Zaragoza", "Málaga", "Murcia", "Palma", "Las Palmas",
             "Santander", "Pamplona", "San Sebastián", "Vitoria", "Córdoba"]
CITIES_US = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
             "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
             "Austin", "Boston", "Seattle", "Denver", "Atlanta"]
CITIES_UK = ["London", "Manchester", "Birmingham", "Leeds", "Glasgow",
             "Liverpool", "Bristol", "Edinburgh", "Sheffield", "Cardiff",
             "Leicester", "Coventry", "Nottingham", "Southampton", "Oxford"]
CITIES_FR = ["Paris", "Lyon", "Marseille", "Toulouse", "Nice",
             "Nantes", "Strasbourg", "Montpellier", "Bordeaux", "Lille",
             "Rennes", "Reims", "Saint-Étienne", "Toulon", "Grenoble"]
CITIES_DE = ["Berlin", "Hamburg", "Munich", "Cologne", "Frankfurt",
             "Stuttgart", "Düsseldorf", "Leipzig", "Dortmund", "Bremen",
             "Dresden", "Hannover", "Nuremberg", "Duisburg", "Bochum"]
CITIES_GLOBAL = ["New York", "London", "Paris", "Berlin", "Madrid",
                 "Amsterdam", "Toronto", "Sydney", "Singapore", "Dubai",
                 "Tokyo", "São Paulo", "Mexico City", "Mumbai", "Seoul",
                 "Chicago", "Los Angeles", "Frankfurt", "Stockholm", "Zurich"]

LAST_NAMES_ES = ["García", "Martínez", "López", "Sánchez", "González",
                 "Rodríguez", "Fernández", "Pérez", "Gómez", "Díaz",
                 "Jiménez", "Ruiz", "Hernández", "Moreno", "Muñoz"]
LAST_NAMES_FR = ["Martin", "Bernard", "Dubois", "Moreau", "Laurent",
                 "Simon", "Michel", "Lefebvre", "Leroy", "Roux",
                 "Dupont", "Morin", "Girard", "Fontaine", "Rousseau"]
LAST_NAMES_DE = ["Müller", "Schmidt", "Schneider", "Fischer", "Weber",
                 "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann",
                 "Schäfer", "Koch", "Bauer", "Richter", "Klein"]

MARKET_CONFIG = {
    "IL":     {"country": "IL",  "currency": "ILS", "cities": CITIES_IL,     "last_names": LAST_NAMES},
    "ES":     {"country": "ES",  "currency": "EUR", "cities": CITIES_ES,     "last_names": LAST_NAMES + LAST_NAMES_ES},
    "US":     {"country": "US",  "currency": "USD", "cities": CITIES_US,     "last_names": LAST_NAMES},
    "UK":     {"country": "GB",  "currency": "GBP", "cities": CITIES_UK,     "last_names": LAST_NAMES},
    "FR":     {"country": "FR",  "currency": "EUR", "cities": CITIES_FR,     "last_names": LAST_NAMES + LAST_NAMES_FR},
    "DE":     {"country": "DE",  "currency": "EUR", "cities": CITIES_DE,     "last_names": LAST_NAMES + LAST_NAMES_DE},
    "GLOBAL": {"country": "INT", "currency": "USD", "cities": CITIES_GLOBAL, "last_names": LAST_NAMES},
}

# Default B2B account/store type suffixes per market (used when config omits accountTypes/storeTypes)
DEFAULT_STORE_TYPES = {
    "IL":     ["Minimarket", "Supermarket", "FreshMart", "Makolet", "Delek Store",
               "QuickMart", "ShopMaster", "GreenGrocer", "FoodPlus", "DailyMart"],
    "ES":     ["Supermercado", "Alimentación", "Frutería", "Carnicería",
               "Ultramarinos", "Colmado", "Tienda", "Mercado"],
    "US":     ["Market", "Grocery", "Foods", "Superstore", "Provisions", "Fresh Market"],
    "UK":     ["Market", "Grocery", "Foods", "Provisions", "Store", "Deli"],
    "FR":     ["Épicerie", "Supermarché", "Boucherie", "Fromagerie", "Marché", "Commerce"],
    "DE":     ["Markt", "Laden", "Supermarkt", "Lebensmittel", "Frischmarkt"],
    "GLOBAL": ["Market", "Grocery", "Superstore", "Foods", "Fresh Market"],
}
DEFAULT_ACCOUNT_TYPES = ["Technologies", "Systems", "Solutions", "Labs", "Digital",
                         "Software", "Innovations", "Analytics", "Platform", "AI",
                         "Data", "Cloud", "Networks", "Consulting", "Group"]


def _uuid():
    return str(uuid.uuid4())


def _norm(s: str) -> str:
    """Strip accents, lowercase, remove non-ASCII — safe email prefix."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


def _date(start_year=2015, end_year=2024) -> str:
    start = datetime(start_year, 1, 1)
    end = datetime(end_year, 12, 31)
    return (start + timedelta(days=random.randint(0, (end - start).days))).strftime("%Y-%m-%d")


def _datetime(start_year=2015, end_year=2024) -> str:
    """DateTime in ISO 8601 format — maps to Salesforce DateTime DMO fields.

    Fields like ssot__BirthDate__c, ssot__CreatedDate__c, ssot__ActiveFromDate__c
    are DateTime type in the standard DMOs. Generating YYYY-MM-DDTHH:MM:SS.000Z
    lets the schema inferrer type them as DateTime, enabling the mapping.
    """
    start = datetime(start_year, 1, 1)
    end = datetime(end_year, 12, 31)
    d = start + timedelta(days=random.randint(0, (end - start).days))
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _recent_date(days_back=365) -> str:
    return (datetime.today() - timedelta(days=random.randint(0, days_back))).strftime("%Y-%m-%d")


def _recent_datetime(days_back=365) -> str:
    """Recent DateTime for fields that need ISO 8601 DateTime format."""
    d = datetime.today() - timedelta(days=random.randint(0, days_back))
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _future_date(days_ahead=365*10) -> str:
    return (datetime.today() + timedelta(days=random.randint(30, days_ahead))).strftime("%Y-%m-%d")


# ─── contact generation (shared across industries) ───────────────────────────

def gen_contacts(n: int, market: str = "IL") -> list[dict]:
    """Generate N individual profiles with scoring attributes.

    market: one of IL | ES | US | UK | FR | DE | GLOBAL
    Drives city names, country code, and surname pool.
    """
    mkt = MARKET_CONFIG.get(market.upper(), MARKET_CONFIG["IL"])
    cities     = mkt["cities"]
    country    = mkt["country"]
    last_names = mkt["last_names"]

    contacts = []
    for i in range(n):
        gender = random.choice(["M", "F"])
        first = random.choice(FIRST_NAMES_M if gender == "M" else FIRST_NAMES_F)
        last  = random.choice(last_names)
        email_prefix = f"{_norm(first)}.{_norm(last)}{random.randint(1, 999)}"
        email = f"{email_prefix}@{random.choice(DOMAINS)}"
        city  = random.choice(cities)
        birth_year = random.randint(1955, 2003)
        since_year = random.randint(2010, 2023)
        tiers        = ["Bronze", "Silver", "Gold", "Platinum"]
        tier_weights = [0.35, 0.35, 0.20, 0.10]
        tier  = random.choices(tiers, weights=tier_weights)[0]
        ltv   = round(random.uniform(500, 150000), 2)
        churn = round(random.uniform(0, 100), 1)
        nps   = round(random.uniform(0, 10), 1)
        # Loyalty points — present for ALL industries (balance, earned/redeemed YTD).
        # These drive the loyalty widgets in Contact 360 without needing a CI.
        pts_earned_ytd   = random.randint(50, 5000)
        pts_redeemed_ytd = random.randint(0, pts_earned_ytd)
        pts_balance      = max(0, pts_earned_ytd - pts_redeemed_ytd + random.randint(0, 2000))
        # Income range — useful for banking, insurance, and general profiling.
        income_range = random.choices(
            ["<25k", "25k-50k", "50k-75k", "75k-100k", ">100k"],
            weights=[0.15, 0.25, 0.30, 0.20, 0.10],
        )[0]
        # ── Pre-computed enrichment flags (all industries) ──────────────────────
        # value_tier: derived from LTV for direct Segment Builder filtering
        if   ltv >= 50_000: value_tier = "VIP"
        elif ltv >= 20_000: value_tier = "High"
        elif ltv >= 5_000:  value_tier = "Medium"
        else:               value_tier = "Low"
        # rfm_segment: RFM-style label, very useful in demo segments
        if   churn <= 20 and ltv >= 20_000: rfm_segment = "Champion"
        elif churn <= 35 and ltv >= 5_000:  rfm_segment = "Loyal"
        elif churn >= 60:                   rfm_segment = "At-Risk"
        elif churn >= 45 and ltv < 5_000:   rfm_segment = "Dormant"
        else:                               rfm_segment = "New"
        digital_active      = random.choices([1, 0], weights=[0.60, 0.40])[0]
        preferred_channel   = random.choices(
            ["Email", "App", "Web", "InStore", "SMS"],
            weights=[0.30, 0.25, 0.20, 0.15, 0.10],
        )[0]
        acquisition_channel = random.choices(
            ["Organic", "Paid Search", "Referral", "Store", "App"],
            weights=[0.30, 0.25, 0.20, 0.15, 0.10],
        )[0]
        predicted_ltv = round(ltv * random.uniform(0.8, 1.8), 2)
        contacts.append({
            "id": _uuid(),
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": f"+{random.randint(1,99)}-{random.randint(100,999)}-{random.randint(1000000,9999999)}",
            # DateTime format for fields that map to standard DMO DateTime fields:
            #   birth_date   → ssot__BirthDate__c    (DateTime)
            #   created_date → ssot__CreatedDate__c  (DateTime)
            "birth_date": datetime(birth_year, random.randint(1, 12), random.randint(1, 28)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "gender": gender,
            "city": city,
            "country": country,
            "postal_code": f"{random.randint(10000, 99999)}",
            "street_address": f"{random.randint(1, 200)} Main St.",
            "customer_since": f"{since_year}-{random.randint(1,12):02d}-01",
            "loyalty_tier": tier,
            "loyalty_points_balance": pts_balance,
            "points_earned_ytd": pts_earned_ytd,
            "points_redeemed_ytd": pts_redeemed_ytd,
            "ltv": ltv,
            "churn_score": churn,
            "nps_score": nps,
            "income_range": income_range,
            "value_tier": value_tier,
            "digital_active": digital_active,
            "preferred_channel": preferred_channel,
            "acquisition_channel": acquisition_channel,
            # days_since_last_purchase: placeholder — overridden in main() from real event data
            "days_since_last_purchase": 0,
            "rfm_segment": rfm_segment,
            "predicted_ltv": predicted_ltv,
            # product_affinity: overridden per-industry in main()
            "product_affinity": None,
            "source": random.choice(["crm", "web", "mobile", "branch"]),
            "created_date": _datetime(since_year, 2024),
        })
    return contacts


def _split_contact_points(contacts: list[dict]):
    """Split contacts into the 4 standard contact-point tables."""
    emails, phones, addresses = [], [], []
    for c in contacts:
        emails.append({
            "id": _uuid(),
            "contact_id": c["id"],
            "email_address": c["email"],
            "is_primary": "true",
            # DateTime format → maps to ssot__ActiveFromDate__c (DateTime) on ContactPointEmail
            "active_from_date": datetime.strptime(c["customer_since"], "%Y-%m-%d").strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
        phones.append({
            "id": _uuid(),
            "contact_id": c["id"],
            "phone_number": c["phone"],
            "contact_point_type": "Mobile",
            "is_primary": "true",
        })
        addresses.append({
            "id": _uuid(),
            "contact_id": c["id"],
            "address_line1": c["street_address"],
            "city_name": c["city"],
            "country_name": c["country"],
            "postal_code": c["postal_code"],
            "is_primary": "true",
        })
    return emails, phones, addresses


# ─── email engagement (shared) ───────────────────────────────────────────────

def gen_email_engagement(contacts: list[dict], industry: str) -> list[dict]:
    """Generate 4 campaign waves × 2 events (send + open/click) per contact.

    All dates within the last 720 days (capped below 2-year window) to stay
    inside the Engagement DLO P2Y lookback — dates older than 2 years are
    invisible to segment filters on Engagement DLOs.
    """
    campaigns = CAMPAIGN_CATALOG.get(industry, CAMPAIGN_CATALOG["retail"])
    today = datetime.today()
    rows = []
    for contact in contacts:
        # Not every contact gets every campaign
        for camp in random.sample(campaigns, k=min(len(campaigns), random.randint(2, len(campaigns)))):
            # Random date within last 720 days (safely inside P2Y window)
            days_back = random.randint(0, 720)
            sent_date = today - timedelta(days=days_back)
            opened = random.random() < camp.get("open_rate", 0.3)
            clicked = opened and (random.random() < camp.get("click_rate", 0.15))
            # sent_date → DateTime: it's the eventDateTimeFieldName for Engagement DLOs
            # open_date / click_date → DateTime to be consistent
            row = {
                "event_id": _uuid(),
                "contact_id": contact["id"],
                "email": contact["email"],
                "campaign_id": camp["id"],
                "campaign_name": camp["name"],
                "subject": camp["subject"],
                "sent_date": sent_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "opened": "1" if opened else "0",
                "open_date": (sent_date + timedelta(days=random.randint(0, 3))).strftime("%Y-%m-%dT%H:%M:%S.000Z") if opened else "",
                "clicked": "1" if clicked else "0",
                "click_date": (sent_date + timedelta(days=random.randint(0, 5))).strftime("%Y-%m-%dT%H:%M:%S.000Z") if clicked else "",
                "unsubscribed": "1" if (not opened and random.random() < 0.01) else "0",
            }
            rows.append(row)
    return rows


CAMPAIGN_CATALOG = {
    "insurance": [
        {"id": "CAMP-INS-01", "name": "Annual Policy Review", "subject": "Your Policy is Up for Review — Personalised Recommendations Inside", "open_rate": 0.38, "click_rate": 0.18},
        {"id": "CAMP-INS-02", "name": "Health Coverage Upgrade", "subject": "Upgrade Your Health Coverage — Exclusive Member Rates", "open_rate": 0.32, "click_rate": 0.14},
        {"id": "CAMP-INS-03", "name": "Pension Savings Opportunity", "subject": "Maximise Your Pension — See How Much More You Could Save", "open_rate": 0.28, "click_rate": 0.12},
        {"id": "CAMP-INS-04", "name": "Digital Claims Launch", "subject": "New: File Your Claim Online in Under 5 Minutes", "open_rate": 0.45, "click_rate": 0.25},
    ],
    "food": [
        {"id": "CAMP-FOOD-01", "name": "Weekly Freshness", "subject": "Fresh Picks This Week — Shop Before They're Gone", "open_rate": 0.35, "click_rate": 0.20},
        {"id": "CAMP-FOOD-02", "name": "Loyalty Bonus", "subject": "You've Earned Double Points This Weekend", "open_rate": 0.50, "click_rate": 0.30},
        {"id": "CAMP-FOOD-03", "name": "New Product Launch", "subject": "Introducing Our New Range — Taste Something New", "open_rate": 0.30, "click_rate": 0.15},
        {"id": "CAMP-FOOD-04", "name": "Seasonal Offers", "subject": "Summer Favourites — Big Savings on Your Staples", "open_rate": 0.40, "click_rate": 0.22},
    ],
    "retail": [
        {"id": "CAMP-RET-01", "name": "New Season Arrivals", "subject": "New Season Is Here — Shop the Look", "open_rate": 0.35, "click_rate": 0.20},
        {"id": "CAMP-RET-02", "name": "VIP Member Sale", "subject": "Exclusive: 20% Off for Members This Weekend Only", "open_rate": 0.48, "click_rate": 0.28},
        {"id": "CAMP-RET-03", "name": "Abandoned Cart", "subject": "You Left Something Behind — Complete Your Order", "open_rate": 0.42, "click_rate": 0.25},
        {"id": "CAMP-RET-04", "name": "Back in Stock", "subject": "The Item You Loved Is Back", "open_rate": 0.55, "click_rate": 0.32},
    ],
    "banking": [
        {"id": "CAMP-BNK-01", "name": "New Mortgage Rates", "subject": "Rates Just Dropped — See Your Personalised Offer", "open_rate": 0.36, "click_rate": 0.16},
        {"id": "CAMP-BNK-02", "name": "Savings Account Upgrade", "subject": "You Could Be Earning More — Upgrade Your Savings", "open_rate": 0.30, "click_rate": 0.14},
        {"id": "CAMP-BNK-03", "name": "Card Benefits Reminder", "subject": "Benefits You Might Be Missing — Your Card Perks", "open_rate": 0.38, "click_rate": 0.18},
        {"id": "CAMP-BNK-04", "name": "Digital Banking", "subject": "Manage Everything from Your Phone — See What's New", "open_rate": 0.44, "click_rate": 0.22},
    ],
    "pharma": [
        {"id": "CAMP-PHA-01", "name": "Refill Reminder", "subject": "Time to Refill? Your Prescription Is Ready", "open_rate": 0.60, "click_rate": 0.35},
        {"id": "CAMP-PHA-02", "name": "Wellness Programme", "subject": "Join Our Wellness Programme — Exclusive Benefits for You", "open_rate": 0.32, "click_rate": 0.15},
        {"id": "CAMP-PHA-03", "name": "New Therapy Options", "subject": "New Treatment Options Available — Talk to Your Doctor", "open_rate": 0.28, "click_rate": 0.12},
        {"id": "CAMP-PHA-04", "name": "Seasonal Health", "subject": "Flu Season Is Coming — Are You Protected?", "open_rate": 0.40, "click_rate": 0.20},
    ],
    "telco": [
        {"id": "CAMP-TEL-01", "name": "5G Upgrade", "subject": "You're Eligible for 5G — Upgrade at No Extra Cost", "open_rate": 0.45, "click_rate": 0.25},
        {"id": "CAMP-TEL-02", "name": "Bundle Offer", "subject": "Save More: Add TV to Your Mobile Plan", "open_rate": 0.35, "click_rate": 0.18},
        {"id": "CAMP-TEL-03", "name": "Contract Renewal", "subject": "Your Contract Is Expiring — Lock In Our Best Rate", "open_rate": 0.50, "click_rate": 0.30},
        {"id": "CAMP-TEL-04", "name": "Data Usage Alert", "subject": "You've Used 80% of Your Data — Options Inside", "open_rate": 0.62, "click_rate": 0.40},
    ],
}
CAMPAIGN_CATALOG["travel"] = [
    {"id": "CAMP-TRV-01", "name": "Summer Destinations", "subject": "Top Destinations for Summer 2024 — Early Bird Deals", "open_rate": 0.38, "click_rate": 0.20},
    {"id": "CAMP-TRV-02", "name": "Loyalty Miles Expiry", "subject": "Your Miles Are About to Expire — Use Them Now", "open_rate": 0.55, "click_rate": 0.35},
    {"id": "CAMP-TRV-03", "name": "Flash Sale", "subject": "48-Hour Flash Sale — Up to 40% Off Selected Routes", "open_rate": 0.50, "click_rate": 0.30},
    {"id": "CAMP-TRV-04", "name": "Upgrade Offer", "subject": "Bid on a Business Class Upgrade for Your Next Flight", "open_rate": 0.42, "click_rate": 0.22},
]
CAMPAIGN_CATALOG["energy"] = [
    {"id": "CAMP-ENE-01", "name": "Green Tariff", "subject": "Switch to 100% Renewable — Same Price, Better Planet", "open_rate": 0.30, "click_rate": 0.14},
    {"id": "CAMP-ENE-02", "name": "Smart Meter", "subject": "Get Your Free Smart Meter — See Usage in Real Time", "open_rate": 0.35, "click_rate": 0.18},
    {"id": "CAMP-ENE-03", "name": "Winter Prep", "subject": "Beat the Winter Bills — See Your Personalised Tips", "open_rate": 0.40, "click_rate": 0.20},
    {"id": "CAMP-ENE-04", "name": "EV Charging Plan", "subject": "New EV? You Need Our Overnight Charging Plan", "open_rate": 0.28, "click_rate": 0.16},
]
CAMPAIGN_CATALOG["food_b2b"] = [
    {"id": "CAMP-B2B-01", "name": "New Product Range", "subject": "New SKUs Available — Expand Your Shelf This Season", "open_rate": 0.40, "click_rate": 0.22},
    {"id": "CAMP-B2B-02", "name": "Volume Discount", "subject": "Order More, Save More — Exclusive Volume Pricing", "open_rate": 0.45, "click_rate": 0.28},
    {"id": "CAMP-B2B-03", "name": "Promo Bundle", "subject": "This Month's Promotional Bundle — Limited Quantities", "open_rate": 0.38, "click_rate": 0.20},
    {"id": "CAMP-B2B-04", "name": "Account Review", "subject": "Your Q3 Account Review Is Ready — See Your Insights", "open_rate": 0.50, "click_rate": 0.30},
]
CAMPAIGN_CATALOG["hightech"] = [
    {"id": "CAMP-HT-01", "name": "Feature Release", "subject": "New Features Released — See What's Changed for You", "open_rate": 0.42, "click_rate": 0.25},
    {"id": "CAMP-HT-02", "name": "Renewal Reminder", "subject": "Your Subscription Renews Soon — Review Your Plan", "open_rate": 0.55, "click_rate": 0.35},
    {"id": "CAMP-HT-03", "name": "Upgrade Offer", "subject": "Get More Seats at a Special Rate — Limited Time", "open_rate": 0.38, "click_rate": 0.20},
    {"id": "CAMP-HT-04", "name": "Webinar Invite", "subject": "Live Webinar: Get the Most Out of Your Subscription", "open_rate": 0.35, "click_rate": 0.18},
]
CAMPAIGN_CATALOG["utilities"] = [
    {"id": "CAMP-UTL-01", "name": "Green Tariff", "subject": "Switch to 100% Renewable — Same Price, Better Planet", "open_rate": 0.30, "click_rate": 0.14},
    {"id": "CAMP-UTL-02", "name": "Smart Meter", "subject": "Get Your Free Smart Meter — See Usage in Real Time", "open_rate": 0.35, "click_rate": 0.18},
    {"id": "CAMP-UTL-03", "name": "Winter Prep", "subject": "Beat the Winter Bills — See Your Personalised Tips", "open_rate": 0.40, "click_rate": 0.20},
    {"id": "CAMP-UTL-04", "name": "EV Charging Plan", "subject": "New EV? You Need Our Overnight Charging Plan", "open_rate": 0.28, "click_rate": 0.16},
]
CAMPAIGN_CATALOG["airlines"] = [
    {"id": "CAMP-AIR-01", "name": "Summer Destinations", "subject": "Top Destinations for Summer — Early Bird Deals", "open_rate": 0.38, "click_rate": 0.20},
    {"id": "CAMP-AIR-02", "name": "Miles Expiry", "subject": "Your Miles Are About to Expire — Use Them Now", "open_rate": 0.55, "click_rate": 0.35},
    {"id": "CAMP-AIR-03", "name": "Flash Sale", "subject": "48-Hour Flash Sale — Up to 40% Off Selected Routes", "open_rate": 0.50, "click_rate": 0.30},
    {"id": "CAMP-AIR-04", "name": "Upgrade Offer", "subject": "Bid on a Business Class Upgrade for Your Next Flight", "open_rate": 0.42, "click_rate": 0.22},
]


# ─── WEB ENGAGEMENT ──────────────────────────────────────────────────────────

# Page catalog per industry.  Each entry: {url, category, weight}
WEB_PAGE_CATALOG = {
    "insurance": [
        {"url": "/products/life-insurance",   "category": "Products"},
        {"url": "/products/health",            "category": "Products"},
        {"url": "/products/vehicle",           "category": "Products"},
        {"url": "/claims",                     "category": "Self-Service"},
        {"url": "/my-policy",                  "category": "Self-Service"},
        {"url": "/get-quote",                  "category": "Acquisition"},
        {"url": "/blog/insurance-tips",        "category": "Content"},
        {"url": "/calculators/pension",        "category": "Tools"},
    ],
    "food": [
        {"url": "/products",                   "category": "Catalogue"},
        {"url": "/promotions/weekly-deals",    "category": "Promotions"},
        {"url": "/loyalty-rewards",            "category": "Loyalty"},
        {"url": "/find-store",                 "category": "Store-Locator"},
        {"url": "/categories/fresh",           "category": "Catalogue"},
        {"url": "/categories/dairy",           "category": "Catalogue"},
        {"url": "/my-account/orders",          "category": "Self-Service"},
    ],
    "retail": [
        {"url": "/new-arrivals",               "category": "Catalogue"},
        {"url": "/sale",                       "category": "Promotions"},
        {"url": "/bags",                       "category": "Category-Page"},
        {"url": "/shoes",                      "category": "Category-Page"},
        {"url": "/apparel",                    "category": "Category-Page"},
        {"url": "/wishlist",                   "category": "Self-Service"},
        {"url": "/cart",                       "category": "Checkout"},
        {"url": "/lookbook",                   "category": "Content"},
    ],
    "banking": [
        {"url": "/accounts/checking",          "category": "Products"},
        {"url": "/accounts/savings",           "category": "Products"},
        {"url": "/loans/personal",             "category": "Products"},
        {"url": "/loans/mortgage",             "category": "Products"},
        {"url": "/credit-cards",               "category": "Products"},
        {"url": "/transfers",                  "category": "Self-Service"},
        {"url": "/investments",                "category": "Products"},
        {"url": "/calculators/mortgage",       "category": "Tools"},
    ],
    "pharma": [
        {"url": "/medications",                "category": "Catalogue"},
        {"url": "/prescriptions/refill",       "category": "Self-Service"},
        {"url": "/health-guide",               "category": "Content"},
        {"url": "/locate-pharmacy",            "category": "Store-Locator"},
        {"url": "/my-account/prescriptions",   "category": "Self-Service"},
        {"url": "/news/treatments",            "category": "Content"},
    ],
    "telco": [
        {"url": "/plans/mobile",               "category": "Products"},
        {"url": "/plans/broadband",            "category": "Products"},
        {"url": "/plans/bundles",              "category": "Products"},
        {"url": "/upgrade",                    "category": "Acquisition"},
        {"url": "/my-account/usage",           "category": "Self-Service"},
        {"url": "/devices",                    "category": "Catalogue"},
        {"url": "/support",                    "category": "Self-Service"},
    ],
    "food_b2b": [
        {"url": "/catalogue",                  "category": "Catalogue"},
        {"url": "/promotions",                 "category": "Promotions"},
        {"url": "/orders",                     "category": "Self-Service"},
        {"url": "/account/rep",                "category": "Self-Service"},
        {"url": "/new-products",               "category": "Catalogue"},
        {"url": "/seasonal-offers",            "category": "Promotions"},
    ],
    "hightech": [
        {"url": "/product",                    "category": "Products"},
        {"url": "/pricing",                    "category": "Acquisition"},
        {"url": "/features",                   "category": "Products"},
        {"url": "/docs",                       "category": "Support"},
        {"url": "/blog",                       "category": "Content"},
        {"url": "/integrations",               "category": "Products"},
        {"url": "/signup",                     "category": "Acquisition"},
        {"url": "/demo-request",               "category": "Acquisition"},
    ],
}
# Re-use similar catalogs for less-common industries
WEB_PAGE_CATALOG["travel"]  = WEB_PAGE_CATALOG["retail"]
WEB_PAGE_CATALOG["energy"]  = WEB_PAGE_CATALOG["banking"]
WEB_PAGE_CATALOG["utilities"] = [
    {"url": "/my-account/usage",           "category": "Self-Service"},
    {"url": "/products/electricity",       "category": "Products"},
    {"url": "/products/gas",               "category": "Products"},
    {"url": "/products/water",             "category": "Products"},
    {"url": "/green-energy",               "category": "Products"},
    {"url": "/smart-meter",                "category": "Self-Service"},
    {"url": "/bill-payment",               "category": "Self-Service"},
    {"url": "/savings-calculator",         "category": "Tools"},
]
WEB_PAGE_CATALOG["airlines"] = [
    {"url": "/flights/search",             "category": "Booking"},
    {"url": "/flights/manage",             "category": "Self-Service"},
    {"url": "/frequent-flyer",             "category": "Loyalty"},
    {"url": "/destinations",               "category": "Content"},
    {"url": "/upgrade",                    "category": "Acquisition"},
    {"url": "/check-in",                   "category": "Self-Service"},
    {"url": "/baggage",                    "category": "Self-Service"},
    {"url": "/special-offers",             "category": "Promotions"},
]

WEB_EVENT_TYPES = ["page_view", "page_view", "page_view", "click_cta", "search", "video_watch", "download"]
WEB_DEVICES     = ["Mobile", "Desktop", "Tablet"]
WEB_DEVICE_W    = [0.55, 0.35, 0.10]


def gen_web_engagement(contacts: list[dict], industry: str) -> list[dict]:
    """Generate website engagement events for all contacts.

    ENGAGEMENT stream — every row has event_datetime (DateTime, ISO 8601).
    Date range: last 720 days (capped below P2Y lookback window).

    Each contact gets 3–25 events across random sessions in the last 2 years.
    """
    pages  = WEB_PAGE_CATALOG.get(industry, WEB_PAGE_CATALOG["retail"])
    today  = datetime.today()
    rows   = []
    for c in contacts:
        n_sessions = random.randint(3, 25)
        for _ in range(n_sessions):
            days_back  = random.randint(0, 720)
            session_dt = today - timedelta(days=days_back)
            session_id = _uuid()
            n_events   = random.randint(1, 6)
            for e in range(n_events):
                page      = random.choice(pages)
                event_dt  = session_dt + timedelta(minutes=e * random.randint(1, 12))
                rows.append({
                    "event_id":         _uuid(),
                    "contact_id":       c["id"],
                    "session_id":       session_id,
                    "event_datetime":   event_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "page_url":         page["url"],
                    "page_category":    page["category"],
                    "event_type":       random.choice(WEB_EVENT_TYPES),
                    "device_type":      random.choices(WEB_DEVICES, weights=WEB_DEVICE_W)[0],
                    "duration_seconds": random.randint(5, 300),
                })
    return rows


# ─── BANKING PRODUCTS ────────────────────────────────────────────────────────

BANKING_PRODUCTS_CATALOG = [
    # (product_type, product_name, amount_min, amount_max, rate_min, rate_max)
    ("Credit Card",   "Classic Visa",        5_000,   30_000,  15.9, 24.9),
    ("Credit Card",   "Platinum Rewards",   15_000,   80_000,  12.9, 19.9),
    ("Personal Loan", "Personal Loan",      10_000,  200_000,   4.5, 12.0),
    ("Mortgage",      "Home Mortgage",     300_000, 3_000_000,  3.5,  5.5),
    ("Auto Loan",     "Auto Financing",     30_000,  250_000,   3.9,  8.9),
    ("Business Loan", "SME Business Loan",  50_000, 1_000_000,  5.5, 10.0),
]


def gen_banking_products(contacts: list[dict]) -> list[dict]:
    """Generate product holdings per banking customer (credit cards, loans, mortgage).

    OTHER stream — mutable records (status, balance change over time).
    Not subject to P2Y lookback.  1–3 products per customer.
    """
    rows = []
    for c in contacts:
        n_products   = random.choices([1, 2, 3], weights=[0.50, 0.35, 0.15])[0]
        seen_types   = set()
        for _ in range(n_products):
            candidates = [p for p in BANKING_PRODUCTS_CATALOG if p[0] not in seen_types]
            if not candidates:
                break
            prod = random.choice(candidates)
            seen_types.add(prod[0])
            amount = round(random.uniform(prod[2], prod[3]), 2)
            rows.append({
                "product_id":    _uuid(),
                "contact_id":    c["id"],
                "product_type":  prod[0],
                "product_name":  prod[1],
                "amount":        amount,
                "interest_rate": round(random.uniform(prod[4], prod[5]), 1),
                "status":        random.choices(
                    ["Active", "Active", "Active", "Closed", "Pending"],
                    weights=[0.75, 0.75, 0.75, 0.10, 0.05],
                )[0],
                "opened_date":   _date(2015, 2024),
            })
    return rows


# ─── INSURANCE ───────────────────────────────────────────────────────────────

INSURANCE_PRODUCTS = [
    # (name, category, monthly_min, monthly_max, coverage_min, coverage_max)
    ("Life Protection Plus",      "Life",       80,  400, 250000, 2000000),
    ("Life Term Basic",           "Life",       40,  180, 100000,  800000),
    ("Family Health Shield",      "Health",    120,  600,  50000,  500000),
    ("Health Dental & Vision",    "Health",     50,  200,  10000,   80000),
    ("Home & Contents Cover",     "Property",   60,  250,  80000,  600000),
    ("Building Insurance",        "Property",  100,  450, 200000, 1500000),
    ("Comprehensive Motor",       "Vehicle",    90,  500,  20000,  150000),
    ("Third Party Motor",         "Vehicle",    40,  180,  10000,   50000),
    ("Pension Savings Plan",      "Pension",   200,  900,       0,        0),
    ("Executive Pension",         "Pension",   500, 2500,       0,        0),
    ("Disability Income",         "Disability", 80,  350,  20000,  120000),
    ("Critical Illness",          "Health",    100,  400,  50000,  500000),
]

CLAIM_TYPES = {
    "Life":       ["Death benefit payout", "Terminal illness benefit"],
    "Health":     ["Hospitalisation", "Specialist consultation", "Surgery", "Physiotherapy", "Dental treatment"],
    "Property":   ["Fire damage", "Flood damage", "Theft/Burglary", "Accidental damage", "Subsidence"],
    "Vehicle":    ["Road traffic accident", "Theft", "Windscreen damage", "Third party damage", "Weather damage"],
    "Disability": ["Accident disability", "Illness disability", "Rehabilitation costs"],
    "Pension":    [],  # no claims on pension
}


def gen_insurance_policies(contacts: list[dict]) -> list[dict]:
    rows = []
    for c in contacts:
        n_policies = random.choices([1, 2, 3, 4, 5], weights=[0.1, 0.25, 0.35, 0.20, 0.10])[0]
        for _ in range(n_policies):
            prod = random.choice(INSURANCE_PRODUCTS)
            # Mix of portfolio ages (realistic for insurance):
            #   ~30% recent sign-ups (last 2 years) — ensures segment lookback filters work
            #   ~50% mid-term (2-6 years ago) — core portfolio
            #   ~20% long-standing (7-15 years ago) — loyal customers
            now = datetime.today()
            p = random.random()
            if p < 0.30:
                # Recent: within last 720 days (P2Y-safe)
                days_back = random.randint(0, 720)
                start_dt = now - timedelta(days=days_back)
            elif p < 0.80:
                start_dt = datetime.strptime(_date(now.year - 6, now.year - 2), "%Y-%m-%d")
            else:
                start_dt = datetime.strptime(_date(2010, now.year - 7), "%Y-%m-%d")
            duration_years = random.randint(1, 20)
            end_dt = start_dt + timedelta(days=duration_years * 365)
            if end_dt > now:
                status = random.choices(["Active", "Active", "Active", "Pending", "Lapsed"], weights=[0.7, 0.7, 0.7, 0.1, 0.1])[0]
            else:
                status = random.choices(["Lapsed", "Cancelled"], weights=[0.7, 0.3])[0]

            monthly = round(random.uniform(prod[2], prod[3]), 2)
            coverage = round(random.uniform(prod[4], prod[5]), 2) if prod[5] > 0 else 0
            rows.append({
                "policy_id": _uuid(),
                "contact_id": c["id"],
                "policy_number": f"POL-{random.randint(10000000, 99999999)}",
                "product_name": prod[0],
                "product_category": prod[1],
                "premium_monthly": monthly,
                "premium_annual": round(monthly * 12, 2),
                "coverage_amount": coverage,
                "deductible": round(random.choice([0, 500, 1000, 2000, 5000]), 2),
                "start_date": start_dt.strftime("%Y-%m-%d"),
                "end_date": end_dt.strftime("%Y-%m-%d"),
                "status": status,
                "payment_frequency": random.choice(["Monthly", "Quarterly", "Annual"]),
            })
    return rows


def gen_insurance_claims(contacts: list[dict], policies: list[dict]) -> list[dict]:
    # Build policy index by contact
    pol_by_contact: dict[str, list] = {}
    for p in policies:
        pol_by_contact.setdefault(p["contact_id"], []).append(p)

    rows = []
    for c in contacts:
        if random.random() > 0.8:  # ~80% have at least one claim over their history
            continue
        contact_pols = pol_by_contact.get(c["id"], [])
        if not contact_pols:
            continue
        n_claims = random.choices([1, 2, 3], weights=[0.60, 0.28, 0.12])[0]
        for _ in range(n_claims):
            pol = random.choice(contact_pols)
            cat = pol["product_category"]
            types = CLAIM_TYPES.get(cat, ["General claim"])
            if not types:
                continue
            # Within 2-year window — consistent with the P2Y Engagement DLO rule
            # (claims are Other DLO so lookback doesn't apply, but recent dates make
            #  demo CIs more interesting and count-based filters more predictable)
            claim_date = _recent_date(days_back=720)
            claim_dt = datetime.strptime(claim_date, "%Y-%m-%d")
            status = random.choices(["Approved", "Paid", "Open", "Rejected"],
                                     weights=[0.30, 0.45, 0.15, 0.10])[0]
            resolution_dt = (claim_dt + timedelta(days=random.randint(3, 60))) if status in ("Approved", "Paid", "Rejected") else None
            rows.append({
                "claim_id": _uuid(),
                "policy_id": pol["policy_id"],
                "contact_id": c["id"],
                "claim_date": claim_date,
                "claim_type": random.choice(types),
                "claim_amount": round(random.uniform(200, min(pol.get("coverage_amount", 50000) or 50000, 80000)), 2),
                "status": status,
                "resolution_date": resolution_dt.strftime("%Y-%m-%d") if resolution_dt else "",
            })
    return rows


# ─── FOOD ────────────────────────────────────────────────────────────────────

FOOD_PRODUCTS = [
    # (sku, name, category, unit_price_min, unit_price_max)
    ("DAIRY-001", "Full-Fat Milk 3L",          "Dairy",    3.50,   6.00),
    ("DAIRY-002", "Cottage Cheese 250g",        "Dairy",    4.00,   7.00),
    ("DAIRY-003", "Greek Yogurt 500g",          "Dairy",    5.50,   9.00),
    ("DAIRY-004", "Yellow Cheese 200g",         "Dairy",    8.00,  14.00),
    ("DAIRY-005", "Butter 250g",                "Dairy",    7.00,  12.00),
    ("MEAT-001",  "Chicken Breast 1kg",         "Meat",    22.00,  38.00),
    ("MEAT-002",  "Ground Beef 500g",           "Meat",    18.00,  28.00),
    ("MEAT-003",  "Turkey Cold Cut 200g",       "Meat",    12.00,  20.00),
    ("BKY-001",   "Whole Wheat Bread",          "Bakery",   4.50,   8.00),
    ("BKY-002",   "Pita Bread 6-pack",          "Bakery",   5.00,   8.50),
    ("PRO-001",   "Tomatoes 1kg",               "Produce",  4.00,   9.00),
    ("PRO-002",   "Cucumber 500g",              "Produce",  3.00,   6.00),
    ("PRO-003",   "Bell Peppers 500g",          "Produce",  5.00,  10.00),
    ("BEV-001",   "Mineral Water 6-pack 1.5L",  "Beverages", 8.00, 14.00),
    ("BEV-002",   "Orange Juice 1L",            "Beverages", 6.00, 11.00),
    ("SNACK-001", "Potato Chips 150g",          "Snacks",   5.00,   9.00),
    ("SNACK-002", "Chocolate 100g",             "Snacks",   4.00,   8.00),
    ("FROZEN-001","Frozen Vegetables Mix 500g", "Frozen",   8.00,  14.00),
]

STORES = [
    ("STORE-001", "Rami Levy Tel Aviv"),
    ("STORE-002", "Shufersal Jerusalem"),
    ("STORE-003", "Victory Haifa"),
    ("STORE-004", "Yochananof Beer Sheva"),
    ("STORE-005", "Mega Netanya"),
    ("STORE-006", "Freshmarket Herzliya"),
    ("STORE-007", "AM:PM Ramat Gan"),
    ("ONLINE",    "Online"),
]


def gen_food_orders(contacts: list[dict], products_catalog=None) -> tuple[list, list]:
    """Generate purchase orders and order lines for food retail.

    products_catalog: optional list of (sku, name, category, price_min, price_max).
    If None, falls back to the default FOOD_PRODUCTS catalog.
    Override via config.json: "catalog_overrides": {"products": [["SKU-001","Whole Milk 1L","Dairy",1.2,2.5], ...]}
    """
    products = products_catalog if products_catalog else FOOD_PRODUCTS
    orders, lines = [], []
    for c in contacts:
        n_orders = random.choices([2, 3, 4, 5, 7, 10], weights=[0.15, 0.25, 0.25, 0.15, 0.12, 0.08])[0]
        for _ in range(n_orders):
            store = random.choice(STORES)
            order_datetime = _recent_datetime(548)  # 18 months, DateTime for Engagement DLO
            order_id = _uuid()
            n_lines = random.randint(3, 12)
            order_total = 0.0
            for ln in range(n_lines):
                prod = random.choice(products)
                qty = random.randint(1, 4)
                price = round(random.uniform(prod[3], prod[4]), 2)
                total = round(qty * price, 2)
                order_total += total
                lines.append({
                    "line_id": _uuid(),
                    "order_id": order_id,
                    "contact_id": c["id"],
                    "product_sku": prod[0],
                    "product_name": prod[1],
                    "category": prod[2],
                    "quantity": qty,
                    "unit_price": price,
                    "line_total": total,
                })
            orders.append({
                "order_id": order_id,
                "contact_id": c["id"],
                "order_datetime": order_datetime,
                "store_id": store[0],
                "store_name": store[1],
                "channel": "Online" if store[0] == "ONLINE" else random.choice(["In-Store", "In-Store", "In-Store", "Click & Collect"]),
                "total_amount": round(order_total, 2),
                "currency": "ILS",
                "loyalty_points_earned": int(order_total),
            })
    return orders, lines


def gen_loyalty_transactions(contacts: list[dict], orders: list[dict]) -> list[dict]:
    """Generate earn/redeem loyalty events.

    ENGAGEMENT stream — event_datetime is required (DateTime, not Date).
    Sorting uses event_datetime for running balance computation.
    """
    rows = []
    for order in orders:
        # Derive a plausible event time from the order datetime (already DateTime format)
        # Use the date portion and randomise the time to avoid exact duplicates
        hr = random.randint(8, 22)
        mn = random.randint(0, 59)
        order_dt = f"{order['order_datetime'][:10]}T{hr:02d}:{mn:02d}:00.000Z"
        rows.append({
            "tx_id": _uuid(),
            "contact_id": order["contact_id"],
            "event_datetime": order_dt,
            "type": "earn",
            "points": order["loyalty_points_earned"],
            "reference": order["order_id"],
            "balance": 0,  # filled below
        })
    # Add some redeems
    redeem_contacts = random.sample(contacts, k=int(len(contacts) * 0.3))
    for c in redeem_contacts:
        rows.append({
            "tx_id": _uuid(),
            "contact_id": c["id"],
            "event_datetime": _recent_datetime(180),
            "type": "redeem",
            "points": -random.randint(50, 500),
            "reference": f"REDEEM-{random.randint(10000, 99999)}",
            "balance": 0,
        })
    # Compute running balance per contact (sorted by event_datetime)
    by_contact: dict[str, list] = {}
    for r in rows:
        by_contact.setdefault(r["contact_id"], []).append(r)
    for c_rows in by_contact.values():
        c_rows.sort(key=lambda x: x["event_datetime"])
        bal = 0
        for r in c_rows:
            bal += r["points"]
            r["balance"] = max(0, bal)
    return rows


def _compute_loyalty_balance(rows: list[dict]) -> list[dict]:
    """Sort loyalty rows by event_datetime per contact and compute running balance."""
    by_contact: dict[str, list] = {}
    for r in rows:
        by_contact.setdefault(r["contact_id"], []).append(r)
    for c_rows in by_contact.values():
        c_rows.sort(key=lambda x: x["event_datetime"])
        bal = 0
        for r in c_rows:
            bal += r["points"]
            r["balance"] = max(0, bal)
    return rows


def gen_banking_loyalty(contacts: list[dict], transactions: list[dict]) -> list[dict]:
    """Generate loyalty earn events for banking — 1 pt per $10 of |transaction amount|.

    ENGAGEMENT stream: event_datetime required (DateTime, ISO 8601).
    Only expense transactions generate points (positive amounts = income = excluded).
    ~30% of contacts also get a redeem event.
    """
    rows = []
    for tx in transactions:
        pts = max(1, int(abs(tx["amount"]) / 10))
        rows.append({
            "tx_id":           _uuid(),
            "contact_id":      tx["contact_id"],
            "event_datetime":  tx["tx_datetime"],
            "type":            "earn",
            "points":          pts,
            "reference":       tx["tx_id"],
            "balance":         0,
        })
    redeem_contacts = random.sample(contacts, k=int(len(contacts) * 0.3))
    for c in redeem_contacts:
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     c["id"],
            "event_datetime": _recent_datetime(180),
            "type":           "redeem",
            "points":         -random.randint(50, 500),
            "reference":      f"REDEEM-{random.randint(10000, 99999)}",
            "balance":        0,
        })
    return _compute_loyalty_balance(rows)


def gen_retail_loyalty(contacts: list[dict], orders: list[dict]) -> list[dict]:
    """Generate loyalty earn events for retail — 1 pt per currency unit of order total.

    ENGAGEMENT stream: event_datetime required (DateTime, ISO 8601).
    ~30% of contacts also get a redeem event.
    """
    rows = []
    for order in orders:
        pts = max(1, int(order["total_amount"]))
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     order["contact_id"],
            "event_datetime": order["order_datetime"],
            "type":           "earn",
            "points":         pts,
            "reference":      order["order_id"],
            "balance":        0,
        })
    redeem_contacts = random.sample(contacts, k=int(len(contacts) * 0.3))
    for c in redeem_contacts:
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     c["id"],
            "event_datetime": _recent_datetime(180),
            "type":           "redeem",
            "points":         -random.randint(50, 500),
            "reference":      f"REDEEM-{random.randint(10000, 99999)}",
            "balance":        0,
        })
    return _compute_loyalty_balance(rows)


def gen_food_b2b_loyalty(contacts: list[dict], orders: list[dict]) -> list[dict]:
    """Generate loyalty earn events for food B2B — 0.2 pts per currency unit of order total.

    B2B accounts earn at a lower rate than B2C consumers.
    ENGAGEMENT stream: event_datetime required (DateTime, ISO 8601).
    ~30% of contacts also get a redeem event.
    """
    rows = []
    for order in orders:
        pts = max(1, int(order["total_amount"] * 0.2))
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     order["contact_id"],
            "event_datetime": order["order_datetime"],
            "type":           "earn",
            "points":         pts,
            "reference":      order["order_id"],
            "balance":        0,
        })
    redeem_contacts = random.sample(contacts, k=int(len(contacts) * 0.3))
    for c in redeem_contacts:
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     c["id"],
            "event_datetime": _recent_datetime(180),
            "type":           "redeem",
            "points":         -random.randint(100, 1000),
            "reference":      f"REDEEM-{random.randint(10000, 99999)}",
            "balance":        0,
        })
    return _compute_loyalty_balance(rows)


# ─── FOOD B2B ────────────────────────────────────────────────────────────────

_B2B_REPS = [f"Rep_{i:02d}" for i in range(1, 21)]  # Rep_01 … Rep_20


def gen_food_b2b(contacts: list[dict], products_catalog=None) -> tuple[list, list]:
    """Generate wholesale orders and order lines for B2B food distribution.

    products_catalog: optional list of (sku, name, category, price_min, price_max).
    Override via config.json: "catalog_overrides": {"products": [...]}
    """
    products = products_catalog if products_catalog else FOOD_PRODUCTS
    orders, lines = [], []
    for c in contacts:
        n_orders = random.choices([4, 6, 8, 10], weights=[0.20, 0.35, 0.30, 0.15])[0]
        for _ in range(n_orders):
            order_id = _uuid()
            order_datetime = _recent_datetime(720)  # DateTime for Engagement DLO (P2Y-safe)
            n_lines = random.randint(3, 8)
            order_lines = []
            for _ in range(n_lines):
                prod = random.choice(products)
                qty = random.randint(10, 100)
                # B2B price = roughly half retail
                retail_price = round(random.uniform(prod[3], prod[4]), 2)
                unit_price = round(retail_price / 2, 2)
                line_total = round(qty * unit_price, 2)
                is_promo = 1 if random.random() < 0.20 else 0
                order_lines.append({
                    "line_id": _uuid(),
                    "order_id": order_id,
                    "contact_id": c["id"],
                    "product_sku": prod[0],
                    "product_name": prod[1],
                    "category": prod[2],
                    "quantity": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                    "is_promotional": is_promo,
                })
            total_amount = round(sum(l["line_total"] for l in order_lines), 2)
            orders.append({
                "order_id": order_id,
                "contact_id": c["id"],
                "order_datetime": order_datetime,
                "total_amount": total_amount,
                "item_count": len(order_lines),
                "status": random.choices(
                    ["Delivered", "Shipped", "Processing", "Cancelled"],
                    weights=[0.70, 0.15, 0.10, 0.05],
                )[0],
                "payment_terms": random.choice(["NET30", "NET60", "NET45", "COD"]),
                "sales_rep": random.choice(_B2B_REPS),
            })
            lines.extend(order_lines)
    return orders, lines


# ─── RETAIL ──────────────────────────────────────────────────────────────────

RETAIL_PRODUCTS = [
    ("SKU-BAG-001",  "City Tote",           "Bags",        200, 900),
    ("SKU-BAG-002",  "Mini Crossbody",      "Bags",        150, 700),
    ("SKU-SHO-001",  "Leather Sneaker",     "Shoes",       120, 450),
    ("SKU-SHO-002",  "Chelsea Boot",        "Shoes",       180, 600),
    ("SKU-APP-001",  "Wool Coat",           "Apparel",     300, 1200),
    ("SKU-APP-002",  "Silk Shirt",          "Apparel",     80,  400),
    ("SKU-ACC-001",  "Silk Scarf",          "Accessories", 40,  300),
    ("SKU-ACC-002",  "Leather Gloves",      "Accessories", 60,  250),
    ("SKU-BAG-003",  "Weekender Duffle",    "Bags",        250, 900),
    ("SKU-SHO-003",  "Slide Sandal",        "Shoes",       80,  350),
    ("SKU-APP-003",  "Cashmere Sweater",    "Apparel",     200, 800),
    ("SKU-ACC-003",  "Sunglasses",          "Accessories", 100, 600),
]


def gen_retail_orders(contacts: list[dict], products_catalog=None) -> tuple[list, list]:
    """Generate sales orders and order lines for retail.

    products_catalog: optional list of (sku, name, category, price_min, price_max).
    If None, falls back to the default RETAIL_PRODUCTS catalog.
    Override via config.json: "catalog_overrides": {"products": [...]}
    """
    products = products_catalog if products_catalog else RETAIL_PRODUCTS
    orders, lines = [], []
    for c in contacts:
        n_orders = random.choices([1, 2, 3, 4], weights=[0.30, 0.35, 0.25, 0.10])[0]
        for _ in range(n_orders):
            order_datetime = _recent_datetime(548)  # DateTime for Engagement DLO
            order_id = _uuid()
            n_lines = random.randint(1, 4)
            total = 0.0
            for ln in range(n_lines):
                prod = random.choice(products)
                qty = random.randint(1, 2)
                price = round(random.uniform(prod[3], prod[4]), 2)
                subtotal = round(qty * price, 2)
                total += subtotal
                lines.append({
                    "line_id": _uuid(),
                    "order_id": order_id,
                    "contact_id": c["id"],
                    "product_sku": prod[0],
                    "product_name": prod[1],
                    "category": prod[2],
                    "quantity": qty,
                    "unit_price": price,
                    "line_total": subtotal,
                    "currency": "ILS",
                })
            orders.append({
                "order_id": order_id,
                "contact_id": c["id"],
                "order_datetime": order_datetime,
                "channel": random.choice(["Web", "Web", "Mobile", "Store"]),
                "total_amount": round(total, 2),
                "currency": "ILS",
                "status": random.choices(["Delivered", "Shipped", "Processing", "Returned"],
                                          weights=[0.65, 0.20, 0.10, 0.05])[0],
            })
    return orders, lines


# ─── BANKING ─────────────────────────────────────────────────────────────────

ACCOUNT_TYPES = ["Checking", "Savings", "Investment", "Credit", "Mortgage", "Business Checking"]
TX_CATEGORIES = ["Groceries", "Dining", "Travel", "Entertainment", "Healthcare",
                 "Utilities", "Rent/Mortgage", "Shopping", "Transfer", "ATM Withdrawal",
                 "Salary", "Dividend", "Insurance Premium", "Subscription"]


def gen_banking_accounts(contacts: list[dict]) -> tuple[list, list]:
    accounts, transactions = [], []
    for c in contacts:
        n_accounts = random.choices([1, 2, 3], weights=[0.40, 0.40, 0.20])[0]
        for _ in range(n_accounts):
            acct_type = random.choice(ACCOUNT_TYPES[:4])  # avoid over-weighting mortgage
            acct_id = _uuid()
            balance = round(random.uniform(-5000, 200000), 2)
            accounts.append({
                "account_id": acct_id,
                "contact_id": c["id"],
                "account_number": f"IL{random.randint(10000000000000000, 99999999999999999)}",
                "account_type": acct_type,
                "balance": balance,
                "currency": "ILS",
                "opened_date": _date(2010, 2022),
                "interest_rate": round(random.uniform(0, 4.5), 2),
                "status": random.choices(["Active", "Active", "Active", "Frozen", "Closed"],
                                          weights=[0.7, 0.7, 0.7, 0.05, 0.05])[0],
            })
            # 6 months of transactions
            # tx_datetime is DateTime (ENGAGEMENT stream) — P2Y lookback applies.
            for _ in range(random.randint(10, 35)):
                cat = random.choice(TX_CATEGORIES)
                amount = round(random.uniform(5, 3000), 2)
                if cat in ("Salary", "Dividend"):
                    amount = round(random.uniform(3000, 25000), 2)
                transactions.append({
                    "tx_id": _uuid(),
                    "account_id": acct_id,
                    "contact_id": c["id"],
                    "tx_datetime": _recent_datetime(180),
                    "category": cat,
                    "amount": amount if cat in ("Salary", "Dividend", "Transfer") else -amount,
                    "currency": "ILS",
                    "merchant": f"Merchant_{random.randint(1000, 9999)}",
                    "description": f"{cat} transaction",
                })
    return accounts, transactions


# ─── PHARMA ──────────────────────────────────────────────────────────────────

DRUGS = [
    ("Lipitor",      "Cardiovascular",  "Atorvastatin 20mg"),
    ("Metformin",    "Diabetes",        "Metformin 500mg"),
    ("Amoxicillin",  "Antibiotic",      "Amoxicillin 500mg"),
    ("Omeprazole",   "Gastroenterology","Omeprazole 20mg"),
    ("Lisinopril",   "Cardiovascular",  "Lisinopril 10mg"),
    ("Advil",        "Pain Relief",     "Ibuprofen 400mg"),
    ("Zoloft",       "Psychiatry",      "Sertraline 50mg"),
    ("Ventolin",     "Respiratory",     "Salbutamol 100mcg"),
    ("Humira",       "Immunology",      "Adalimumab 40mg"),
    ("Eliquis",      "Cardiovascular",  "Apixaban 5mg"),
    ("Januvia",      "Diabetes",        "Sitagliptin 100mg"),
    ("Symbicort",    "Respiratory",     "Budesonide/Formoterol"),
]

DIAGNOSES = ["Hypertension", "Type 2 Diabetes", "Hypercholesterolaemia", "GERD",
             "Asthma", "Depression", "Anxiety", "Rheumatoid Arthritis",
             "COPD", "Atrial Fibrillation", "Osteoporosis", "Chronic Pain"]


def gen_pharma_prescriptions(contacts: list[dict]) -> list[dict]:
    rows = []
    for c in contacts:
        n_rx = random.choices([1, 2, 3, 4], weights=[0.30, 0.35, 0.25, 0.10])[0]
        for _ in range(n_rx):
            drug = random.choice(DRUGS)
            rows.append({
                "rx_id": _uuid(),
                "contact_id": c["id"],
                "drug_name": drug[0],
                "therapeutic_area": drug[1],
                "formulation": drug[2],
                "diagnosis": random.choice(DIAGNOSES),
                "prescribing_physician": f"Dr. {random.choice(LAST_NAMES)}",
                "fill_datetime": _recent_datetime(720),  # DateTime — REQUIRED for Engagement DLO (P2Y-safe)
                "refills_remaining": random.randint(0, 5),
                "status": random.choices(["Active", "Discontinued", "Expired"], weights=[0.65, 0.20, 0.15])[0],
                "days_supply": random.choice([30, 60, 90]),
            })
    return rows


# ─── TELCO ───────────────────────────────────────────────────────────────────

TELCO_PLANS = [
    ("Starter Mobile",    "Mobile",    40,   0,   0,   10),
    ("Unlimited Mobile",  "Mobile",    80,   0,   0,  100),
    ("Family Bundle",     "Mobile",   180,   0,   0, 1000),
    ("Broadband 100Mbps", "Broadband", 60, 100,   0,    0),
    ("Broadband 1Gbps",   "Broadband", 90, 1000,  0,   0),
    ("TV Basic",          "TV",        30,   0, 100,   0),
    ("TV Premium",        "TV",        70,   0, 250,   0),
    ("Full Bundle",       "Bundle",   160, 500, 200, 500),
    ("Business Mobile",   "Mobile",   350,   0,   0, 9999),
]


def gen_telco_contracts(contacts: list[dict]) -> tuple[list, list]:
    contracts, usage = [], []
    for c in contacts:
        n_contracts = random.choices([1, 2], weights=[0.65, 0.35])[0]
        for _ in range(n_contracts):
            plan = random.choice(TELCO_PLANS)
            start = _date(2020, 2023)
            contract_id = _uuid()
            contracts.append({
                "contract_id": contract_id,
                "contact_id": c["id"],
                "contract_number": f"CTR-{random.randint(10000000, 99999999)}",
                "plan_name": plan[0],
                "plan_type": plan[1],
                "monthly_fee": plan[2] + random.randint(-5, 20),
                "data_allowance_gb": plan[3],
                "tv_channels": plan[4],
                "voice_minutes": plan[5] if plan[5] < 9000 else -1,  # -1 = unlimited
                "start_date": start,
                "end_date": (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=730)).strftime("%Y-%m-%d"),
                "status": random.choices(["Active", "Active", "Active", "Suspended", "Cancelled"],
                                          weights=[0.75, 0.75, 0.75, 0.05, 0.05])[0],
            })
            # 6 months of usage
            for month_offset in range(6):
                ref_date = datetime.today() - timedelta(days=30 * month_offset)
                usage.append({
                    "usage_id": _uuid(),
                    "contract_id": contract_id,
                    "contact_id": c["id"],
                    "usage_date": ref_date.strftime("%Y-%m-01"),  # Date (first day of month)
                    "data_used_gb": round(random.uniform(0, max(plan[3] * 1.1, 5)), 2) if plan[3] > 0 else 0,
                    "voice_minutes_used": random.randint(0, 600),
                    "sms_count": random.randint(0, 200),
                    "overage_charge": round(random.uniform(0, 30), 2),
                })
    return contracts, usage


# ─── HIGHTECH ────────────────────────────────────────────────────────────────

HT_PRODUCTS = [
    # (product_name, tier, base_price, max_seats)
    ("Platform Starter",       "Starter",      1500,  10),
    ("Platform Professional",  "Professional", 3500,  25),
    ("Platform Enterprise",    "Enterprise",   8000, 100),
    ("Analytics Add-on",       "Add-on",       1000,   5),
    ("API Access Pro",         "Add-on",       2000,  10),
]


def gen_hightech(contacts: list[dict], products=None) -> tuple[list, list, list]:
    """Generate SaaS subscriptions, usage records, and support tickets.

    products: optional list of (name, tier, base_price, max_seats) tuples.
              Pass cfg["customProducts"] names via main() to override HT_PRODUCTS.
    """
    effective_products = products if products is not None else HT_PRODUCTS
    today = datetime.today()
    subscriptions, usage_records, support_tickets = [], [], []

    for c in contacts:
        n_subs = random.choices([1, 2], weights=[0.7, 0.3])[0]
        contact_subs = []

        for _ in range(n_subs):
            prod = random.choice(effective_products)
            sub_id = _uuid()
            seats = random.randint(1, prod[3])
            mrr = round(prod[2] * seats / 100, 2)
            start_date = datetime.strptime(_date(2022, 2025), "%Y-%m-%d")
            renewal_date = start_date + timedelta(days=365)
            status = random.choices(
                ["Active", "Lapsed", "Cancelled"],
                weights=[0.80, 0.15, 0.05],
            )[0]
            days_until_renewal = max(0, (renewal_date - today).days) if status == "Active" else 0

            subscriptions.append({
                "sub_id": sub_id,
                "contact_id": c["id"],
                "product_name": prod[0],
                "tier": prod[1],
                "seats": seats,
                "mrr": mrr,
                "start_date": start_date.strftime("%Y-%m-%d"),
                "renewal_date": renewal_date.strftime("%Y-%m-%d"),
                "status": status,
                "days_until_renewal": days_until_renewal,
            })
            contact_subs.append((sub_id, status, seats))

        # Usage records: 12 months for each Active subscription
        for sub_id, status, seats in contact_subs:
            if status != "Active":
                continue
            for month_offset in range(12):
                ref_date = today - timedelta(days=30 * month_offset)
                active_users = random.randint(
                    max(1, int(seats * 0.3)),
                    max(1, int(seats * 1.0)),
                )
                usage_records.append({
                    "usage_id": _uuid(),
                    "subscription_id": sub_id,
                    "contact_id": c["id"],
                    "usage_date": ref_date.strftime("%Y-%m-01"),  # Date (first day of month)
                    "active_users": active_users,
                    "login_count": random.randint(1, 200),
                    "feature_adoption_score": round(random.uniform(0, 100), 1),
                    "data_volume_gb": round(random.uniform(0, 50), 2),
                })

        # Support tickets: 0-3 per contact
        n_tickets = random.choices([0, 1, 2, 3], weights=[0.40, 0.35, 0.18, 0.07])[0]
        for _ in range(n_tickets):
            ticket_status = random.choices(
                ["Open", "Closed", "In Progress"],
                weights=[0.20, 0.65, 0.15],
            )[0]
            resolution_days = 0 if ticket_status in ("Open", "In Progress") else random.randint(1, 15)
            csat_score = 0 if ticket_status != "Closed" else random.choice([1, 2, 3, 4, 5])
            created_date = _recent_date(730)
            _days_open = (datetime.today().date() -
                          datetime.strptime(created_date, "%Y-%m-%d").date()).days
            support_tickets.append({
                "ticket_id": _uuid(),
                "contact_id": c["id"],
                "created_date": created_date,
                "days_since_opened": _days_open,  # pre-computed — used in SupportProfile CI
                "category": random.choice(["Technical", "Billing", "Feature Request", "Onboarding", "Performance"]),
                "severity": random.choices(
                    ["Low", "Medium", "High", "Critical"],
                    weights=[0.45, 0.35, 0.15, 0.05],
                )[0],
                "status": ticket_status,
                "resolution_days": resolution_days,
                "csat_score": csat_score,
            })

    return subscriptions, usage_records, support_tickets


# ─── UTILITIES ───────────────────────────────────────────────────────────────

UTILITY_PLANS_CATALOG = [
    # (type, name, fee_min, fee_max, cons_min, cons_max)
    ("Electricity", "Residential Basic",   45,   90,  200, 500),   # kWh/month
    ("Electricity", "Residential Premium", 75,  130,  400, 900),
    ("Electricity", "Business Standard",  150,  350,  800, 3000),
    ("Gas",         "Residential Gas",     30,   65,  100, 400),   # m3/month
    ("Gas",         "Business Gas",       100,  280,  500, 2000),
    ("Water",       "Residential Water",   20,   40,    8,  25),   # m3/month
]

UTILITY_STATUS_W = [0.85, 0.08, 0.07]   # Active, Suspended, Terminated
UTILITY_STATUSES = ["Active", "Suspended", "Terminated"]


def gen_utility_contracts(contacts: list[dict]) -> list[dict]:
    """Generate 1-2 utility service contracts per customer.

    OTHER stream — mutable records (status, fee may change over time). No P2Y lookback.
    """
    rows = []
    today = datetime.today()
    for c in contacts:
        n_contracts = random.choices([1, 2], weights=[0.60, 0.40])[0]
        for _ in range(n_contracts):
            plan = random.choice(UTILITY_PLANS_CATALOG)
            years_back = random.randint(1, 5)
            days_back = random.randint(0, 365)
            start_dt = today - timedelta(days=years_back * 365 + days_back)
            status = random.choices(UTILITY_STATUSES, weights=UTILITY_STATUS_W)[0]
            monthly_fee = round(random.uniform(plan[2], plan[3]), 2)
            rows.append({
                "contract_id": _uuid(),
                "contact_id":  c["id"],
                "plan_type":   plan[0],
                "plan_name":   plan[1],
                "monthly_fee": monthly_fee,
                "start_date":  start_dt.strftime("%Y-%m-%d"),
                "status":      status,
                # Store plan baseline for consumption generation (not in CSV)
                "_cons_min":   plan[4],
                "_cons_max":   plan[5],
            })
    return rows


def gen_consumption_records(contacts: list[dict], contracts: list[dict]) -> list[dict]:
    """Generate monthly consumption records for the last 24 months per active contract.

    OTHER stream — monthly aggregate records. Date = YYYY-MM-01 for native range filtering.
    Seasonal variation: electricity up in winter (heating) and summer (A/C), gas up in winter.
    """
    today = datetime.today()
    # Build index of active contracts
    active_contracts = [c for c in contracts if c["status"] == "Active"]

    rows = []
    for contract in active_contracts:
        cons_min = contract["_cons_min"]
        cons_max = contract["_cons_max"]
        baseline = (cons_min + cons_max) / 2.0
        plan_type = contract["plan_type"]
        monthly_fee = contract["monthly_fee"]

        for month_offset in range(24):
            ref_date = today - timedelta(days=30 * month_offset)
            month_num = ref_date.month

            # Seasonal adjustment
            if plan_type == "Electricity":
                if month_num in (12, 1, 2):       # winter: heating
                    seasonal = 1.30
                elif month_num in (6, 7, 8):       # summer: A/C
                    seasonal = 1.20
                else:
                    seasonal = 1.00
            elif plan_type == "Gas":
                if month_num in (12, 1, 2):        # winter: high gas use
                    seasonal = 1.60
                elif month_num in (6, 7, 8):       # summer: low gas use
                    seasonal = 0.50
                else:
                    seasonal = 1.00
            else:  # Water — relatively flat
                seasonal = 1.00 + random.uniform(-0.10, 0.10)

            # Consumption with ±30% random fluctuation
            consumption = round(
                baseline * seasonal * random.uniform(0.70, 1.30), 2
            )
            consumption = max(cons_min * 0.5, consumption)  # floor at 50% of min

            # Overage charge (~15% of months)
            overage_charge = round(random.uniform(5, 45), 2) if random.random() < 0.15 else 0.0

            # Monthly bill = fee scaled by consumption ratio + overage
            monthly_bill = round(
                monthly_fee * (consumption / baseline) + overage_charge, 2
            )

            # Unit label
            consumption_unit = "kWh" if plan_type == "Electricity" else "m3"

            rows.append({
                "record_id":         _uuid(),
                "contract_id":       contract["contract_id"],
                "contact_id":        contract["contact_id"],
                "usage_date":        ref_date.strftime("%Y-%m-01"),
                "consumption_value": consumption,
                "consumption_unit":  consumption_unit,
                "monthly_bill":      monthly_bill,
                "overage_charge":    overage_charge,
            })
    return rows


# ─── AIRLINES ────────────────────────────────────────────────────────────────

AIRLINE_ROUTES_CATALOG = [
    # (origin, destination, distance_km, base_fare_min, base_fare_max)
    ("TLV", "LHR", 3600,  280,  950),
    ("TLV", "CDG", 3300,  250,  880),
    ("TLV", "JFK", 9100,  480, 2200),
    ("TLV", "BKK", 7900,  420, 1800),
    ("TLV", "FCO", 2200,  180,  650),
    ("TLV", "BCN", 3200,  220,  780),
    ("TLV", "AMS", 3400,  260,  900),
    ("TLV", "MIA", 9800,  520, 2400),
    ("TLV", "DXB", 2300,  150,  580),
    ("TLV", "IST", 1600,  120,  420),
]

AIRLINE_CABINS = ["Economy", "Premium Economy", "Business", "First"]
AIRLINE_CABIN_W = [0.70, 0.15, 0.12, 0.03]
AIRLINE_CABIN_MILES_MULTIPLIER = {"Economy": 1.0, "Premium Economy": 1.5, "Business": 2.0, "First": 3.0}

AIRLINE_STATUSES = ["Confirmed", "Completed", "Cancelled", "No-show"]
AIRLINE_STATUS_W  = [0.15, 0.75, 0.08, 0.02]


def gen_flight_bookings(contacts: list[dict],
                        routes_catalog=None) -> list[dict]:
    """Generate 2-15 flight booking events per passenger (ENGAGEMENT stream).

    All booking_datetime values within the last 720 days (P2Y-safe Engagement window).

    routes_catalog: optional list of (origin, destination, distance_km, fare_min, fare_max).
    If None, falls back to the default AIRLINE_ROUTES_CATALOG (TLV-hub).
    Override via config.json:  "catalog_overrides": {"routes": [["MAD","LHR",1800,90,420], ...]}
    """
    catalog = routes_catalog if routes_catalog else AIRLINE_ROUTES_CATALOG
    rows = []
    today = datetime.today()
    for c in contacts:
        n_bookings = random.randint(2, 15)
        for _ in range(n_bookings):
            route = random.choice(catalog)
            cabin = random.choices(AIRLINE_CABINS, weights=AIRLINE_CABIN_W)[0]
            cabin_mult = AIRLINE_CABIN_MILES_MULTIPLIER[cabin]
            # Cabin fare adjustment: Business/First multiplied
            cabin_fare_mult = {"Economy": 1.0, "Premium Economy": 1.4, "Business": 2.5, "First": 4.5}[cabin]
            base_fare = round(
                random.uniform(route[3], route[4]) * cabin_fare_mult, 2
            )
            miles_earned = int(route[2] * cabin_mult * 0.05)
            booking_datetime = _recent_datetime(720)
            status = random.choices(AIRLINE_STATUSES, weights=AIRLINE_STATUS_W)[0]
            rows.append({
                "booking_id":       _uuid(),
                "contact_id":       c["id"],
                "booking_datetime": booking_datetime,
                "origin":           route[0],
                "destination":      route[1],
                "cabin_class":      cabin,
                "base_fare":        base_fare,
                "miles_earned":     miles_earned,
                "status":           status,
            })
    return rows


def gen_airline_loyalty(contacts: list[dict], bookings: list[dict]) -> list[dict]:
    """Generate FFP earn events (1 per completed booking) + ~25% of contacts get 1-2 redeems.

    ENGAGEMENT stream: event_datetime required (DateTime, ISO 8601).
    Uses _compute_loyalty_balance() for running balance computation.
    """
    rows = []
    # Earn events from completed bookings
    for booking in bookings:
        if booking["status"] != "Completed":
            continue
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     booking["contact_id"],
            "event_datetime": booking["booking_datetime"],
            "type":           "earn",
            "points":         booking["miles_earned"],
            "reference":      booking["booking_id"],
            "balance":        0,
        })
    # Redeem events (~25% of contacts, 1-2 redemptions each)
    redeem_contacts = random.sample(contacts, k=int(len(contacts) * 0.25))
    for c in redeem_contacts:
        n_redeems = random.choices([1, 2], weights=[0.75, 0.25])[0]
        for _ in range(n_redeems):
            ref_type = random.choice(["Upgrade", "Free Flight"])
            rows.append({
                "tx_id":          _uuid(),
                "contact_id":     c["id"],
                "event_datetime": _recent_datetime(365),
                "type":           "redeem",
                "points":         -random.randint(5000, 30000),
                "reference":      ref_type,
                "balance":        0,
            })
    return _compute_loyalty_balance(rows)


# ─── HEALTHCARE ──────────────────────────────────────────────────────────────

MEDICAL_SPECIALTIES = [
    "General Practitioner", "Cardiologist", "Dermatologist", "Orthopedics",
    "Gynecology", "Pediatrics", "Ophthalmology", "ENT",
    "Oncology", "Mental Health", "Physical Therapy",
]
MEDICAL_SPECIALTY_W = [0.35, 0.08, 0.07, 0.08, 0.07, 0.06, 0.06, 0.05, 0.04, 0.06, 0.08]

VISIT_TYPES  = ["Clinic", "Telemedicine", "Emergency", "Follow-up"]
VISIT_TYPE_W = [0.55, 0.25, 0.10, 0.10]

LAB_TESTS = [
    "Blood Panel", "Cholesterol", "Blood Glucose", "HbA1c",
    "Thyroid Panel", "Kidney Function", "CBC", "Vitamin D", "PSA", "Mammography",
]
RESULT_STATUSES = ["Normal", "Borderline", "Abnormal", "Critical"]
RESULT_STATUS_W = [0.65, 0.22, 0.10, 0.03]

ICD_CODES = [
    "J06.9", "Z00.0", "M54.5", "I10", "E11.9",
    "J45.9", "F32.1", "K21.0", "N39.0", "H52.1",
]

def gen_medical_visits(contacts: list[dict]) -> list[dict]:
    """Generate 2-12 medical visits per member over the last 2 years (OTHER stream)."""
    rows = []
    for c in contacts:
        n_visits = random.choices(
            [2, 3, 4, 5, 6, 8, 10, 12],
            weights=[0.10, 0.15, 0.20, 0.20, 0.15, 0.10, 0.07, 0.03]
        )[0]
        for _ in range(n_visits):
            specialty = random.choices(MEDICAL_SPECIALTIES, weights=MEDICAL_SPECIALTY_W)[0]
            vtype = random.choices(VISIT_TYPES, weights=VISIT_TYPE_W)[0]
            copay = round(random.uniform(0, 40) if vtype != "Emergency" else random.uniform(20, 80), 2)
            rows.append({
                "visit_id":       _uuid(),
                "contact_id":     c["id"],
                "visit_date":     _recent_date(days_back=730),
                "specialty":      specialty,
                "visit_type":     vtype,
                "copay_amount":   copay,
                "diagnosis_code": random.choice(ICD_CODES),
            })
    return rows

def gen_lab_results(contacts: list[dict]) -> list[dict]:
    """Generate 1-6 lab results per member (OTHER stream)."""
    rows = []
    for c in contacts:
        n_tests = random.choices([1, 2, 3, 4, 5, 6], weights=[0.15, 0.25, 0.25, 0.18, 0.10, 0.07])[0]
        for _ in range(n_tests):
            status = random.choices(RESULT_STATUSES, weights=RESULT_STATUS_W)[0]
            rows.append({
                "result_id":     _uuid(),
                "contact_id":    c["id"],
                "test_date":     _recent_date(days_back=730),
                "test_type":     random.choice(LAB_TESTS),
                "result_status": status,
                "is_abnormal":   1 if status in ("Abnormal", "Critical") else 0,
            })
    return rows

# ─── SPORTS CLUB ──────────────────────────────────────────────────────────────

SC_PLANS = [
    # (plan_type, monthly_fee_min, monthly_fee_max, tier)
    ("Basic",    15, 30, "Bronze"),
    ("Standard", 30, 55, "Silver"),
    ("Premium",  55, 90, "Gold"),
    ("VIP",      90, 150, "Platinum"),
    ("Family",   60, 110, "Silver"),
]
SC_PLAN_W = [0.30, 0.30, 0.20, 0.08, 0.12]

SC_ACTIVITY_TYPES = [
    "Gym Floor", "Group Class", "Swimming", "Squash",
    "Tennis", "Personal Training", "Spa", "Basketball", "Cycling Class", "Yoga",
]
SC_ACTIVITY_W = [0.35, 0.25, 0.12, 0.06, 0.05, 0.06, 0.03, 0.04, 0.02, 0.02]

def gen_memberships(contacts: list[dict]) -> list[dict]:
    """Generate 1-2 memberships per member (current + optionally expired).

    Includes pre-computed RenewingSoon and MembershipAgeMonths fields.
    """
    rows = []
    today = datetime.today()
    for c in contacts:
        plan = random.choices(SC_PLANS, weights=SC_PLAN_W)[0]
        monthly_fee = round(random.uniform(plan[1], plan[2]), 2)
        age_months = random.randint(1, 48)
        start_dt = today - timedelta(days=age_months * 30)
        renewal_months = random.randint(1, 13)
        renewal_dt = today + timedelta(days=renewal_months * 30)
        renewing_soon = 1 if renewal_months <= 3 else 0
        status = random.choices(["Active", "Active", "Active", "Suspended", "Cancelled"],
                                weights=[0.80, 0.80, 0.80, 0.08, 0.04])[0]
        rows.append({
            "membership_id":          _uuid(),
            "contact_id":             c["id"],
            "plan_type":              plan[0],
            "monthly_fee":            monthly_fee,
            "start_date":             start_dt.strftime("%Y-%m-%d"),
            "renewal_date":           renewal_dt.strftime("%Y-%m-%d"),
            "renewing_soon":          renewing_soon,
            "membership_age_months":  age_months,
            "status":                 status,
            "tier":                   plan[3],
        })
    return rows

def gen_activity_records(contacts: list[dict]) -> list[dict]:
    """Generate gym/fitness activity records per member (ENGAGEMENT stream, P2Y-safe)."""
    rows = []
    for c in contacts:
        freq_tier = random.choices(["dormant", "occasional", "regular", "enthusiast"],
                                   weights=[0.20, 0.35, 0.30, 0.15])[0]
        n_map = {"dormant": (0, 5), "occasional": (6, 20), "regular": (21, 50), "enthusiast": (51, 100)}
        lo, hi = n_map[freq_tier]
        n_visits = random.randint(lo, hi)
        for _ in range(n_visits):
            activity = random.choices(SC_ACTIVITY_TYPES, weights=SC_ACTIVITY_W)[0]
            duration = random.randint(20, 120)
            calories = int(duration * random.uniform(4, 10))
            rows.append({
                "activity_id":      _uuid(),
                "contact_id":       c["id"],
                "activity_date":    _recent_datetime(720),
                "activity_type":    activity,
                "duration_minutes": duration,
                "location":         f"Branch {random.randint(1, 8)}",
                "calories_burned":  calories,
            })
    return rows


# ─── ECOMMERCE ────────────────────────────────────────────────────────────────

# (product_sku, product_name, category, unit_price_min, unit_price_max)
ECOM_PRODUCTS = [
    ("ELEC-001", "Wireless Headphones",    "Electronics",      40,  250),
    ("ELEC-002", "Smart Watch",            "Electronics",      80,  450),
    ("ELEC-003", "Portable Charger",       "Electronics",      15,   60),
    ("BOOK-001", "Bestseller Novel",       "Books",             8,   25),
    ("BOOK-002", "Non-Fiction Hardback",   "Books",            12,   40),
    ("HOME-001", "Scented Candle Set",     "Home & Garden",    15,   55),
    ("HOME-002", "Kitchen Knife Set",      "Home & Garden",    30,  140),
    ("SPRT-001", "Running Shoes",          "Sports",           50,  200),
    ("SPRT-002", "Yoga Mat",               "Sports",           15,   60),
    ("APPR-001", "Cotton T-Shirt",         "Apparel",          10,   45),
    ("APPR-002", "Denim Jeans",            "Apparel",          30,  120),
    ("APPR-003", "Waterproof Jacket",      "Apparel",          60,  280),
    ("BEAU-001", "Skincare Face Cream",    "Beauty",           20,   90),
    ("BEAU-002", "Perfume 50ml",           "Beauty",           30,  150),
    ("TOYS-001", "LEGO City Set",          "Toys",             25,  120),
    ("TOYS-002", "Board Game",             "Toys",             15,   55),
    ("ELEC-004", "Bluetooth Speaker",      "Electronics",      25,  180),
    ("HOME-003", "Air Fryer",              "Home & Garden",    50,  200),
]

ECOM_CHANNELS  = ["web", "mobile_app", "mobile_web"]
ECOM_CHANNEL_W = [0.45, 0.40, 0.15]

ECOM_PAYMENT  = ["Credit Card", "Debit Card", "PayPal", "Apple Pay", "Google Pay", "Buy Now Pay Later"]
ECOM_DELIVERY = ["Standard", "Express", "Next Day", "Click & Collect"]

ECOM_STATUSES  = ["Completed", "Completed", "Completed", "Returned", "Cancelled"]


def gen_ecom_orders(contacts: list[dict],
                    products_catalog=None) -> tuple:
    """Generate 1-8 online orders per shopper with line items.

    Returns (orders, lines) — both ENGAGEMENT stream (order_datetime).
    All order_datetime values within the last 720 days (P2Y-safe window).

    products_catalog: optional list of (sku, name, category, price_min, price_max).
    Override via config: "catalog_overrides": {"products": [...]}
    """
    catalog = products_catalog if products_catalog else ECOM_PRODUCTS
    orders = []
    lines = []
    for c in contacts:
        n_orders = random.randint(1, 8)
        for _ in range(n_orders):
            order_dt = _recent_datetime(720)
            status = random.choice(ECOM_STATUSES)
            channel = random.choices(ECOM_CHANNELS, weights=ECOM_CHANNEL_W)[0]
            n_items = random.randint(1, 5)
            order_id = _uuid()
            total_amount = 0.0
            for _ in range(n_items):
                prod = random.choice(catalog)
                qty = random.randint(1, 3)
                unit_price = round(random.uniform(prod[3], prod[4]), 2)
                line_total = round(unit_price * qty, 2)
                total_amount += line_total
                lines.append({
                    "line_id":      _uuid(),
                    "order_id":     order_id,
                    "contact_id":   c["id"],
                    "product_sku":  prod[0],
                    "product_name": prod[1],
                    "category":     prod[2],
                    "quantity":     qty,
                    "unit_price":   unit_price,
                    "line_total":   line_total,
                })
            orders.append({
                "order_id":       order_id,
                "contact_id":     c["id"],
                "order_datetime": order_dt,
                "total_amount":   round(total_amount, 2),
                "item_count":     n_items,
                "channel":        channel,
                "payment_method": random.choice(ECOM_PAYMENT),
                "delivery_type":  random.choice(ECOM_DELIVERY),
                "status":         status,
            })
    return orders, lines


def gen_cart_abandonments(contacts: list[dict]) -> list[dict]:
    """Generate 0-3 cart abandonment events for ~40% of shoppers.

    ENGAGEMENT stream — abandonment_datetime is the event datetime.
    All events within the last 720 days (P2Y-safe window).
    """
    rows = []
    eligible = random.sample(contacts, k=int(len(contacts) * 0.40))
    for c in eligible:
        n_abandoned = random.randint(1, 3)
        for _ in range(n_abandoned):
            n_products = random.randint(1, 8)
            cart_value = round(random.uniform(10, 400), 2)
            rows.append({
                "abandonment_id":       _uuid(),
                "contact_id":           c["id"],
                "abandonment_datetime": _recent_datetime(720),
                "product_count":        n_products,
                "cart_value":           cart_value,
                "device_type":          random.choice(["Desktop", "Mobile", "Tablet"]),
                "session_id":           _uuid(),
            })
    return rows


# ─── HOSPITALITY ──────────────────────────────────────────────────────────────

# (hotel_name, city, country)
HOTEL_CATALOG = [
    ("The David Citadel",   "Jerusalem",  "IL"),
    ("Fattal Tel Aviv",     "Tel Aviv",   "IL"),
    ("Herods Palace Eilat", "Eilat",      "IL"),
    ("Leonardo Club Haifa", "Haifa",      "IL"),
    ("Rimonim Galilee",     "Safed",      "IL"),
    ("Dan Panorama TLV",    "Tel Aviv",   "IL"),
    ("Isrotel Royal Beach", "Eilat",      "IL"),
    ("Cramim Resort",       "Jerusalem",  "IL"),
]

HOTEL_ROOM_TYPES = ["Standard", "Superior", "Deluxe", "Junior Suite", "Suite"]
HOTEL_ROOM_TYPE_W = [0.30, 0.25, 0.25, 0.12, 0.08]

HOTEL_ROOM_RATES = {
    "Standard":     (80,  150),
    "Superior":     (120, 200),
    "Deluxe":       (160, 280),
    "Junior Suite": (250, 450),
    "Suite":        (400, 900),
}

HOTEL_STATUSES  = ["Completed", "Completed", "Completed", "Cancelled", "No-show"]
HOTEL_STATUS_W  = [0.80, 0.80, 0.80, 0.10, 0.05]


def gen_hotel_stays(contacts: list[dict],
                    hotels_catalog=None) -> list[dict]:
    """Generate 1-6 hotel stays per guest (ENGAGEMENT stream).

    checkin_datetime is the event datetime — within the last 720 days (P2Y-safe).
    checkout_date = checkin + nights (Date).

    hotels_catalog: optional list of (hotel_name, city, country).
    Override via config: "catalog_overrides": {"hotels": [...]}
    """
    catalog = hotels_catalog if hotels_catalog else HOTEL_CATALOG
    rows = []
    today = datetime.today()
    for c in contacts:
        n_stays = random.randint(1, 6)
        for _ in range(n_stays):
            hotel = random.choice(catalog)
            room_type = random.choices(HOTEL_ROOM_TYPES, weights=HOTEL_ROOM_TYPE_W)[0]
            rate_min, rate_max = HOTEL_ROOM_RATES[room_type]
            nights = random.randint(1, 7)
            nightly_rate = round(random.uniform(rate_min, rate_max), 2)
            room_revenue = round(nightly_rate * nights, 2)
            fnb_revenue = round(random.uniform(0, room_revenue * 0.30), 2)
            total_revenue = round(room_revenue + fnb_revenue, 2)
            status = random.choices(HOTEL_STATUSES, weights=HOTEL_STATUS_W)[0]
            pts_earned = int(total_revenue * 10) if status == "Completed" else 0
            checkin_dt = _recent_datetime(720)
            checkin_date = datetime.fromisoformat(checkin_dt[:10])
            checkout_date = (checkin_date + timedelta(days=nights)).strftime("%Y-%m-%d")
            rows.append({
                "stay_id":               _uuid(),
                "contact_id":            c["id"],
                "checkin_datetime":      checkin_dt,
                "checkout_date":         checkout_date,
                "hotel_name":            hotel[0],
                "city":                  hotel[1],
                "room_type":             room_type,
                "nights_stayed":         nights,
                "room_revenue":          room_revenue,
                "fnb_revenue":           fnb_revenue,
                "total_revenue":         total_revenue,
                "status":                status,
                "loyalty_points_earned": pts_earned,
            })
    return rows


def gen_hospitality_loyalty(contacts: list[dict], stays: list[dict]) -> list[dict]:
    """Generate loyalty earn events from completed stays + ~20% redeems.

    ENGAGEMENT stream: event_datetime = checkin_datetime of the stay.
    Earn rate: 10 pts per currency unit of total revenue.
    Redeems: ~20% of guests, 1-2 redemptions (Free Night or Upgrade).
    Uses _compute_loyalty_balance() for running balance.
    """
    rows = []
    for stay in stays:
        if stay["status"] != "Completed" or stay["loyalty_points_earned"] <= 0:
            continue
        rows.append({
            "tx_id":          _uuid(),
            "contact_id":     stay["contact_id"],
            "event_datetime": stay["checkin_datetime"],
            "type":           "earn",
            "points":         stay["loyalty_points_earned"],
            "reference":      stay["stay_id"],
            "balance":        0,
        })
    redeem_contacts = random.sample(contacts, k=int(len(contacts) * 0.20))
    for c in redeem_contacts:
        n_redeems = random.choices([1, 2], weights=[0.80, 0.20])[0]
        for _ in range(n_redeems):
            ref_type = random.choice(["Free Night", "Room Upgrade", "Spa Credit"])
            rows.append({
                "tx_id":          _uuid(),
                "contact_id":     c["id"],
                "event_datetime": _recent_datetime(365),
                "type":           "redeem",
                "points":         -random.randint(2000, 15000),
                "reference":      ref_type,
                "balance":        0,
            })
    return _compute_loyalty_balance(rows)


# ─── MEDIA generators ────────────────────────────────────────────────────────

MEDIA_PLANS = [
    ("Basic",    "SVOD",  9.99),
    ("Standard", "SVOD", 14.99),
    ("Premium",  "SVOD", 19.99),
    ("Sports",   "SVOD", 24.99),
    ("Trial",    "Trial", 0.00),
]

MEDIA_GENRES = [
    "Drama", "Comedy", "Action", "Thriller", "Documentary",
    "Horror", "Romance", "Sci-Fi", "Sport", "Kids",
]

MEDIA_TITLES = [
    "The Last Kingdom", "City of Shadows", "Wild Horizons",
    "Deep Blue Night", "Neon District", "The Reckoning",
    "Beyond the Wall", "Summer Frenzy", "Code Red", "Origins",
    "The Verdict", "Midnight Hour", "Storm Front", "Lost Signal",
    "Parallel Lives", "Iron Will", "Sacred Ground", "Velocity",
    "Hidden Truth", "Breaking Point",
]

MEDIA_DEVICES = ["Smart TV", "Mobile", "Tablet", "Desktop", "Console"]

MEDIA_STATUSES = ["Active", "Paused", "Cancelled"]
MEDIA_STATUS_W = [0.70, 0.10, 0.20]


def gen_subscriptions(contacts: list) -> list:
    """Generate one subscription per contact for media/streaming industry."""
    rows = []
    for c in contacts:
        plan_name, plan_type, monthly_fee = random.choice(MEDIA_PLANS)
        status = random.choices(MEDIA_STATUSES, weights=MEDIA_STATUS_W)[0]
        # cancelled subs started further back
        start_days = random.randint(30, 730) if status == "Cancelled" else random.randint(7, 365)
        start_date = (datetime.today() - timedelta(days=start_days)).strftime("%Y-%m-%d")
        rows.append({
            "subscription_id": _uuid(),
            "contact_id":      c["id"],
            "plan_name":       plan_name,
            "plan_type":       plan_type,
            "monthly_fee":     monthly_fee,
            "start_date":      start_date,
            "status":          status,
        })
    return rows


def gen_content_views(contacts: list) -> list:
    """Generate content view events (ENGAGEMENT, 720-day window)."""
    rows = []
    for c in contacts:
        n_views = random.randint(1, 30)
        for _ in range(n_views):
            genre = random.choice(MEDIA_GENRES)
            title = random.choice(MEDIA_TITLES)
            duration = random.randint(20, 150)
            completed = random.random() < 0.65
            rows.append({
                "view_id":          _uuid(),
                "contact_id":       c["id"],
                "view_datetime":    _recent_datetime(720),
                "content_id":       _uuid(),
                "title":            title,
                "genre":            genre,
                "duration_minutes": duration,
                "device_type":      random.choice(MEDIA_DEVICES),
                "completed":        str(completed).lower(),
            })
    return rows


# ─── AUTOMOTIVE generators ────────────────────────────────────────────────────

AUTO_CATALOG = [
    ("Toyota",  "Corolla",   "Sedan"),
    ("Toyota",  "RAV4",      "SUV"),
    ("Toyota",  "Camry",     "Sedan"),
    ("Honda",   "Civic",     "Sedan"),
    ("Honda",   "CR-V",      "SUV"),
    ("Ford",    "Focus",     "Hatchback"),
    ("Ford",    "Kuga",      "SUV"),
    ("BMW",     "3 Series",  "Sedan"),
    ("BMW",     "X5",        "SUV"),
    ("Mercedes","C-Class",   "Sedan"),
    ("Mercedes","GLC",       "SUV"),
    ("Audi",    "A4",        "Sedan"),
    ("Audi",    "Q5",        "SUV"),
    ("VW",      "Golf",      "Hatchback"),
    ("VW",      "Tiguan",    "SUV"),
    ("Tesla",   "Model 3",   "Sedan"),
    ("Tesla",   "Model Y",   "SUV"),
    ("Kia",     "Sportage",  "SUV"),
    ("Hyundai", "Tucson",    "SUV"),
    ("Peugeot", "308",       "Hatchback"),
]

AUTO_TRIMS = ["Base", "Sport", "Luxury", "Executive", "SE", "SR"]
AUTO_COLORS = [
    "Midnight Black", "Pearl White", "Silver", "Metallic Blue",
    "Racing Red", "Graphite Grey", "Dark Green", "Champagne Gold",
]
AUTO_STATUSES = ["Active", "Sold", "Scrapped"]
AUTO_STATUS_W = [0.75, 0.15, 0.10]

SERVICE_TYPES = [
    "Oil Change", "Tyre Rotation", "Brake Inspection", "MOT",
    "Full Service", "Battery Replacement", "Air Filter",
    "Transmission Service", "Coolant Flush", "Wheel Alignment",
]

TECHNICIANS = [
    "James Mitchell", "Sarah Blake", "Carlos Rivera", "Priya Sharma",
    "Tom Henderson", "Emma Walsh", "Luca Ferrari", "Ana Torres",
]


def gen_vehicles(contacts: list) -> list:
    """Generate vehicle ownership records."""
    rows = []
    base_year = datetime.today().year
    for c in contacts:
        n_vehicles = random.choices([1, 2, 3], weights=[0.70, 0.23, 0.07])[0]
        for _ in range(n_vehicles):
            make, model, _ = random.choice(AUTO_CATALOG)
            vehicle_year = random.randint(base_year - 10, base_year)
            # price correlates with brand and age
            price_band = {"BMW": (30000, 80000), "Mercedes": (32000, 90000),
                          "Audi": (28000, 70000), "Tesla": (40000, 85000)}.get(make, (12000, 45000))
            purchase_price = round(random.uniform(*price_band), 0)
            purchase_days_ago = random.randint(30, 2000)
            purchase_date = (datetime.today() - timedelta(days=purchase_days_ago)).strftime("%Y-%m-%d")
            rows.append({
                "vehicle_id":     _uuid(),
                "contact_id":     c["id"],
                "vin":            _uuid().replace("-", "")[:17].upper(),
                "make":           make,
                "model":          model,
                "year":           vehicle_year,
                "trim":           random.choice(AUTO_TRIMS),
                "color":          random.choice(AUTO_COLORS),
                "purchase_date":  purchase_date,
                "purchase_price": purchase_price,
                "status":         random.choices(AUTO_STATUSES, weights=AUTO_STATUS_W)[0],
            })
    return rows


def gen_service_records(contacts: list, vehicles: list) -> list:
    """Generate vehicle service history records."""
    # Build a vehicle → contact map
    vehicle_map = {}  # contact_id → list of vehicle_ids
    for v in vehicles:
        vehicle_map.setdefault(v["contact_id"], []).append(v["vehicle_id"])

    rows = []
    for c in contacts:
        vids = vehicle_map.get(c["id"], [])
        if not vids:
            continue
        n_services = random.randint(1, 6)
        for _ in range(n_services):
            labor = round(random.uniform(50, 500), 2)
            parts = round(random.uniform(0, 400), 2)
            rows.append({
                "service_id":   _uuid(),
                "contact_id":   c["id"],
                "vehicle_id":   random.choice(vids),
                "service_date": _recent_date(730),
                "service_type": random.choice(SERVICE_TYPES),
                "mileage":      random.randint(5000, 150000),
                "labor_cost":   labor,
                "parts_cost":   parts,
                "total_cost":   round(labor + parts, 2),
                "technician":   random.choice(TECHNICIANS),
            })
    return rows


# ─── REAL ESTATE generators ───────────────────────────────────────────────────

RE_PROPERTY_TYPES = ["Apartment", "House", "Villa", "Studio", "Penthouse", "Townhouse"]
RE_CHANNELS = ["Website", "Agent", "Portal", "Referral", "Social Media", "Walk-in"]
RE_TRANSACTION_TYPES = ["Purchase", "Rental"]

RE_CITIES = [
    "London", "Manchester", "Birmingham", "Bristol", "Leeds",
    "Edinburgh", "Glasgow", "Liverpool", "Sheffield", "Nottingham",
]

RE_DEFAULT_CATALOG = [
    # (property_type, city, min_price, max_price)
    ("Apartment", "London",     400000, 1500000),
    ("House",     "London",     700000, 3000000),
    ("Apartment", "Manchester", 150000,  450000),
    ("House",     "Manchester", 250000,  700000),
    ("Apartment", "Birmingham", 120000,  350000),
    ("House",     "Birmingham", 200000,  600000),
    ("Villa",     "Bristol",    400000, 1200000),
    ("Studio",    "Edinburgh",   90000,  280000),
    ("House",     "Leeds",      180000,  500000),
    ("Penthouse", "London",    1000000, 5000000),
]

BEDROOMS_BY_TYPE = {
    "Studio": 0, "Apartment": random.choice([1, 2]),
    "House": random.choice([3, 4]),
    "Townhouse": random.choice([2, 3]),
    "Villa": random.choice([4, 5, 6]),
    "Penthouse": random.choice([2, 3, 4]),
}

RE_AGENTS = [
    "Sophie Turner", "James Wilson", "Aisha Patel", "David Chen",
    "Maria Santos", "Robert Kimura", "Emma Clarke", "Faisal Al-Hassan",
]


def gen_property_inquiries(contacts: list) -> list:
    """Generate property inquiry events (ENGAGEMENT, 720-day window)."""
    rows = []
    for c in contacts:
        n_inqs = random.randint(1, 8)
        for _ in range(n_inqs):
            prop = random.choice(RE_DEFAULT_CATALOG)
            prop_type, city, min_p, max_p = prop
            price = round(random.uniform(min_p, max_p), 0)
            bdrms = {"Studio": 0, "Apartment": random.choice([1, 2]),
                     "House": random.choice([3, 4]),
                     "Townhouse": random.choice([2, 3]),
                     "Villa": random.choice([4, 5, 6]),
                     "Penthouse": random.choice([2, 3, 4])}.get(prop_type, 2)
            rows.append({
                "inquiry_id":       _uuid(),
                "contact_id":       c["id"],
                "inquiry_datetime": _recent_datetime(720),
                "property_id":      _uuid(),
                "property_type":    prop_type,
                "listing_price":    price,
                "bedrooms":         bdrms,
                "city":             city,
                "channel":          random.choice(RE_CHANNELS),
            })
    return rows


def gen_property_transactions(contacts: list) -> list:
    """Generate completed property transactions (OTHER DMO)."""
    rows = []
    for c in contacts:
        # ~60% of contacts have at least one closed transaction
        if random.random() > 0.60:
            continue
        n_txns = random.choices([1, 2, 3], weights=[0.70, 0.22, 0.08])[0]
        for _ in range(n_txns):
            prop = random.choice(RE_DEFAULT_CATALOG)
            prop_type, city, min_p, max_p = prop
            sale_price = round(random.uniform(min_p, max_p), 0)
            commission = round(sale_price * random.uniform(0.01, 0.025), 0)
            bdrms = {"Studio": 0, "Apartment": random.choice([1, 2]),
                     "House": random.choice([3, 4]),
                     "Townhouse": random.choice([2, 3]),
                     "Villa": random.choice([4, 5, 6]),
                     "Penthouse": random.choice([2, 3, 4])}.get(prop_type, 2)
            tx_type = random.choices(RE_TRANSACTION_TYPES, weights=[0.55, 0.45])[0]
            close_days_ago = random.randint(30, 1500)
            close_date = (datetime.today() - timedelta(days=close_days_ago)).strftime("%Y-%m-%d")
            rows.append({
                "transaction_id":   _uuid(),
                "contact_id":       c["id"],
                "property_id":      _uuid(),
                "transaction_type": tx_type,
                "close_date":       close_date,
                "sale_price":       sale_price,
                "property_type":    prop_type,
                "bedrooms":         bdrms,
                "city":             city,
                "agent_name":       random.choice(RE_AGENTS),
                "commission":       commission,
            })
    return rows


# ─── BETTING generators ───────────────────────────────────────────────────────

BETTING_GAME_TYPES = ["Sports Betting", "Casino", "Poker", "Lottery", "Virtual Sports"]
BETTING_GAMES = {
    "Sports Betting":  ["Football", "Tennis", "Horse Racing", "Basketball", "Cricket"],
    "Casino":          ["Roulette", "Blackjack", "Slots", "Baccarat", "Live Casino"],
    "Poker":           ["Texas Hold'em", "Omaha", "Seven Card Stud", "Mixed Game", "Sit & Go"],
    "Lottery":         ["National Lottery", "EuroMillions", "Lotto Plus", "Scratch Card", "Daily Draw"],
    "Virtual Sports":  ["Virtual Football", "Virtual Horse Racing", "Virtual Tennis", "Virtual Dogs", "Virtual Cycling"],
}

BETTING_ACCOUNT_TYPES = ["Sports", "Casino", "Combined"]
BETTING_KYC_STATUSES = ["Verified", "Pending", "Failed"]
BETTING_KYC_W = [0.80, 0.15, 0.05]
BETTING_CHANNELS = ["Web", "Mobile App", "Retail"]


def gen_betting_accounts(contacts: list) -> list:
    """Generate one betting account per contact."""
    rows = []
    for c in contacts:
        reg_days_ago = random.randint(30, 1460)
        reg_date = (datetime.today() - timedelta(days=reg_days_ago)).strftime("%Y-%m-%d")
        balance = round(random.uniform(0, 2000), 2)
        deposit_limit = round(random.choice([100, 200, 500, 1000, 2000, 5000]), 0)
        kyc = random.choices(BETTING_KYC_STATUSES, weights=BETTING_KYC_W)[0]
        rg_flag = str(random.random() < 0.08).lower()
        status = random.choices(["Active", "Suspended", "Closed"],
                                weights=[0.82, 0.10, 0.08])[0]
        rows.append({
            "account_id":              _uuid(),
            "contact_id":              c["id"],
            "account_type":            random.choice(BETTING_ACCOUNT_TYPES),
            "registration_date":       reg_date,
            "kyc_status":              kyc,
            "deposit_limit":           deposit_limit,
            "balance":                 balance,
            "status":                  status,
            "responsible_gaming_flag": rg_flag,
        })
    return rows


def gen_betting_transactions(contacts: list) -> list:
    """Generate betting transaction events (ENGAGEMENT, 720-day window)."""
    rows = []
    for c in contacts:
        n_bets = random.randint(1, 40)
        for _ in range(n_bets):
            game_type = random.choice(BETTING_GAME_TYPES)
            game_name = random.choice(BETTING_GAMES[game_type])
            stake = round(random.uniform(1, 200), 2)
            # ~45% of bets win
            won = random.random() < 0.45
            payout = round(stake * random.uniform(1.5, 10.0), 2) if won else 0.0
            net_result = round(payout - stake, 2)
            rows.append({
                "tx_id":                _uuid(),
                "contact_id":           c["id"],
                "transaction_datetime": _recent_datetime(720),
                "game_type":            game_type,
                "game_name":            game_name,
                "stake":                stake,
                "payout":               payout,
                "net_result":           net_result,
                "channel":              random.choices(BETTING_CHANNELS,
                                                       weights=[0.40, 0.50, 0.10])[0],
            })
    return rows


# ─── Postal ───────────────────────────────────────────────────────────────────

POSTAL_STATUSES   = ["Delivered", "In Transit", "Out for Delivery", "Failed", "Returned"]
POSTAL_STATUS_W   = [0.65, 0.15, 0.10, 0.07, 0.03]
POSTAL_SERVICES   = ["Standard", "Express", "Registered", "Eco"]
POSTAL_SERVICE_W  = [0.50, 0.25, 0.15, 0.10]
POSTAL_DESTS      = ["Domestic", "EU", "USA", "UK", "Other International"]
POSTAL_DEST_W     = [0.60, 0.20, 0.08, 0.07, 0.05]
POSTAL_PRODUCTS   = ["PO Box", "Digital Mailbox", "Mail Forwarding", "Premium"]
POSTAL_PROD_ST    = ["Active", "Expired", "Cancelled"]
POSTAL_PROD_ST_W  = [0.70, 0.20, 0.10]


def gen_parcels(contacts: list) -> list:
    """Generate parcel shipment records — ENGAGEMENT stream (720-day window)."""
    rows = []
    for c in contacts:
        n = random.randint(0, 15)
        for _ in range(n):
            rows.append({
                "parcel_id":           _uuid(),
                "contact_id":          c["id"],
                "ship_datetime":       _recent_datetime(720),
                "tracking_number":     f"IL{random.randint(100000000, 999999999)}",
                "status":              random.choices(POSTAL_STATUSES, weights=POSTAL_STATUS_W)[0],
                "service_type":        random.choices(POSTAL_SERVICES, weights=POSTAL_SERVICE_W)[0],
                "weight_kg":           round(random.uniform(0.1, 30.0), 2),
                "destination_country": random.choices(POSTAL_DESTS, weights=POSTAL_DEST_W)[0],
            })
    return rows


def gen_postal_products(contacts: list) -> list:
    """Generate postal product subscriptions — ~40% of customers hold 1-2 products."""
    rows = []
    for c in contacts:
        if random.random() > 0.40:
            continue
        for _ in range(random.randint(1, 2)):
            start_days_ago = random.randint(30, 1095)
            start_date   = (datetime.today() - timedelta(days=start_days_ago)).strftime("%Y-%m-%d")
            renewal_date = (datetime.today() + timedelta(days=random.randint(30, 365))).strftime("%Y-%m-%d")
            rows.append({
                "product_id":   _uuid(),
                "contact_id":   c["id"],
                "product_type": random.choice(POSTAL_PRODUCTS),
                "status":       random.choices(POSTAL_PROD_ST, weights=POSTAL_PROD_ST_W)[0],
                "start_date":   start_date,
                "renewal_date": renewal_date,
            })
    return rows


# ─── Profile enrichment helpers ─────────────────────────────────────────────

PRODUCT_AFFINITY_CATALOG: dict[str, list[str]] = {
    "insurance":   ["Life", "Health", "Property", "Vehicle", "Pension"],
    "food":        ["Dairy", "Meat", "Bakery", "Produce", "Snacks"],
    "retail":      ["Apparel", "Footwear", "Accessories", "Bags", "Sportswear"],
    "banking":     ["Savings", "Credit Card", "Mortgage", "Investment", "Personal Loan"],
    "pharma":      ["Cardiovascular", "Diabetes", "Respiratory", "Pain Relief", "Wellness"],
    "telco":       ["Mobile", "Broadband", "TV", "Bundle", "Data Add-on"],
    "food_b2b":    ["Dairy", "Bakery", "Meat", "Produce", "Snacks"],
    "hightech":    ["Analytics", "Automation", "Collaboration", "Security", "AI"],
    "utilities":   ["Electricity", "Gas", "Water", "Green Energy", "EV Charging"],
    "airlines":    ["Economy", "Business Class", "Long-haul", "Short-haul", "Premium"],
    "ecommerce":   ["Electronics", "Apparel", "Home & Garden", "Sports", "Beauty"],
    "hospitality": ["Standard", "Superior", "Deluxe", "Junior Suite", "Suite"],
    "media":       ["Drama", "Comedy", "Action", "Thriller", "Documentary"],
    "automotive":  ["Sedan", "SUV", "Hatchback", "Electric", "Hybrid"],
    "real_estate": ["Apartment", "House", "Villa", "Studio", "Penthouse"],
    "betting":     ["Sports Betting", "Casino", "Poker", "Lottery", "Virtual Sports"],
    "postal":      ["Standard", "Express", "Registered", "PO Box", "Digital Mailbox"],
}


def _days_since_map(rows: list[dict], contact_field: str, date_field: str) -> dict[str, int]:
    """Return {contact_id: days_since_most_recent_event} from a list of row dicts.

    Handles both Date (YYYY-MM-DD) and DateTime (YYYY-MM-DDTHH:MM:SS.000Z) formats.
    """
    from datetime import date as _dt
    today = _dt.today()
    latest: dict[str, _dt] = {}
    for row in rows:
        cid = row.get(contact_field)
        raw = row.get(date_field, "")
        if not cid or not raw:
            continue
        d = _dt.fromisoformat(str(raw)[:10])
        if cid not in latest or d > latest[cid]:
            latest[cid] = d
    return {cid: (today - d).days for cid, d in latest.items()}


# ─── CSV writer ──────────────────────────────────────────────────────────────

def write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        print(f"  ⚠️  No rows for {path.name} — skipping")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  ✓  {path.name}  ({len(rows):,} rows)")
    return len(rows)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    n = int(cfg.get("n", 10000))   # default 10 000; SKILL requires minimum 7 000
    industry = cfg.get("industry", "insurance").lower()
    slug = cfg.get("clientSlug", "client")
    out_dir = Path(args.out or cfg.get("outputDir", f"data/{slug}"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Market drives city names, country codes, and surname pool.
    # Set by wizard in Step 2 based on the demo audience (IL / ES / US / UK / FR / DE / GLOBAL).
    market = cfg.get("market", "IL").upper()
    mkt_cfg = MARKET_CONFIG.get(market, MARKET_CONFIG["IL"])

    print(f"\n🏭  Generating data for {cfg.get('clientName', slug)} ({industry}) — {n:,} individuals")
    print(f"    Market: {market}  ({mkt_cfg['country']} / {mkt_cfg['currency']})")
    print(f"    Output: {out_dir.resolve()}\n")

    # 1. Contacts
    print("  Generating profiles...")
    contacts = gen_contacts(n, market)

    # Add company_name for B2B industries.
    # food_b2b: store/minimarket names (these are the buyer accounts).
    # hightech: SaaS customer company names.
    # B2C industries get empty string (field excluded from CSV below).
    #
    # accountTypes / storeTypes can be set in config to customise the company names:
    #   "accountTypes": ["Digital Agency", "E-Commerce", "Financial Services"]  ← hightech
    #   "storeTypes":   ["Supermercado", "Alimentación", "Frutería"]             ← food_b2b
    _store_types = cfg.get("storeTypes",
                           DEFAULT_STORE_TYPES.get(market, DEFAULT_STORE_TYPES["IL"]))
    _ht_types    = cfg.get("accountTypes", DEFAULT_ACCOUNT_TYPES)

    for c in contacts:
        if industry == "food_b2b":
            c["company_name"] = f"{random.choice(_store_types)} {c['last_name']}"
        elif industry == "hightech":
            c["company_name"] = f"{c['last_name']} {random.choice(_ht_types)}"
        else:
            c["company_name"] = ""

    # B2B-specific profile fields — added here so the DLO contains them only for
    # the B2B industries where the mapping references them (NumberOfEmployees__c,
    # AnnualRevenue__c in B2B_STANDARD_MAPPINGS IndividualProfile section).
    if industry in ("food_b2b", "hightech"):
        emp_sizes = [5, 10, 25, 50, 100, 200, 500, 1000, 5000]
        for c in contacts:
            emp = random.choice(emp_sizes)
            c["number_of_employees"] = emp
            c["annual_revenue"]      = round(emp * random.uniform(50_000, 500_000), 2)

    # Product affinity — industry-specific category preference label
    _affinity_opts = PRODUCT_AFFINITY_CATALOG.get(industry, ["General"])
    for c in contacts:
        c["product_affinity"] = random.choice(_affinity_opts)

    # 2. Contact points — email only (phone/address skipped: IR uses email+name)
    emails, _, _ = _split_contact_points(contacts)
    write_csv(out_dir / "contact_emails.csv", emails)

    # 3. Industry-specific tables
    # NOTE: contacts.csv is written AFTER industry branches below so that
    # days_since_last_purchase (computed from real order data) is included.
    if industry == "insurance":
        print("  Generating insurance policies...")
        policies = gen_insurance_policies(contacts)
        write_csv(out_dir / "insurance_policies.csv", policies)
        print("  Generating claims...")
        claims = gen_insurance_claims(contacts, policies)
        write_csv(out_dir / "insurance_claims.csv", claims)
        # days_since_last_purchase from most-recent policy start date (best proxy for insurance)
        _dslp = _days_since_map(policies, "contact_id", "start_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 180))

    elif industry == "food":
        print("  Generating purchase orders...")
        custom_products = cfg.get("catalog_overrides", {}).get("products")
        if custom_products:
            custom_products = [tuple(p) for p in custom_products]
            cats = sorted({p[2] for p in custom_products})
            print(f"  ℹ️  Using {len(custom_products)} custom products from config "
                  f"(categories: {cats})")
        orders, lines = gen_food_orders(contacts, products_catalog=custom_products)
        write_csv(out_dir / "purchase_orders.csv", orders)
        write_csv(out_dir / "order_lines.csv", lines)
        print("  Generating loyalty transactions...")
        loyalty = gen_loyalty_transactions(contacts, orders)
        write_csv(out_dir / "loyalty_transactions.csv", loyalty)
        _dslp = _days_since_map(orders, "contact_id", "order_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 90))

    elif industry == "retail":
        print("  Generating sales orders...")
        custom_products = cfg.get("catalog_overrides", {}).get("products")
        if custom_products:
            custom_products = [tuple(p) for p in custom_products]
            cats = sorted({p[2] for p in custom_products})
            print(f"  ℹ️  Using {len(custom_products)} custom products from config "
                  f"(categories: {cats})")
        orders, lines = gen_retail_orders(contacts, products_catalog=custom_products)
        write_csv(out_dir / "sales_orders.csv", orders)
        write_csv(out_dir / "order_lines.csv", lines)
        print("  Generating loyalty transactions...")
        loyalty = gen_retail_loyalty(contacts, orders)
        write_csv(out_dir / "loyalty_transactions.csv", loyalty)
        _dslp = _days_since_map(orders, "contact_id", "order_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 90))

    elif industry == "banking":
        print("  Generating accounts and transactions...")
        accounts, transactions = gen_banking_accounts(contacts)
        write_csv(out_dir / "financial_accounts.csv", accounts)
        write_csv(out_dir / "transactions.csv", transactions)
        print("  Generating banking products (credit cards, loans, mortgage)...")
        banking_products = gen_banking_products(contacts)
        write_csv(out_dir / "banking_products.csv", banking_products)
        print("  Generating loyalty transactions...")
        loyalty = gen_banking_loyalty(contacts, transactions)
        write_csv(out_dir / "loyalty_transactions.csv", loyalty)
        _dslp = _days_since_map(transactions, "contact_id", "tx_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 30))

    elif industry == "pharma":
        print("  Generating prescriptions...")
        prescriptions = gen_pharma_prescriptions(contacts)
        write_csv(out_dir / "prescriptions.csv", prescriptions)
        _dslp = _days_since_map(prescriptions, "contact_id", "fill_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 365))

    elif industry == "telco":
        print("  Generating contracts and usage...")
        contracts, usage = gen_telco_contracts(contacts)
        write_csv(out_dir / "service_contracts.csv", contracts)
        write_csv(out_dir / "usage_records.csv", usage)
        _dslp = _days_since_map(contracts, "contact_id", "start_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 365))

    elif industry == "food_b2b":
        print("  Generating wholesale orders...")
        custom_products = cfg.get("catalog_overrides", {}).get("products")
        if custom_products:
            custom_products = [tuple(p) for p in custom_products]
            cats = sorted({p[2] for p in custom_products})
            print(f"  ℹ️  Using {len(custom_products)} custom products from config "
                  f"(categories: {cats})")
        wholesale_orders, wholesale_lines = gen_food_b2b(contacts, products_catalog=custom_products)
        write_csv(out_dir / "wholesale_orders.csv", wholesale_orders)
        write_csv(out_dir / "wholesale_order_lines.csv", wholesale_lines)
        print("  Generating loyalty transactions...")
        loyalty = gen_food_b2b_loyalty(contacts, wholesale_orders)
        write_csv(out_dir / "loyalty_transactions.csv", loyalty)
        _dslp = _days_since_map(wholesale_orders, "contact_id", "order_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 90))

    elif industry == "hightech":
        # customProducts: optional list of product names from config.
        # The wizard sets these based on its knowledge of the client (e.g. SimilarWeb plans).
        # Names replace the generic "Platform Starter / Professional / Enterprise" labels
        # while keeping the same pricing tiers and seat structures.
        custom_product_names = cfg.get("customProducts", [])
        effective_products = list(HT_PRODUCTS)
        if custom_product_names:
            for i, name in enumerate(custom_product_names[:len(effective_products)]):
                old = effective_products[i]
                effective_products[i] = (name, old[1], old[2], old[3])
            print(f"  ℹ️  Using custom product names: {[p[0] for p in effective_products]}")
        print("  Generating subscriptions, usage, and support tickets...")
        subs, usage, tickets = gen_hightech(contacts, products=effective_products)
        write_csv(out_dir / "ht_subscriptions.csv", subs)
        write_csv(out_dir / "ht_usage_records.csv", usage)
        write_csv(out_dir / "ht_support_tickets.csv", tickets)
        _dslp = _days_since_map(subs, "contact_id", "start_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 365))

    elif industry == "utilities":
        print("  Generating utility contracts...")
        contracts = gen_utility_contracts(contacts)
        # Strip internal helpers before writing CSV
        contracts_csv = [{k: v for k, v in r.items() if not k.startswith("_")} for r in contracts]
        write_csv(out_dir / "utility_contracts.csv", contracts_csv)
        print("  Generating consumption records...")
        consumption = gen_consumption_records(contacts, contracts)
        write_csv(out_dir / "consumption_records.csv", consumption)
        _dslp = _days_since_map(contracts_csv, "contact_id", "start_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 180))

    elif industry == "airlines":
        print("  Generating flight bookings...")
        # Allow SE to override routes via config: "catalog_overrides": {"routes": [...]}
        # Each route: [origin, destination, distance_km, fare_min, fare_max]
        # Example (Iberia): ["MAD", "LHR", 1800, 90, 420]
        custom_routes = cfg.get("catalog_overrides", {}).get("routes")
        if custom_routes:
            custom_routes = [tuple(r) for r in custom_routes]
            print(f"  ℹ️  Using {len(custom_routes)} custom routes from config "
                  f"(hubs: {sorted({r[0] for r in custom_routes})})")
        bookings = gen_flight_bookings(contacts, routes_catalog=custom_routes)
        write_csv(out_dir / "flight_bookings.csv", bookings)
        print("  Generating FFP loyalty miles...")
        loyalty = gen_airline_loyalty(contacts, bookings)
        write_csv(out_dir / "loyalty_transactions.csv", loyalty)
        _dslp = _days_since_map(bookings, "contact_id", "booking_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 180))

    elif industry == "healthcare":
        print("  Generating medical visits...")
        visits = gen_medical_visits(contacts)
        write_csv(out_dir / "medical_visits.csv", visits)
        print("  Generating lab results...")
        lab_results = gen_lab_results(contacts)
        write_csv(out_dir / "lab_results.csv", lab_results)
        _dslp = _days_since_map(visits, "contact_id", "visit_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 365))

    elif industry == "sports_club":
        print("  Generating memberships...")
        memberships = gen_memberships(contacts)
        write_csv(out_dir / "memberships.csv", memberships)
        print("  Generating activity records...")
        activities = gen_activity_records(contacts)
        write_csv(out_dir / "activity_records.csv", activities)
        _dslp = _days_since_map(activities, "contact_id", "activity_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 90))

    elif industry == "ecommerce":
        print("  Generating ecommerce orders...")
        custom_products = cfg.get("catalog_overrides", {}).get("products")
        if custom_products:
            custom_products = [tuple(p) for p in custom_products]
            cats = sorted({p[2] for p in custom_products})
            print(f"  ℹ️  Using {len(custom_products)} custom products from config "
                  f"(categories: {cats})")
        orders, lines = gen_ecom_orders(contacts, products_catalog=custom_products)
        write_csv(out_dir / "ecom_orders.csv", orders)
        write_csv(out_dir / "ecom_order_lines.csv", lines)
        print("  Generating cart abandonments...")
        carts = gen_cart_abandonments(contacts)
        write_csv(out_dir / "cart_abandonments.csv", carts)
        _dslp = _days_since_map(orders, "contact_id", "order_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 120))

    elif industry == "hospitality":
        print("  Generating hotel stays...")
        custom_hotels = cfg.get("catalog_overrides", {}).get("hotels")
        if custom_hotels:
            custom_hotels = [tuple(h) for h in custom_hotels]
            cities = sorted({h[1] for h in custom_hotels})
            print(f"  ℹ️  Using {len(custom_hotels)} custom hotels from config "
                  f"(cities: {cities})")
        stays = gen_hotel_stays(contacts, hotels_catalog=custom_hotels)
        write_csv(out_dir / "hotel_stays.csv", stays)
        print("  Generating loyalty transactions...")
        loyalty = gen_hospitality_loyalty(contacts, stays)
        write_csv(out_dir / "loyalty_transactions.csv", loyalty)
        _dslp = _days_since_map(stays, "contact_id", "checkin_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 365))

    elif industry == "media":
        print("  Generating subscriptions...")
        subscriptions = gen_subscriptions(contacts)
        write_csv(out_dir / "subscriptions.csv", subscriptions)
        print("  Generating content views...")
        content_views = gen_content_views(contacts)
        write_csv(out_dir / "content_views.csv", content_views)
        _dslp = _days_since_map(content_views, "contact_id", "view_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 90))

    elif industry == "automotive":
        print("  Generating vehicles...")
        vehicles = gen_vehicles(contacts)
        write_csv(out_dir / "vehicles.csv", vehicles)
        print("  Generating service records...")
        service_records = gen_service_records(contacts, vehicles)
        write_csv(out_dir / "service_records.csv", service_records)
        _dslp = _days_since_map(service_records, "contact_id", "service_date")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 365))

    elif industry == "real_estate":
        print("  Generating property inquiries...")
        property_inquiries = gen_property_inquiries(contacts)
        write_csv(out_dir / "property_inquiries.csv", property_inquiries)
        print("  Generating property transactions...")
        property_transactions = gen_property_transactions(contacts)
        write_csv(out_dir / "property_transactions.csv", property_transactions)
        _dslp = _days_since_map(property_inquiries, "contact_id", "inquiry_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 180))

    elif industry == "betting":
        print("  Generating betting accounts...")
        betting_accounts = gen_betting_accounts(contacts)
        write_csv(out_dir / "betting_accounts.csv", betting_accounts)
        print("  Generating betting transactions...")
        betting_transactions = gen_betting_transactions(contacts)
        write_csv(out_dir / "betting_transactions.csv", betting_transactions)
        _dslp = _days_since_map(betting_transactions, "contact_id", "transaction_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(1, 60))

    elif industry == "postal":
        print("  Generating parcels...")
        parcels = gen_parcels(contacts)
        write_csv(out_dir / "parcels.csv", parcels)
        print("  Generating postal products...")
        postal_products = gen_postal_products(contacts)
        write_csv(out_dir / "postal_products.csv", postal_products)
        _dslp = _days_since_map(parcels, "contact_id", "ship_datetime")
        for c in contacts:
            c["days_since_last_purchase"] = _dslp.get(c["id"], random.randint(30, 180))

    else:
        # fallback: just contacts + email engagement
        print(f"  ℹ️  No custom data template for '{industry}' — individual + email only")
        for c in contacts:
            c["days_since_last_purchase"] = random.randint(1, 180)

    # 3b. Write contacts.csv (after industry branches — days_since_last_purchase is now set)
    # company_name is mapped to ssot__Account__dlm.ssot__Name__c for B2B.
    # For B2C industries we exclude it from the CSV to keep the file clean.
    _excl_fields = {"email", "phone", "street_address", "postal_code"}
    if industry not in ("food_b2b", "hightech"):
        _excl_fields.add("company_name")
    write_csv(out_dir / "contacts.csv", [
        {k: v for k, v in c.items() if k not in _excl_fields}
        for c in contacts
    ])

    # 4. Email engagement (all industries)
    print("  Generating email engagements...")
    engagements = gen_email_engagement(contacts, industry)
    write_csv(out_dir / "email_engagement.csv", engagements)

    # 5. Web engagement (all industries) — ENGAGEMENT stream, 2-year window
    print("  Generating web engagements...")
    web_events = gen_web_engagement(contacts, industry)
    write_csv(out_dir / "web_engagement.csv", web_events)

    # 6. Write manifest for downstream scripts
    manifest = {
        "n_contacts": len(contacts),
        "industry": industry,
        "files": [str(p.name) for p in sorted(out_dir.glob("*.csv"))],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n✅  Done — {sum(1 for _ in out_dir.glob('*.csv'))} CSV files written to {out_dir.resolve()}")


if __name__ == "__main__":
    main()
