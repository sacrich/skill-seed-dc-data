# Changelog

All notable changes to `seed-demo-data` are documented here.
Format: [version] — date — summary.

---

## [1.0.0] — 2026-06-26

### 18 verticals — full pipeline for each

insurance · food (B2C) · food_b2b · retail · banking · pharma · telco · hightech ·
utilities · airlines · healthcare · sports_club · ecommerce · hospitality ·
media · automotive · real_estate · betting

### Pipeline per vertical
- Synthetic data generation (`gen_data.py`) — profiles + all transactional tables
- Custom DMOs with descriptions and field types (`create_dmos.py`)
- Field mappings — all CSV columns mapped to DMO fields (`create_mappings.py`)
- DMO relationships — correct cardinality, traversable in Segment Builder (`create_relationships.py`)
- Identity Resolution — B2C (Individual) and B2B (Account) modes (`setup_ir.py`)
- Calculated Insights — 2–3 CIs per vertical, 6-hour refresh schedule (`create_calculated_insights.py`)
- Segments — 5 segments per vertical, count-verified before publishing (`create_segments.py`)

### Architecture
- Standard DMOs used where available: `ssot__Individual__dlm`, `ssot__Account__dlm`,
  `WebEngagement__dlm`, `EmailEngagement__dlm` — custom fields added via `extend_standard_dmo()`
  instead of creating separate profile DMOs
- `LoyaltyTransaction__dlm` shared (idempotent) across food, retail, banking, food_b2b,
  airlines, hospitality — created once per org
- All event dates capped at 720 days back (P2Y lookback safe margin)
- Pre-computed fields (`VehicleAgeYears__c`, `RenewingSoon__c`, `MembershipAgeMonths__c`,
  `IsHighBudget__c`) stored in CSVs to avoid SQL date functions in CI queries

### Catalog intelligence
- Wizard auto-infers product/route/hotel/vehicle/property catalogs from client knowledge
- SE is never asked for catalog details — proposed in Step 3 data plan for correction
- Configurable via `catalog_overrides` in `config.json` for fully custom catalogs

### Market & client personalization
- `market` config key sets currency, locale, city names, representative data
- `client_name` used as stream prefix and in generated company/product names

### Quality gates
- Step 5b: all streams must show rows before DMO creation
- Step 6d2: IR must be COMPLETED before CIs are created
- Step 6e3: CIs must have output rows before segments are published

---

## Upcoming

- education vertical (universities, EdTech)
- travel/OTA vertical (Booking.com, Expedia, Issta)
