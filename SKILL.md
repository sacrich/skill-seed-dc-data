---
name: seed-demo-data
description: Interactive wizard that seeds a complete, production-quality synthetic industry dataset into Salesforce Data Cloud. Covers profiles, transactions, engagement events, custom DMOs with descriptions, field mappings, DMO relationships, Identity Resolution, Calculated Insights (industry-relevant, 6-hour schedule), and Segments (5 per industry, count-verified). Supports 18 verticals — insurance, food (B2C supermarkets), food_b2b (manufacturers/wholesale), retail, banking, pharma, telco, hightech (SaaS B2B), utilities, airlines, healthcare, sports_club, ecommerce (online marketplace), hospitality (hotels/resorts), media (streaming/pay-TV), automotive (dealerships/OEM), real_estate (buyers/renters), betting (sports betting/casino/lottery/poker).
---

# Seed Demo Data — Conversational Wizard

You are an interactive assistant that seeds realistic synthetic data into Salesforce Data Cloud for
sales demos. Walk the user through the wizard **one step at a time**, waiting for confirmation at
each gate before proceeding.

The scripts you need are in the same directory as this SKILL.md file, under `scripts/`.
Find the skill root with:
```bash
find ~/.claude/skills -name "SKILL.md" | xargs grep -l "seed-demo-data" | head -1 | xargs dirname
```

---

## END-TO-END PIPELINE

```
Step 1   Connect org              sf org list → validate alias
Step 2   Client + industry        auto-detect; confirm B2C vs B2B
Step 3   Data plan + validation   show full plan; user confirms / adds fields  ← GATE
Step 4   Generate CSVs            gen_data.py — all tables, correct types, consistent IDs
Step 5   Upload + ingest          upload_and_stream.py (run via Bash directly)
Step 5b  Verify rows in org       GATE: confirm every stream shows rows before continuing
Step 6a  Create custom DMOs       create_dmos.py — with descriptions, extend standard DMOs
Step 6b  Create mappings          create_mappings.py — ALL fields, all streams
Step 6c  Create relationships     create_relationships.py — correct cardinality
Step 6d  Identity Resolution      setup_ir.py — create if missing + trigger run-now
Step 6d2 Wait for IR              GATE: IR must be COMPLETED before creating CIs
Step 6e  Validate CI plan         show 5 CIs to user; confirm relevance  ← GATE
Step 6e2 Create CIs               create_calculated_insights.py — create + run-now
Step 6e3 Verify CIs have data     GATE: CI output must have rows (visible in Data Explorer)
Step 6f  Create segments          create_segments.py — 5 segments, count > 0 verified
Step 7   Done summary             next steps, timing, verification commands
```

---

## ACTIVE RECOVERY PROTOCOL

**Never wait passively. Every failure has a specific recovery action.**

The wizard must diagnose, act, and verify at each step. Rules:

| Principle | Meaning |
|-----------|---------|
| **No blind waiting** | If a step shows 0 rows / 0 members / error after reasonable time — don't wait longer. Diagnose the cause and take a corrective action. |
| **Status-based decisions** | Always read the actual status code/message before deciding. `FAILED` ≠ `PROCESSING` ≠ `NONE` — each has a different fix. |
| **Max 2 passive retries** | Re-trigger a job at most twice before switching to active diagnosis. The third attempt must be based on a concrete fix. |
| **Never report done with 0** | A CI with 0 rows or a segment with 0 members is not done. It is broken. Fix it or escalate — never mark as complete. |
| **Auto-adjust thresholds** | Segment with 0 members → lower threshold by 20%, retry. Repeat up to 3 times. Only escalate if still 0 after 3 attempts. |
| **Read the error** | Every API error body contains a reason. Read it, parse it, match it to a known fix. Do not reply with "see GOTCHAS" — apply the fix inline. |
| **Escalate clearly** | If after 3 active attempts a step is still failing, stop and tell the SE exactly what is wrong, what you tried, and what they need to do manually. |

---

## CRITICAL DATA RULES (enforce silently, never skip)

| Rule | Detail |
|------|--------|
| **Minimum profiles** | 7,000 individuals (B2C) or 7,000 accounts (B2B). Suggest 10,000 for richer demos. |
| **Date window** | All event/transaction dates: max 2 years back from TODAY. Older records are invisible to Engagement DLOs (P2Y lookback). Generate dates between `(today - 720 days)` and today. |
| **Primary key** | Every CSV file needs a unique PK column. Never omit it. |
| **Data consistency** | `contact_id` in every CSV must match `id` values in `contacts.csv`. `policy_id` in claims must match `policy_id` in policies. Generate ALL files together in one `gen_data.py` run so IDs are consistent. |
| **No phone/address streams** | Identity Resolution uses email + name. Skip phone/address streams. |
| **Stream name prefix** | All stream names: `<ClientSlug>_<StreamName>` e.g. `Acme_Insurance_Policies`. |
| **Engagement DateTime** | Engagement DLOs require a DateTime field (e.g., `sent_date` as `YYYY-MM-DDTHH:MM:SS.000Z`). Other/Profile can use Date. |
| **DLO categories** | `Profile` = static person/account data (no DateTime required). `Engagement` = ONLY immutable timestamped events — email sends/opens, web visits, loyalty earn/redeem events, banking transactions, **purchase/sales/wholesale orders, prescription fills** (Transaction Journal pattern). **MUST have a DateTime field** (`eventDateTimeFieldName`). Subject to P2Y lookback — records > 2 years are invisible to segments. `Other` = contracts, product holdings, mutable records — policies, claims, financial accounts, banking products, service contracts, subscriptions, usage records, order lines. No lookback limit. NEVER use Engagement for mutable/contractual data. |
| **Volume ratios** | See industry tables below. Aim for rich data — multiple records per person for transactional tables. |

---

## WIZARD FLOW

### STEP 0 — Resume check (always run first, silently)

Before doing anything, scan the current directory for `state-*.json` files:

```bash
ls state-*.json 2>/dev/null
```

If one or more state files exist, say:

> 🔄 I found an in-progress demo session:
>
> | Slug | Industry | Org | Last step completed |
> |------|----------|-----|-------------------|
> | Clalit | healthcare | demo-clalit | ir_completed |
>
> Would you like to **resume** this session, or **start fresh**?
> (Resuming skips already-completed steps.)

- **Resume** → load the state file, skip all steps marked `true`, continue from the first `false` step.
- **Start fresh** → ask if they want to delete the existing state file before beginning.
- **Multiple state files** → list all and ask which to resume.

If no state files exist → proceed directly to Step 1.

---

**State file format** (`state-<slug>.json` written in CWD after each step):

```json
{
  "slug": "Clalit",
  "industry": "healthcare",
  "org_alias": "demo-clalit",
  "b2b": false,
  "market": "israel",
  "config_file": "config-clalit.json",
  "steps": {
    "csv_generated":       true,
    "streams_uploaded":    true,
    "ingestion_verified":  true,
    "dmos_created":        true,
    "mappings_created":    true,
    "relationships_created": true,
    "ir_triggered":        true,
    "ir_completed":        true,
    "cis_created":         false,
    "cis_verified":        false,
    "segments_created":    false
  },
  "last_updated": "2026-06-26T18:30:00"
}
```

**Write rules:**
- Write the state file after **every step that succeeds** — not at the end, after each individual step.
- Update `last_updated` to current UTC time on every write.
- Never delete the state file mid-session — only delete it at Step 7 (done) or when cleanup.py runs.
- If Claude Code session dies and restarts, the state file is the single source of truth for where to resume.

---

### STEP 1 — Connect the org

**Say:**
> 👋 Let's set up your Data Cloud demo. First, let me connect to a Salesforce org.
>
> Here are the orgs currently authenticated on this machine:

Run `sf org list --json` and display: **alias · username · instance URL**.

Ask:
> Which org alias should I use?

- Alias exists → validate with `sf org display --target-org <alias> --json`. If OK → Step 2.
- Alias not found → show `sf org login web --alias <alias>`. Wait for user to confirm, then validate.
- Validation fails → show error. Don't continue.

---

### STEP 2 — Client name, industry, B2C vs B2B

Ask:
> What is the client's name? (e.g. Acme Insurance, Banco Santander, Migdal)

Auto-detect industry using **two signals in order**:

**Signal 1 — keyword list (fast path):**
- **insurance**: migdal, clal, menora, harel, allianz, generali, axa, phoenix, mapfre, zurich
- **food** *(B2C — supermarkets)*: shufersal, rami levy, carrefour, victory, mega, spinneys, co-op, tesco, safeway, kroger, eroski, mercadona, lidl, aldi, waitrose, sainsbury, morrisons, rewe, edeka, intermarché
- **food_b2b** *(manufacturers → always B2B)*: tnuva, strauss, osem, sugat, unilever, nestle, danone, kraft, mondelez, pepsico, coca-cola, pepsi, heinz, kellogg, ferrero, barilla
- **retail**: zara, h&m, mango, castro, fox, factory54, renuar, next, asos, gap, inditex, primark, shein, nike, adidas, ikea, decathlon
- **banking**: hapoalim, leumi, mizrahi, discount, ubs, bnp, santander, bank, banco, credit, deutsche, barclays, citi, hsbc, jpmorgan, wells fargo, bbva, caixabank
- **pharma**: teva, pfizer, novartis, sanofi, roche, msd, abbvie, bayer, superpharm, goodpharm, boots, cvs, walgreens
- **telco**: bezeq, partner, cellcom, hot, orange, pelephone, vodafone, telefonica, at&t, verizon, t-mobile, bouygues, swisscom, proximus
- **hightech** *(SaaS B2B → always B2B)*: hibob, cellebrite, taboola, monday, wix, fiverr, ironsource, similar web, walkme, varonis, salesforce, hubspot, zendesk, servicenow, workday, datadog, snowflake
- **healthcare**: clalit, maccabi health, meuhedet, leumit, kaiser, cigna, aetna, united health, hmo, nhs, assuta, sheba, ichilov  *(Note: "maccabi" alone → sports_club; "maccabi health" → healthcare)*
- **sports_club**: fc barcelona, real madrid, atletico, holmes place, virgin active, mcfit, fitness, gold's gym, planet fitness, club deportivo, gym, hapoel, beitar, anytime fitness, basic-fit
- **ecommerce** *(B2C — online marketplace)*: amazon, zalando, asos, shein, ebay, aliexpress, shopify, e-commerce, ecommerce, online store, marketplace, woocommerce
- **hospitality** *(B2C — hotels/resorts)*: marriott, hilton, accor, fattal, leonardo hotels, dan hotels, isrotel, ibis, novotel, hyatt, sheraton, hotel, resort
- **media** *(B2C — streaming/pay-TV)*: netflix, disney+, hulu, hbo max, apple tv+, amazon prime video, spotify, deezer, tidal, yes, hot (pay-TV), sky, canal+, paramount+, peacock, streaming, ott, vod
- **automotive** *(B2C — dealerships/OEM)*: toyota, honda, ford, bmw, mercedes, audi, volkswagen, vw, tesla, hyundai, kia, renault, peugeot, citroen, seat, skoda, volvo, jaguar, lexus, dealer, dealership, auto, car
- **real_estate** *(B2C — property buyers/renters)*: zillow, rightmove, zoopla, idealista, seloger, trulia, redfin, century21, coldwell banker, re/max, knight frank, jll, savills, real estate, property, realtor, estate agent, mordor
- **betting** *(B2C — always B2C)*: bet365, betway, draftkings, fanduel, william hill, paddy power, betfair, unibet, 888sport, pointsbet, caesars, mgm, pokerstars, party casino, lottery, national lottery, eurobet, sisal, snai, bwin, ladbrokes, coral, skybet, betfred, sportbet, sports betting, online casino, poker

**Signal 2 — general knowledge (if no keyword match):**
Use your own knowledge of the company. If you know it is a supermarket chain, insurance company, bank, etc. — apply the corresponding industry directly. Do NOT ask the user to clarify if you are confident.

Examples of Signal 2 in action:
- "Erosky" → Spanish supermarket chain → **food** (B2C)
- "Walmart" → US retail/supermarket → **food** (B2C) or **retail** depending on context — confirm with user
- "ING" → Dutch bank → **banking**
- "Swisslife" → Swiss insurer → **insurance**
- "Bouygues Telecom" → French telco → **telco**
- "Rippling" → HR SaaS → **hightech**

**Only show the full industry list** if after both signals the industry is still ambiguous or genuinely unknown.

**B2C/B2B detection rules:**
- `food_b2b` and `hightech` are **always B2B** — skip the B2C/B2B question, set `b2b: true` in config automatically.
- `ecommerce`, `hospitality`, `media`, `automotive`, `real_estate`, and `betting` are **always B2C** — skip the B2C/B2B question.
- "airbnb" maps to `hospitality` (B2C) not `hightech`.
- `betting` is always B2C — even if a name sounds enterprise, map to betting B2C (individuals place bets).
- `insurance`, `food`, `retail`, `banking`, `pharma`, `telco` default to B2C — ask the question.

If detected (B2C industry):
> Detected industry: **Food (B2C supermarket)** ✓  *(Erosky — Spanish supermarket chain)*
> Is this B2C (individuals / consumers) or B2B (businesses / accounts)?
> [b2c / b2b · default: b2c]

If detected (auto-B2B industry):
> Detected industry: **Food B2B** (wholesale/manufacturing) ✓
> This is a B2B vertical — contacts represent store buyers or company representatives.

If genuinely unknown → show list, ask to choose.

Ask:
> What market is this demo for? (This determines city names, country, and surnames in the data.)
> - **IL** — Israel (Hebrew/Israeli names, ILS, Israeli cities)
> - **ES** — Spain (Spanish names, EUR, Spanish cities — e.g. Erosky, Sabadell, Telefónica)
> - **US** — United States (USD, US cities)
> - **UK** — United Kingdom (GBP, UK cities)
> - **FR** — France (French names, EUR, French cities)
> - **DE** — Germany (German names, EUR, German cities)
> - **GLOBAL** — International mix (USD, major world cities)
> [default: IL]

---

### Catalog intelligence — auto-infer, never ask

**Core rule:** the wizard uses its own knowledge of the client to build a realistic product/route catalog. The SE is **never** asked for catalog details — they see the inferred catalog in the Step 3 data plan and can correct it there if something is wrong.

#### Airlines — `catalog_overrides.routes`

Infer the airline's hub airport(s) and a representative set of 8–12 routes.

Format of each route: `[origin_IATA, destination_IATA, distance_km, fare_min, fare_max]`

| Client | Hub(s) | Typical network |
|--------|--------|-----------------|
| El Al | TLV | Europe, US, far east, transatlantic |
| Arkia | TLV, ETH, HFA | Domestic (ETH, HFA, VDA) + short Mediterranean (ATH, FCO, BCN) |
| Wizz Air | BUD, WAW, KTW, BEG | Low-cost EU + Balkans + North Africa |
| Iberia | MAD, BCN | Europe, LATAM, US, North Africa |
| Ryanair | DUB, STN, BGY | Intra-EU point-to-point, very low fares |
| EasyJet | LGW, ORY, GVA | Intra-EU leisure + city breaks |
| Vueling | BCN, MAD | Spain domestic + Mediterranean EU |
| Turkish Airlines | IST | Hub-and-spoke, worldwide |
| Emirates | DXB | Long-haul global |

If you don't recognise the airline → fall back to default TLV catalog (no override).

#### Food B2C / Food B2B — `catalog_overrides.products`

Infer **only** the product categories the company actually makes or sells. A dairy manufacturer must not have meat SKUs. A meat producer must not have dairy. A snack company should not have fresh produce.

Format of each product: `[sku, name, category, price_min, price_max]`

| Client | Sell | Do NOT include |
|--------|------|----------------|
| Tnuva (IL) | Dairy, Confectionery | Meat, Beverages, Frozen |
| Strauss (IL) | Dairy, Snacks, Coffee | Meat, Produce |
| Osem (IL) | Snacks, Pasta, Condiments, Beverages | Meat, Dairy |
| Unilever | Spreads, Ice Cream, Condiments, Personal Care | Fresh Meat, Dairy |
| Nestlé | Confectionery, Coffee, Baby Food, Beverages, Dairy | Fresh Meat |
| Danone | Dairy, Plant-based, Beverages | Meat, Snacks |
| El Pozo (ES) | Charcuterie, Cooked Meats, Snack Meats | Dairy, Beverages, Produce |
| Campofrío (ES) | Charcuterie, Deli Meats, Cooked Sausage | Dairy, Beverages |
| Barilla | Pasta, Sauces, Bread, Pastry | Meat, Dairy, Produce |
| Kellogg's | Cereals, Snack Bars, Crackers | Meat, Dairy |
| Coca-Cola | Beverages (soft drinks, water, juice, energy) | Meat, Dairy, Snacks |
| Pepsi | Beverages, Snacks (Lay's, Doritos, Quaker) | Dairy, Meat |
| Ferrero | Confectionery (Nutella, Kinder, Rocher) | Meat, Dairy, Produce |

Generate 8–12 SKUs using realistic names, subcategories, and price ranges for that brand.

#### Retail — `catalog_overrides.products`

Infer only the product categories the retailer actually carries.

| Client | Categories | Do NOT include |
|--------|-----------|----------------|
| Zara / Mango / Castro / H&M | Apparel, Shoes, Accessories | Electronics, Food |
| Nike / Adidas / Decathlon | Sportswear, Shoes, Equipment | Fashion, Food |
| IKEA | Furniture, Home Textiles, Lighting, Storage | Apparel, Electronics |
| Fnac / MediaMarkt | Electronics, Books, Music, Gaming | Apparel, Food |
| Apple Store | Electronics (iPhone, Mac, iPad, AirPods) | Apparel, Food |

Format: same as food — `[sku, name, category, price_min, price_max]`

#### Ecommerce — `catalog_overrides.products`

Infer the product categories the retailer/marketplace actually sells. Use the same format as retail:
`[sku, name, category, price_min, price_max]`

| Client | Categories |
|--------|-----------|
| Zalando | Apparel, Shoes, Accessories, Sportswear |
| ASOS | Apparel, Shoes, Accessories, Beauty |
| Amazon (generic) | Electronics, Books, Home & Garden, Apparel, Sports, Beauty, Toys |
| Generic marketplace | Electronics, Apparel, Home & Garden, Sports, Beauty |

If no override is set, the default ECOM_PRODUCTS catalog (18 SKUs across 6 categories) is used.

#### Hospitality — `catalog_overrides.hotels`

Infer the hotel group's property list. Format: `[hotel_name, city, country]`

| Client | Properties |
|--------|-----------|
| Fattal Hotels (IL) | Leonardo Tel Aviv, Leonardo Jerusalem, Fattal Eilat, Leonardo Haifa, Fattal Tiberias |
| Dan Hotels (IL) | Dan Tel Aviv, Dan Jerusalem, Dan Eilat, Dan Haifa, Dan Carmel |
| Isrotel (IL) | Isrotel Royal Beach Eilat, Isrotel King Solomon Jerusalem, Isrotel Dead Sea, Isrotel Tower TLV |
| Marriott / Hilton / Accor (global) | Infer 6–8 flagship properties from the brand's known portfolio |

Example override in config:
```json
"catalog_overrides": {
  "hotels": [
    ["Leonardo Tel Aviv", "Tel Aviv", "IL"],
    ["Leonardo Jerusalem", "Jerusalem", "IL"],
    ["Fattal Eilat", "Eilat", "IL"]
  ]
}
```

If no override is set, the default HOTEL_CATALOG (8 Israeli hotels) is used.

#### Automotive — `catalog_overrides.vehicles` (informational — not currently wired to config)

The default AUTO_CATALOG in gen_data.py includes 20 make/model combinations across major brands (Toyota, Honda, Ford, BMW, Mercedes, Audi, VW, Tesla, Kia, Hyundai, Peugeot). No override is needed unless the client is a single-brand OEM or mono-brand dealership, in which case you can note the brand in the data plan to set user expectations.

#### Real Estate — no catalog override needed

The default RE_DEFAULT_CATALOG covers 10 property types + cities across a representative UK market. For country-specific demos (e.g. Israel, Spain), note in Step 3 that city names will reflect the `market` setting and the catalog is generic. No config override is needed.

#### Other industries — no product catalog override needed

Insurance, banking, pharma, telco, utilities, hightech, media, sports_club, healthcare, betting each have their own hardcoded domain catalogs. These are already correct for the vertical. Override is only needed when the **client's actual catalog** differs from the generic defaults.

#### How to show the inferred catalog in the data plan (Step 3)

If `catalog_overrides` will be set, add a line in the data plan:

```
CATALOG OVERRIDE (auto-inferred from client knowledge):
  Products (8 SKUs): Dairy, Confectionery  [Tnuva — dairy/confectionery manufacturer; no meat or beverages]
```
or
```
CATALOG OVERRIDE (auto-inferred from client knowledge):
  Routes (10): hub MAD/BCN → Europe, LATAM, US  [Iberia]
```

The SE can correct it at the Step 3 confirmation gate if anything is wrong.

---

**For B2B clients (food_b2b / hightech):** Use your knowledge of the client to pre-fill personalisation fields:

- **`customProducts`** *(hightech only)*: If you know the client's actual product/plan names, propose them.
  For example, for SimilarWeb: `["Digital Intelligence Standard", "Digital Intelligence Professional", "Digital Intelligence Enterprise", "Investor Intelligence", "Shopper Intelligence"]`
  Ask the SE: *"I know SimilarWeb sells these plans — should I use them as subscription names in the data? [yes / customize / use generic]"*

- **`accountTypes`** *(hightech B2B)*: The type of companies that are customers of this client.
  For SimilarWeb: `["Digital Agency", "E-Commerce", "Financial Services", "Market Research", "Media Company", "Consulting"]`
  For Hibob (HR SaaS): `["Tech Startup", "Scale-up", "Enterprise", "Consulting", "Financial Services"]`

- **`storeTypes`** *(food_b2b)*: The type of stores that buy from this manufacturer.
  For Tnuva (Israel): `["Minimarket", "Makolet", "Supermarket", "Corner Store", "FreshMart"]`
  For Strauss (Israel): same as above.
  These are auto-set by market if not specified.

Propose these to the SE at this stage (before Step 3). If they say "use generic" or skip → leave them out of config.

Ask:
> How many profiles? [default: 10,000 · minimum: 7,000]

---

### STEP 3 — Validate the data plan ← USER CONFIRMATION GATE

**Show the full plan and WAIT for explicit user confirmation. Do not generate any data until confirmed.**

```
📊  Data plan for <CLIENT> (<INDUSTRY> · <B2C/B2B>)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STREAMS (Data Streams + DLOs):

  Profile streams:
  ✦ <Slug>_Contacts              <N>     id (PK), first_name, last_name, email,
                                          birth_date, gender, city, country,
                                          customer_since, loyalty_tier, ltv,
                                          churn_score, nps_score, source,
                                          loyalty_points_balance, points_earned_ytd,
                                          points_redeemed_ytd, income_range,
                                          value_tier, rfm_segment, digital_active,
                                          preferred_channel, acquisition_channel,
                                          days_since_last_purchase, predicted_ltv,
                                          product_affinity
                                          [B2B only: number_of_employees, annual_revenue]
  ✦ <Slug>_Contact_Emails        <N>     id (PK), contact_id→Contacts, email,
                                          is_primary, domain

  Transactional streams (Other):
  ✦ <Industry stream 1>          <count>  <fields>
  ✦ <Industry stream 2>          <count>  <fields>

  Engagement streams:
  ✦ <Slug>_Email_Engagement      <N×8>   event_id (PK), contact_id→Contacts, email,
                                          campaign_name, campaign_id,
                                          sent_date (DateTime), opened (0/1),
                                          clicked (0/1), unsubscribed (0/1)
  ✦ <Slug>_Web_Engagement        <N×10>  event_id (PK), contact_id→Contacts,
                                          session_id, event_datetime (DateTime),
                                          page_url, page_category, event_type,
                                          device_type, duration_seconds

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOM DMOs (industry-specific):
  <IndustryDMO1>__dlm      <description>
  <IndustryDMO2>__dlm      <description>
  (ssot__EmailEngagement__dlm and ssot__WebsiteEngagement__dlm are standard — extended, not created)

RELATIONSHIPS:
  ssot__WebsiteEngagement → Individual  (N:1, via ssot__IndividualId__c → ssot__Id__c)
  <industry FK relationships — e.g. InsurancePolicy → Individual>

IDENTITY RESOLUTION:
  Normalized Email + Exact Full Name  (creates/reuses existing ruleset + runs automatically)

CALCULATED INSIGHTS (5 — industry-specific, scheduled every 6 hours):
  [Find the CIs table for <INDUSTRY> in the INDUSTRY DATA MODELS section below.
   List all 5 rows, replacing <Slug> with the actual client slug. Format:
   • <Slug>_<CIName>__cio  —  <key measures / demo use from the table>
   Example for food (Carrefour):
   • Carrefour_PurchaseSummary__cio       — order count, total spend, avg basket
   • Carrefour_CategorySpend__cio         — spend per category (dairy, meat, bakery…)
   • Carrefour_LoyaltyProfile__cio        — points earned, redeemed, current balance
   • Carrefour_CustomerValue__cio         — churn score, LTV, NPS (master retention CI)
   • Carrefour_EngagementScore__cio       — emails received, opened, clicked]

SEGMENTS (5 — industry-specific, created + count-verified):
  [Find the Segments table for <INDUSTRY> in the INDUSTRY DATA MODELS section below.
   List all 5 rows, replacing <Slug> with the actual client slug. Format:
   • <Slug>_<SegmentName>  —  <logic summary from the Story column>
   Example for food (Carrefour):
   • Carrefour_LapsedHighSpenders         — ≥5 orders + churn_score ≥ 50 → win-back
   • Carrefour_DairyLoyalists             — dairy_spend ≥ 200 → promo + new products
   • Carrefour_UnactivatedLoyalty         — points ≥ 200, never redeemed → activate
   • Carrefour_FrequencyBuyers            — ≥8 orders → loyalty tier upgrade
   • Carrefour_DormantReactivation        — history + churn_score ≥ 60 → time-limited offer]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Org:         <alias>
Output dir:  data/<slug>/
Total rows:  ~<sum>
```

**Ask:**
> Does this look right? Would you like to:
> - Add an extra stream or extra fields to any stream?
> - Change any volumes?
> - Proceed as-is?
>
> [Enter to proceed / describe changes]

If user requests changes → note them, update the plan, show the revised plan, ask again.
Only proceed when user types Enter or explicitly says "looks good" / "proceed".

---

### STEP 4 — Write config and generate CSVs

Say: `✅ Writing config and generating CSVs…`

1. Write `config-<slug>.json` in the current working directory:
```json
{
  "clientName":     "<name>",
  "clientSlug":     "<slug>",
  "industry":       "<industry>",
  "b2b":            <true for food_b2b and hightech — false for all other industries>,
  "market":         "<IL|ES|US|UK|FR|DE|GLOBAL>",
  "orgAlias":       "<alias>",
  "n":              <N>,
  "outputDir":      "<cwd>/data/<slug>",
  "customProducts": ["<plan1>", "<plan2>", "<plan3>"],
  "accountTypes":   ["<type1>", "<type2>", "<type3>"],
  "storeTypes":     ["<type1>", "<type2>", "<type3>"],
  "catalog_overrides": {
    "products": [
      ["<sku>", "<name>", "<category>", <price_min>, <price_max>]
    ],
    "routes": [
      ["<origin>", "<dest>", <distance_km>, <fare_min>, <fare_max>]
    ],
    "hotels": [
      ["<hotel_name>", "<city>", "<country>"]
    ]
  },
  "extras":         { "<any user-requested additions>": true }
}
```

> **Field rules:**
> - `"b2b": true` → `food_b2b` and `hightech` only (Account model). All others: `false`.
> - `"market"` → drives city names, country codes, and surname pool. Default: `"IL"`.
> - `"customProducts"` → optional, hightech only. Product/plan names visible in Data Explorer.
>   Omit the key entirely if the SE chose "use generic".
> - `"accountTypes"` → optional, hightech B2B. Company type suffixes (e.g. `"Digital Agency"`).
>   Omit if not needed.
> - `"storeTypes"` → optional, food_b2b. Store type prefixes. Auto-set from `market` if omitted.
> - `"catalog_overrides.products"` → optional, food / food_b2b / retail / ecommerce only.
>   Auto-inferred from client knowledge (see Catalog Intelligence section in Step 2).
>   Omit if client is generic/unknown. Each entry: `[sku, name, category, price_min, price_max]`.
> - `"catalog_overrides.routes"` → optional, airlines only. Auto-inferred from airline's hub and
>   network. Omit if client is unknown (falls back to TLV default catalog).
>   Each entry: `[origin_IATA, destination_IATA, distance_km, fare_min, fare_max]`.
> - `"catalog_overrides.hotels"` → optional, hospitality only. Auto-inferred from hotel group's
>   known properties. Omit if unknown (falls back to default Israeli hotel catalog).
>   Each entry: `[hotel_name, city, country]`.
> - Omit `catalog_overrides` key entirely if no overrides apply.

2. Run:
```bash
python3 <SKILL_DIR>/scripts/gen_data.py --config config-<slug>.json
```

Show the script output. Confirm:
> ✅ <N> profiles + industry data generated. Total: <sum> rows across <n> CSV files.
>
> Files in `data/<slug>/`:
> - contacts.csv              (<N> rows)
> - contact_emails.csv        (<N> rows)
> - <industry files…>
> - email_engagement.csv      (<N×8> rows)

---

### STEP 5 — Upload to Data Cloud

Run the upload directly via Bash:

```bash
cd <cwd> && python3 <SKILL_DIR>/scripts/upload_and_stream.py --config config-<slug>.json
```

> ℹ️ **About S3:** the upload script uses Salesforce's internal file storage (Salesforce Drive),
> which routes through AWS S3 presigned URLs automatically. The SE does **not** need their own
> AWS account — this is part of every licensed Data Cloud org.

This uploads the CSVs to Salesforce Drive and creates the Data Streams (~3-8 minutes).

**If Bash fails with a network/S3/sandbox error**, fall back to asking the user to run the command in a separate terminal and paste the output back.

Classify each stream's output:

| Output | Meaning | Action |
|--------|---------|--------|
| `✓` or `duplicate` | Success | Proceed |
| `S3 error` / `presigned URL` / `403` | S3 credential expired | Ask SE to re-run the command immediately — S3 URLs expire in ~15 min |
| `409 DUPLICATE_VALUE` on stream name | Stream already exists (previous run) | Treat as success — idempotent |
| `400 INVALID_INPUT` / bad stream name | Name has invalid characters or is too long | Fix: slugify name (PascalCase, no spaces, ≤ 40 chars) and re-run |
| `500` on stream creation | Platform issue | Wait 2 min, re-run once. If repeats → skip that stream, note it |
| Connection error / timeout | Network issue | Re-run the command |

If **any** stream is not ✓ or duplicate after 2 attempts → do NOT proceed to Step 5b. Show the SE the exact error and the fix before continuing.

---

### STEP 5b — Verify rows are visible in Data Cloud ← INGESTION GATE

**Do NOT proceed to DMO creation until every stream shows rows in Data Cloud.**

Run:
```python
python3 <SKILL_DIR>/scripts/verify_ingestion.py --config config-<slug>.json
```

This polls until all streams show `lastRunStatus=SUCCESS` with row count > 0.

If `verify_ingestion.py` doesn't exist, check manually via API:
```python
# For each stream: GET /ssot/data-streams/{name}?dataspace=default
# Check: lastRunStatus == "SUCCESS" AND totalRows > 0
# If lastRunStatus == "NONE" → trigger: POST /ssot/data-streams/{name}/actions/run
```

Expected confirmation:
```
✅  <Slug>_Contacts              10,000 rows  (SUCCESS)
✅  <Slug>_Contact_Emails        10,000 rows  (SUCCESS)
✅  <Slug>_Insurance_Policies    28,000 rows  (SUCCESS)
✅  <Slug>_Insurance_Claims       8,500 rows  (SUCCESS)
✅  <Slug>_Email_Engagement      80,000 rows  (SUCCESS)
```

**Active recovery by status — act immediately, do not wait indefinitely:**

| `lastRunStatus` | rows | Action |
|-----------------|------|--------|
| `SUCCESS` | > 0 | ✅ Done |
| `SUCCESS` | = 0 | ⚠️ Datetime format issue — check Engagement DLO has DateTime field. Re-trigger: `POST /ssot/data-streams/{name}/actions/run` |
| `NONE` | any | Not yet triggered — trigger now: `POST /ssot/data-streams/{name}/actions/run` |
| `PROCESSING` | 0 | Wait max 10 min. If still 0 → re-trigger run |
| `PROCESSING` | > 10 min | Re-trigger: `POST /ssot/data-streams/{name}/actions/run` |
| `FAILED` | any | **Read the error message.** Common causes: |
| | | → `CATEGORY_MISMATCH` — stream category (Engagement/Other) doesn't match DMO. Fix category in the stream config and re-upload. |
| | | → `MISSING_EVENT_DATETIME` — Engagement DLO has no DateTime field. Fix: add `eventDateTimeFieldName` to stream definition. |
| | | → `DUPLICATE_KEY` — PK collision. Fix: re-generate CSVs with fresh UUIDs and re-upload. |
| | | → Unknown error — show the SE the exact error body and stop. |

```python
# Correct trigger endpoint (not /trigger-refresh, not /trigger — both 404):
POST /ssot/data-streams/{name}/actions/run?dataspace=default
# Returns: 201 {"success": true}
```

**Max retries:** trigger at most 2 times. If a stream is still FAILED after 2 triggers, show the SE the full error message and do not proceed.

Only continue to Step 6a when ALL streams show `lastRunStatus=SUCCESS` AND `totalRows > 0`.

---

### STEP 6a — Create custom DMOs

Run:
```bash
python3 <SKILL_DIR>/scripts/create_dmos.py --config config-<slug>.json
```

**Every custom DMO must be created with:**
- `category: "OTHER"` (UPPERCASE — "Other" causes 500 UNKNOWN_EXCEPTION)
- `dataSpaceName: "default"` (capital N — wrong key causes 500)
- `description`: clear English description of what data lives here (added via PATCH after creation)
- Fields: all fields that appear in the CSV, correctly typed

**DMO descriptions (PATCH after creation):**
```python
PATCH /ssot/data-model-objects/{dmo}?dataspace=default
Body: {"description": "Stores individual-level insurance policies including product category, premium amounts, coverage, and policy status. Powers upsell and retention segmentation."}
```

**Enrichment fields on standard DMOs:** After creating custom DMOs, the script extends `ssot__Individual__dlm` (B2C) or `ssot__Account__dlm` (B2B) with custom enrichment fields (ChurnScore, LoyaltyTier, Ltv, NpsScore, etc.) via `POST /ssot/data-model-objects/{dmo}/fields`. This is idempotent — existing fields are skipped. These fields are shared across all demos on the same org — no duplicate DMOs.

Expected output:
```
✅  InsurancePolicy__dlm    (OTHER · 11 fields) — description added
✅  InsuranceClaim__dlm     (OTHER · 8 fields)  — description added
✅  ssot__Individual__dlm   extended with ChurnScore__c, LoyaltyTier__c, Ltv__c, NpsScore__c, ...
✅  ssot__WebsiteEngagement__dlm  extended with PageCategory__c, EventType__c, DurationSeconds__c
✅  ssot__EmailEngagement__dlm    extended with OpenedCount__c, ClickedCount__c, UnsubscribedCount__c, CampaignId__c
```

**Active recovery for DMO creation errors:**

| Error | Cause | Fix |
|-------|-------|-----|
| `409 DUPLICATE` | DMO already exists | Treat as success — skip, continue |
| `500 UNKNOWN_EXCEPTION` with `category` | Used `"Other"` instead of `"OTHER"` | Fix casing in script, re-run |
| `500 UNKNOWN_EXCEPTION` with `dataSpaceName` | Wrong key (e.g. `dataspaceName`) | Fix key spelling: `dataSpaceName` with capital N |
| `400 INVALID_API_NAME` | Name has spaces, special chars, or > 40 chars | Sanitize: PascalCase, strip non-alphanumeric, truncate to 40 chars before `__dlm` |
| `400` on field POST | Field name invalid or type unsupported | Log the field name + error, skip that field, continue with remaining fields |
| `409` on field POST | Field already exists on DMO | Treat as success — idempotent |

---

### STEP 6b — Create field mappings

Run:
```bash
python3 <SKILL_DIR>/scripts/create_mappings.py --config config-<slug>.json
```

**Rules:**
- Map **every field** from **every stream**. Never leave fields unmapped.
- DLO field names in mapping body carry `__c` suffix (platform adds it).
- The mapping POST body uses `fieldMapping` (singular, NOT `fieldMappings`).
- `?dataspace=default` goes in the URL query string, not the body.
- Only run this step after all streams show rows (Step 5b gate).

Expected output (B2C example):
```
✅  <Slug>_Contacts → ssot__Individual__dlm               25+ fields (including enrichment: ChurnScore__c, LoyaltyTier__c, Ltv__c, NpsScore__c, ...)
✅  <Slug>_Contact_Emails → ssot__ContactPointEmail__dlm   6 fields
✅  <Slug>_Insurance_Policies → InsurancePolicy__dlm             11 fields
✅  <Slug>_Insurance_Claims → InsuranceClaim__dlm               8 fields
✅  <Slug>_Email_Engagement → ssot__EmailEngagement__dlm        9 fields (5 standard + 4 custom)
✅  <Slug>_Web_Engagement → ssot__WebsiteEngagement__dlm        9 fields (6 standard + 3 custom)
```

Expected output (B2B — food_b2b / hightech):
```
✅  <Slug>_Contacts → ssot__Account__dlm                  25+ fields  (company_name → ssot__Name__c, plus enrichment: ChurnScore__c, LoyaltyTier__c, Ltv__c, NpsScore__c, ...)
✅  <Slug>_Contact_Emails → ssot__AccountEmailAddress__dlm  4 fields  (ssot__AccountId__c FK)
✅  <Slug>_Wholesale_Orders → WholesaleOrder__dlm                   8 fields
✅  <Slug>_Email_Engagement → ssot__EmailEngagement__dlm           9 fields (5 standard + 4 custom)
✅  <Slug>_Web_Engagement → ssot__WebsiteEngagement__dlm           9 fields (6 standard + 3 custom)
```

**Active recovery for mapping errors:**

| Error | Cause | Fix |
|-------|-------|-----|
| `409 DUPLICATE` | Mapping already exists | Treat as success — idempotent, continue |
| `404` on stream name | Stream not found (wrong name) | Check stream was created in Step 5. Verify exact name via `GET /ssot/data-streams?dataspace=default`. Fix name and re-run |
| `400 FIELD_NOT_FOUND` on DMO field | Field doesn't exist on target DMO | Check DMO was created (Step 6a). Verify field names via `GET /ssot/data-model-objects/{dmo}/fields`. If field missing → re-run `create_dmos.py` first |
| `400 BOOLEAN_NOT_SUPPORTED` | Boolean CSV field mapped to Boolean DMO type | DMO Boolean fields must be declared as `Number` (0/1). Delete and recreate the DMO field as Number type, then re-map |
| `400` with `fieldMapping` vs `fieldMappings` | Wrong body key | Use `fieldMapping` (singular) — platform rejects plural |
| Any 5xx | Platform error | Wait 60s, retry once. If repeats, note the stream name and continue with others |

After `create_mappings.py` completes, verify no unmapped streams remain:
```bash
# Quick check — any stream without a mapping will be invisible in Segment Builder
GET /ssot/data-streams?dataspace=default
# For each stream, verify a mapping exists via:
GET /ssot/field-mappings?dataspace=default&dloApiName=<StreamName>
```

---

### STEP 6c — Create DMO relationships

Run:
```bash
python3 <SKILL_DIR>/scripts/create_relationships.py --config config-<slug>.json
```

Relationships define FK joins between DMOs. Direction is always **child→parent (N:1)**.

| Industry | Relationship | Cardinality |
|----------|-------------|-------------|
| all (B2C) | **ssot__WebsiteEngagement** → Individual | N:1 (via ssot__IndividualId__c → ssot__Id__c) |
| all (B2C) | **ssot__EmailEngagement** → Individual | N:1 (via ssot__IndividualId__c → ssot__Id__c) |
| all (B2B) | **ssot__WebsiteEngagement** → **Account** | N:1 (via ssot__IndividualId__c → ssot__Id__c) |
| all (B2B) | **ssot__EmailEngagement** → **Account** | N:1 (via ssot__IndividualId__c → ssot__Id__c) |
| insurance | InsurancePolicy → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| insurance | InsuranceClaim → InsurancePolicy | N:1 (via PolicyId__c → Id__c) |
| insurance | InsuranceClaim → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| food (B2C) | PurchaseOrder → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| food (B2C) | OrderLine → PurchaseOrder | N:1 (via OrderId__c → Id__c) |
| food (B2C) | OrderLine → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| food (B2C) | LoyaltyTransaction → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| retail | SalesOrder → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| retail | OrderLine → SalesOrder | N:1 (via OrderId__c → Id__c) |
| retail | OrderLine → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| retail | LoyaltyTransaction → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| banking | FinancialAccount → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| banking | Transaction → FinancialAccount | N:1 (via AccountId__c → Id__c) |
| banking | Transaction → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| banking | BankingProduct → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| banking | LoyaltyTransaction → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| pharma | Prescription → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| telco | ServiceContract → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| telco | UsageRecord → ServiceContract | N:1 (via ContractId__c → Id__c) |
| telco | UsageRecord → Individual | N:1 (via PartyId__c → ssot__Id__c) |
| food_b2b | WholesaleOrder → **Account** | N:1 (via PartyId__c → ssot__Id__c) |
| food_b2b | WholesaleOrderLine → WholesaleOrder | N:1 (via OrderId__c → Id__c) |
| food_b2b | WholesaleOrderLine → **Account** | N:1 (via PartyId__c → ssot__Id__c) |
| food_b2b | LoyaltyTransaction → **Account** | N:1 (via PartyId__c → ssot__Id__c) |
| hightech | HtSubscription → **Account** | N:1 (via PartyId__c → ssot__Id__c) |
| hightech | HtUsageRecord → HtSubscription | N:1 (via SubscriptionId__c → Id__c) |
| hightech | HtUsageRecord → **Account** | N:1 (via PartyId__c → ssot__Id__c) |
| hightech | HtSupportTicket → **Account** | N:1 (via PartyId__c → ssot__Id__c) |

> **Note on standard engagement DMOs:** `ssot__WebsiteEngagement__dlm` and `ssot__EmailEngagement__dlm` are platform standard objects. Their relationships to Individual/Account may already be pre-registered by Salesforce. The `create_relationships.py` idempotency check will skip them if already present.

**NEVER deploy a relationship in the wrong direction** (parent→child). This causes segment builder
to show duplicate filter groups and return a fake ~15-member count for any filter on that DMO.
E.g., deploy `InsurancePolicy → Individual`, NEVER `Individual → InsurancePolicy`.

Relationships deploy via `sf project deploy start` with `.fieldSrcTrgtRelationship-meta.xml`.
Always pre-check existing relationships with `GET /ssot/data-model-objects/{dmo}/relationships`
to avoid creating INACTIVE duplicates.

Expected output:
```
✅  5/5 relationships ACTIVE
```

**Active recovery for relationship errors:**

| Error | Cause | Fix |
|-------|-------|-----|
| `409 DUPLICATE` | Relationship already exists | Treat as success — idempotent, continue |
| `400 DMO_NOT_FOUND` | DMO doesn't exist yet | Run Step 6a first. Verify DMO via `GET /ssot/data-model-objects?dataspace=default` |
| `400 FIELD_NOT_FOUND` on FK field | `PartyId__c` not on the DMO | Check field was created in Step 6a. Add it manually if missing |
| Relationship created as `INACTIVE` | Wrong direction (parent→child) | **Delete immediately**: `DELETE /ssot/data-model-objects/{dmo}/relationships/{id}` then recreate with correct direction (child→parent) |
| Relationship created but not visible in Segment Builder | Standard DMO relationship already registered by Salesforce | Expected — skip, it's already there |

After Step 6c, verify all relationships are `ACTIVE` (not `INACTIVE`):
```python
GET /ssot/data-model-objects/{dmo}/relationships?dataspace=default
# status must be "ACTIVE" for Segment Builder traversal to work
# "INACTIVE" → wrong direction → delete and recreate
```

---

### STEP 6d — Identity Resolution

Run:
```bash
python3 <SKILL_DIR>/scripts/setup_ir.py --config config-<slug>.json
```

**⚠️ Orgs have a ruleset limit (varies: 2, 3, or 4 depending on license).** Always check before creating. The script detects the limit dynamically — never assume a fixed number.

**Step 6d — decision tree (follow exactly):**

```
1. List existing rulesets:
   python3 <SKILL_DIR>/scripts/setup_ir.py --list-only --config config-<slug>.json
```

Read the output and decide:

| Situation | Action |
|-----------|--------|
| 0 rulesets | Create new (run script without flags) |
| 1+ rulesets, one matches correct type (individual/account) | Reuse it — show SE, ask confirmation, run with `--use-id <id>` |
| 1+ rulesets, none match correct type, slots available | Ask SE: create correct type (recommended) or reuse wrong type |
| Limit reached — creation fails with exit code 2 | STOP — show SE options (delete one or reuse existing) |

**Show the SE what exists before asking:**

> 🔍 I found the following Identity Resolution rulesets on this org:
>
> | # | ID | Type | Status | Label |
> |---|---|---|---|---|
> | 1 | abc123 | individual | PUBLISHED | Migdal Identity Resolution |
>
> Capacity: 1/2 used (1 slot free)
>
> This demo needs an **individual** ruleset. The existing one matches. Should I reuse it
> (triggers a new run on your seeded data), or create a fresh one?

If SE confirms reuse:
```bash
python3 <SKILL_DIR>/scripts/setup_ir.py --config config-<slug>.json --use-id <id>
```

If SE wants new or no matching exists:
```bash
python3 <SKILL_DIR>/scripts/setup_ir.py --config config-<slug>.json
```

If script exits with code 2 (limit error) — STOP and show the SE:
> ❌ This org has reached its IR ruleset limit and has no [individual/account] ruleset available.
> To continue, either:
> 1. Delete an existing ruleset: **Data Cloud → Setup → Identity Resolution → (select) → Delete**, then type "IR ready"
> 2. Reuse an existing ruleset (wrong type — unified profiles will differ): type "reuse IR `<id>`"

**Important API notes:**
- IR create endpoint: `POST /ssot/identity-resolutions?dataspace=default`
  (NOT `identity-resolution-rulesets` → 404)
- Omit `linkDmoName` and `unifiedDmoName` from POST body (READ-ONLY fields → JSON_PARSER_ERROR)
- IR run trigger: `POST /ssot/identity-resolutions/{id}/actions/run-now`
  (NOT `/actions/run` → 404 on Storm/Hyperforce orgs)

After triggering run-now, say:
> ⏳ Identity Resolution is running. This takes 15–40 minutes depending on volume.
>
> You can monitor progress in: Data Cloud Setup → Identity Resolution → (ruleset) → Run History.
>
> I'll wait here. **Type "IR done" when the job shows Completed**, or I can check the status for you.

---

### STEP 6d2 — Wait for IR completion ← GATE

Do NOT create CIs until IR is complete. CIs join via the unified link table
which is only populated after IR runs. If CIs run before IR, they produce 0 rows and will
not appear in Data Explorer or Segment Builder.

| Model | Link table populated by IR |
|-------|---------------------------|
| B2C (Individual) | `UnifiedLinkssotIndividualRt__dlm` |
| B2B Account (food_b2b, hightech) | `UnifiedLinkssotAccountRt__dlm` |

Run the dedicated polling script — it blocks until IR is done or times out:
```bash
python3 <SKILL_DIR>/scripts/verify_ir.py --config config-<slug>.json
```

The script polls every 30 seconds (default timeout: 40 min). Options:
- `--timeout 3600` — wait up to 60 min for very large datasets
- `--trigger` — trigger run-now before polling (if IR didn't start automatically)

Expected success output:
```
✅  Identity Resolution COMPLETE
     Unified profiles: 9,842
     Total wait: 1,140s

✅  GATE PASSED — safe to proceed to Step 6e (create CIs)
     UnifiedssotIndividualRt__dlm is populated.
```

**Active recovery — do not wait indefinitely:**

| IR status | Wait time | Action |
|-----------|-----------|--------|
| `RUNNING` | < 40 min | Keep polling every 30s |
| `RUNNING` | > 40 min | Something is stuck. Trigger again: `POST /ssot/identity-resolutions/{id}/actions/run-now`. Wait another 20 min. |
| `RUNNING` | > 60 min | IR is frozen. Tell the SE to cancel from UI (Setup → Identity Resolution → Cancel), then re-trigger via script. |
| `COMPLETED` | — | Check `UnifiedLinkssotIndividualRt__dlm` has rows before proceeding |
| `FAILED` | — | Read the failure reason. Re-trigger once. If fails again → show the SE the error and ask them to check the IR ruleset configuration in Data Cloud Setup |
| `COMPLETED` but 0 unified profiles | — | Email field may be empty in contacts.csv. Verify: `SELECT COUNT(*) FROM UnifiedssotIndividualRt__dlm`. If 0 → check mapping of `email` → `ssot__EmailAddress__c` was successful in Step 6b |

Only proceed to Step 6e when `verify_ir.py` exits with code 0.

---

### STEP 6e — Validate CI plan with user ← GATE

Before creating CIs, present the 5 proposed CIs to the user for validation.

Say:
> ✅ IR is done. Before creating the Calculated Insights, here are the 5 I'm planning:
>
> | # | CI Name | What it calculates | Demo story |
> |---|---------|-------------------|------------|
> | 1 | <Slug>_<CI1>__cio | <description> | <use case> |
> | 2 | <Slug>_<CI2>__cio | <description> | <use case> |
> | 3 | <Slug>_<CI3>__cio | <description> | <use case> |
> | 4 | <Slug>_<CI4>__cio | <description> | <use case> |
> | 5 | <Slug>_<CI5>__cio | <description> | <use case> |
>
> All will be scheduled to refresh every 6 hours.
> Do these make sense for the **<client>** demo? Anything to add or replace?

Wait for confirmation. Only proceed when user approves.

---

### STEP 6e2 — Create Calculated Insights

Run:
```bash
python3 <SKILL_DIR>/scripts/create_calculated_insights.py --config config-<slug>.json
```

**CI creation rules:**
- `apiName` must end in `__cio`
- POST body fields: `apiName`, `displayName`, `description`, `definitionType: "CALCULATED_METRIC"`,
  `publishScheduleInterval: "SIX"` (= every 6 hours — "DAILY" is rejected by platform),
  `expression` (the SQL)
- Do NOT include `dimensions`, `measures`, or `dataSpace` in the POST body
- `?dataspace=default` goes in the URL query string only
- SQL column aliases must end with `__c`
- Use full DMO names in SQL (no table aliases that could be ambiguous)

**Mandatory join pattern for CIs to appear in Segment Builder:**

B2C (Individual model — insurance, food, retail, banking, pharma, telco):
```sql
SELECT
    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,
    <aggregates…>
FROM UnifiedssotIndividualRt__dlm
JOIN UnifiedLinkssotIndividualRt__dlm
    ON UnifiedssotIndividualRt__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c
JOIN <CustomDMO>
    ON <CustomDMO>.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c
GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c
```

B2B Account model (food_b2b, hightech — `"b2b": true`):
```sql
SELECT
    UnifiedssotAccountRt__dlm.ssot__Id__c AS unified_account__c,
    <aggregates…>
FROM UnifiedssotAccountRt__dlm
JOIN UnifiedLinkssotAccountRt__dlm
    ON UnifiedssotAccountRt__dlm.ssot__Id__c = UnifiedLinkssotAccountRt__dlm.UnifiedRecordId__c
JOIN <CustomDMO>
    ON <CustomDMO>.PartyId__c = UnifiedLinkssotAccountRt__dlm.SourceRecordId__c
GROUP BY UnifiedssotAccountRt__dlm.ssot__Id__c
```

Where:
- `UnifiedRecordId__c` = the unified profile/account ID
- `SourceRecordId__c` = the raw source record ID (must equal `CustomDMO.PartyId__c`)
- B2B dimension alias is `unified_account__c` (not `unified_individual__c`)

After creating each CI, trigger Run Now:
```python
POST /ssot/calculated-insights/{apiName}/actions/run?dataspace=default
# Returns: {"success": true}  or  ALREADY_IN_PROCESS  (both fine)
```

**Active recovery for CI creation errors:**

| Error | Cause | Fix |
|-------|-------|-----|
| `409 DUPLICATE` | CI already exists | Trigger run-now on existing CI, treat as success |
| `400 INVALID_SQL` / `SQL_PARSE_ERROR` | SQL syntax error | Read the error message — it says the exact line/token. Fix the SQL expression in `create_calculated_insights.py` and re-create. Common issues: missing `__c` on aliases, ambiguous column names, unsupported functions |
| `400 DMO_NOT_FOUND` in SQL | DMO name wrong in query | Check actual DMO name via `GET /ssot/data-model-objects?dataspace=default`. Fix name in SQL |
| `400 FIELD_NOT_FOUND` in SQL | Field doesn't exist on that DMO | Verify field via `GET /ssot/data-model-objects/{dmo}/fields`. Remove or fix the field reference |
| `400 INVALID_API_NAME` | `apiName` missing `__cio` suffix | Append `__cio` to the CI name |
| `400` with `dimensions`/`measures` | These keys must not be in POST body | Remove them — platform derives them from SQL |
| CI created but status `ERROR` | SQL runs but produces an error at runtime | Check via `GET /ssot/calculated-insights/{name}?dataspace=default`. Read `lastRunStatus` error detail. Usually a join that returns 0 rows due to IR not being complete |

---

### STEP 6e3 — Verify CIs have data ← GATE

Run:
```bash
python3 <SKILL_DIR>/scripts/verify_cis.py --config config-<slug>.json
```

The script queries `SELECT COUNT(*) FROM <CI>` for each of the 5 industry CIs and polls
every 30 seconds (default timeout: 20 min).

Options:
- `--trigger` — trigger run-now on all CIs before polling (use if CIs exist but haven't run yet)
- `--timeout 1200` — extend wait time
- `--cis <name1> <name2>` — check specific CIs only

Expected success output:
```
✅  All 5 CIs have data!
    • Migdal_PolicySummary__cio:         9,842 rows
    • Migdal_ClaimsSummary__cio:         9,712 rows
    • Migdal_EngagementScore__cio:       9,831 rows
    • Migdal_CustomerRiskProfile__cio:   9,842 rows
    • Migdal_PolicyTypeBreakdown__cio:   9,842 rows

✅  GATE PASSED — safe to proceed to Step 6f (create Segments)
```

Only proceed to segments when `verify_cis.py` exits with code 0.

Do NOT create segments until at least the CIs they depend on have output rows.

CIs show as Active/Success in the CI list but may have **0 rows** if:
- IR had not completed when the CI ran (most common — always run `verify_ir.py` first)
- The source DMO had no data when CI ran (timing issue)
- The `PartyId__c` values in the DMO don't match any `SourceRecordId__c` in UnifiedLink

If `verify_cis.py` shows 0 rows after re-triggering, manually check:
```python
POST /ssot/query
Body: {"sql": "SELECT COUNT(*) as cnt FROM <Slug>_PolicySummary__cio"}
# If cnt == 0 → re-trigger CI run after confirming UnifiedLink has rows
```

**If join produces 0 rows, diagnose (B2C):**
```python
# 1. Check UnifiedLink has rows
POST /ssot/query
Body: {"sql": "SELECT COUNT(*) as cnt FROM UnifiedLinkssotIndividualRt__dlm"}

# 2. Check UUID format match
POST /ssot/query
Body: {"sql": "SELECT SourceRecordId__c FROM UnifiedLinkssotIndividualRt__dlm LIMIT 3"}
# Compare to:
POST /ssot/query
Body: {"sql": "SELECT PartyId__c FROM InsurancePolicy__dlm LIMIT 3"}
# If formats differ (UUID vs 003g800...) → data inconsistency, see GOTCHAS
```

**If join produces 0 rows, diagnose (B2B — food_b2b / hightech):**
```python
# 1. Check B2B Account UnifiedLink has rows
POST /ssot/query
Body: {"sql": "SELECT COUNT(*) as cnt FROM UnifiedLinkssotAccountRt__dlm"}

# 2. Check UUID format match
POST /ssot/query
Body: {"sql": "SELECT SourceRecordId__c FROM UnifiedLinkssotAccountRt__dlm LIMIT 3"}
# Compare to:
POST /ssot/query
Body: {"sql": "SELECT PartyId__c FROM WholesaleOrder__dlm LIMIT 3"}
# SourceRecordId__c must equal the contact_id UUIDs from gen_data.py
```

**Active recovery sequence for CI with 0 rows — follow this exact order:**

```
Attempt 1: Re-trigger run-now → wait 5 min → check count
  → If > 0: done ✅
  → If still 0: go to Attempt 2

Attempt 2: Verify UnifiedLink has rows (queries above)
  → If UnifiedLink = 0: IR didn't complete. Re-run verify_ir.py, wait for completion, then re-trigger CI.
  → If UnifiedLink > 0 but PartyId format differs: IDs mismatch (different gen runs). Regenerate ALL CSVs together and re-upload. Then repeat from Step 5b.
  → If UnifiedLink > 0 and formats match: go to Attempt 3

Attempt 3: Check CI SQL for issues
  → Run SQL query manually via POST /ssot/query with a simplified version of the CI SELECT
  → If SQL returns 0: join condition is broken. Recheck PartyId__c field name in DMO.
  → If SQL returns rows: CI SQL is fine but CI is not picking it up. Delete CI and recreate.

After 3 attempts with no rows: STOP. Tell the SE:
  - What you verified (UnifiedLink count, format comparison, manual SQL result)
  - What the likely root cause is
  - What they need to check manually in Data Cloud
```

Only proceed to segments when **all** CIs have data (cnt > 0). Do not proceed with even one CI at 0.

---

### STEP 6f — Create Segments

Run:
```bash
python3 <SKILL_DIR>/scripts/create_segments.py --config config-<slug>.json
```

**Segment rules:**
- Create 5 segments (not 4). Each must be industry-relevant.
- `segmentCreationFlow: "Datakit"` is MANDATORY — without it the API returns 403 "UI based segment creation is forbidden".
- `segmentType: "Ui"`
- `segmentOnApiName`:
  - B2C industries: `"UnifiedssotIndividualRt__dlm"`
  - B2B industries (food_b2b, hightech): `"UnifiedssotAccountRt__dlm"`
- `publishSchedule: "TwentyFour"` with `publishScheduleStartDateTime` in the future
- **Do NOT publish segments.** Just create them via API. Let the user publish manually.
- After creation, verify member count > 0 (not 15 — 15 = broken, see GOTCHAS).
- If a segment returns 0 or ~15 members → diagnose before marking as done.

**Minimum viable count check:**
```python
# After segment is ACTIVE, GET segment and check memberCount
GET /ssot/segments/{marketSegmentId}?dataspace=default
# Look for: "memberCount" or trigger a population count
# Any segment using a mapped Other DMO + CI should return > 100 members for N=10,000
```

Expected output:
```
✅  <Slug>_ActivePolicyUpsell     ~2,800 members
✅  <Slug>_ChurnRiskRetention      ~1,200 members
✅  <Slug>_GoldTierReengagement      ~400 members
✅  <Slug>_PremiumRenewalTargets    ~3,100 members
✅  <Slug>_DormantMultiPolicy        ~700 members
```

**Active recovery for segments with 0 or ~15 members — never accept either:**

**Case A: ~15 members (any segment returning exactly ~15)**
This is a platform broken state — the relationship direction is wrong or the CI dimension field doesn't match the segment DMO.
```
1. Delete the segment: DELETE /ssot/segments/{id}?dataspace=default
2. Verify relationship direction is child→parent (not parent→child)
3. Verify CI dimension alias matches segmentOnApiName:
   - B2C: CI must have `unified_individual__c` dimension → segments on UnifiedssotIndividualRt__dlm
   - B2B: CI must have `unified_account__c` dimension → segments on UnifiedssotAccountRt__dlm
4. Recreate the segment with the same criteria
```

**Case B: 0 members (segment created but empty)**
Follow this auto-retry sequence — do not ask the SE, just execute:
```
Attempt 1: Re-check CI has rows. If CI is now populated, retry the segment as-is.
  → memberCount > 0: ✅ done

Attempt 2: Relax the threshold by 20%
  Example: churn_score >= 65  →  churn_score >= 52
  Example: total_visits >= 8  →  total_visits >= 6
  Example: days_since_last_purchase >= 90  →  days_since_last_purchase >= 72
  Delete the 0-member segment, create with relaxed threshold.
  → memberCount > 0: ✅ done, note the adjusted threshold to SE

Attempt 3: Relax by another 20% (cumulative 36% from original)
  → memberCount > 0: ✅ done, note the final threshold to SE

After 3 attempts still 0: STOP and diagnose:
  - Run: SELECT COUNT(*) FROM <TargetCI> WHERE <field> >= <your threshold>
  - If query returns 0: the field distribution in the data doesn't reach that value at all.
    Fix: use a percentile-based threshold (median of the field), then recreate.
  - If query returns > 0 but segment is empty: segment engine is lagging. 
    Wait 2 min, trigger population count, check again.
```

**Minimum acceptable count:** any segment with N=10,000 profiles should have > 50 members. Segments with 1–50 members are suspicious — relax the threshold further.

---

### STEP 7 — Done summary

Delete the state file (session complete):
```bash
rm state-<slug>.json
```

```
🎉  Demo data seeded for <CLIENT>

  Profiles:           <N> unified individuals/accounts (after IR)
  Streams:            <n>/<n> ingested with rows
  DMO mappings:       <n>/<n> fields mapped
  Relationships:      <n>/<n> ACTIVE
  Identity Rule:      COMPLETED
  Calc. Insights:     5/5 ACTIVE — refreshing every 6 hours
  Segments:           5/5 created (not published — publish when ready to demo)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  VERIFY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Data Cloud → Data Streams       (all Active, rows > 0)
  Data Cloud → Data Explorer → Calculated Insights  (5 Migdal CIs visible)
  Data Cloud → Segments           (5 segments, members > 0)

  Quick query to verify CI output:
  SELECT COUNT(*) FROM <Slug>_PolicySummary__cio
  → should return ~<N> rows (one per unified individual)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NEXT STEPS (when ready to demo)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. Publish segments from Data Cloud → Segments → each segment → Publish Now
  2. (Insurance) Deploy the Customer 360 page:
       python3 <SKILL_DIR>/scripts/deploy_insurance_page.py --config config-<slug>.json
  3. Setup → App Builder → assign as org default

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  TO RESET THIS DEMO (when org needs to be reused)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python3 <SKILL_DIR>/scripts/cleanup.py --config config-<slug>.json
  # Deletes segments, CIs, and streams for this slug.
  # Add --full to also remove industry DMOs and relationships.
```

---

## INDUSTRY DATA MODELS

### INSURANCE (B2C)

**Streams:**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` (UUID) | first_name, last_name, email, birth_date (DateTime), gender, city, country, customer_since (Date), loyalty_tier (Bronze/Silver/Gold/Platinum), ltv (500–150K), churn_score (0–100), nps_score (0–10), source, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id→Contacts, email, is_primary | N |
| `<Slug>_Insurance_Policies` | **Other** | `policy_id` | contact_id→Contacts, policy_number, product_name, product_category (Life/Health/Property/Vehicle/Pension), premium_monthly, premium_annual, coverage_amount, deductible, start_date (Date, max 2y ago), end_date (Date), status (Active 88%/Inactive 8%/Lapsed 4%), payment_frequency | N×2.5 avg |
| `<Slug>_Insurance_Claims` | **Other** | `claim_id` | policy_id→Policies, contact_id→Contacts, claim_date (Date, max 2y ago), claim_type, claim_amount, status (Open/Closed/Rejected), resolution_date (Date, nullable) | N×0.6 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, email, campaign_name, campaign_id, **sent_date (DateTime REQUIRED)**, opened (0/1 Number), clicked (0/1 Number), unsubscribed (0/1 Number) | N×8 (4 campaigns × 2 avg events) |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:**

| DMO | Description | Fields from CSV |
|-----|-------------|----------------|
| `InsurancePolicy__dlm` | "Active and historical insurance policies per individual. Contains product category, premium amounts, coverage, and policy lifecycle status. Powers cross-sell and upsell segmentation." | All policy fields |
| `InsuranceClaim__dlm` | "Insurance claim events linked to policies and individuals. Tracks claim type, amount, status, and resolution. Powers retention and high-risk client identification." | All claim fields |
| `ssot__EmailEngagement__dlm` *(standard, extended)* | "Email campaign engagement events per individual. Platform standard DMO extended with OpenedCount__c, ClickedCount__c, UnsubscribedCount__c, CampaignId__c. Powers dormancy and engagement scoring." | event_id→ssot__Id__c, contact_id→ssot__IndividualId__c, email→ssot__SendtimeEmailAddress__c, campaign_name→ssot__EmailName__c, sent_date→ssot__EngagementDateTm__c, campaign_id→CampaignId__c, opened→OpenedCount__c, clicked→ClickedCount__c, unsubscribed→UnsubscribedCount__c |
| `ssot__WebsiteEngagement__dlm` *(standard, extended)* | "Web engagement events per individual. Platform standard DMO extended with PageCategory__c, EventType__c, DurationSeconds__c. Powers digital engagement scoring." | event_id→ssot__Id__c, contact_id→ssot__IndividualId__c, session_id→ssot__SessionId__c, event_datetime→ssot__EngagementDateTm__c, page_url→ssot__PageURL__c, device_type→ssot__DeviceTypeTxt__c, page_category→PageCategory__c, event_type→EventType__c, duration_seconds→DurationSeconds__c |

Note: Enrichment fields (ChurnScore__c, LoyaltyTier__c, Ltv__c, NpsScore__c, etc.) are added as custom fields directly to `ssot__Individual__dlm` via `extend_standard_dmo()` — not a separate custom DMO.

⚠️ **Boolean field mapping gotcha:** The CSV `opened`/`clicked`/`unsubscribed` columns are 0/1 (Number).
Map to Number custom fields (`OpenedCount__c`, `ClickedCount__c`, `UnsubscribedCount__c`) on the standard DMO,
NOT to any Boolean fields. Mapping Number DLO → Boolean DMO causes materialization failure. See GOTCHAS.

**Relationships (all N:1 / ManyToOne):**
1. `ssot__WebsiteEngagement__dlm.ssot__IndividualId__c → ssot__Individual__dlm.ssot__Id__c`
2. `ssot__EmailEngagement__dlm.ssot__IndividualId__c → ssot__Individual__dlm.ssot__Id__c`
3. `InsurancePolicy__dlm.PartyId__c → ssot__Individual__dlm.ssot__Id__c`
4. `InsuranceClaim__dlm.PolicyId__c → InsurancePolicy__dlm.Id__c`
5. `InsuranceClaim__dlm.PartyId__c → ssot__Individual__dlm.ssot__Id__c`

**Calculated Insights (5):**

All use the mandatory unified join pattern:
```sql
FROM UnifiedssotIndividualRt__dlm
JOIN UnifiedLinkssotIndividualRt__dlm
    ON UnifiedssotIndividualRt__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c
JOIN <DMO>
    ON <DMO>.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c
GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c
```

| CI | Measures | Demo use |
|----|---------|---------|
| `<Slug>_PolicySummary__cio` | policy_count__c, active_policy_count__c, total_annual_premium__c, avg_annual_premium__c, total_coverage_amount__c | Segment by premium tier, coverage level |
| `<Slug>_ClaimsSummary__cio` | claims_count__c, open_claims_count__c, total_claimed_amount__c, avg_claim_amount__c | Identify high-risk clients |
| `<Slug>_EngagementScore__cio` | emails_received__c (COUNT), emails_opened__c (SUM OpenedCount__c), emails_clicked__c (SUM ClickedCount__c) | Find dormant / unreachable clients |
| `<Slug>_CustomerRiskProfile__cio` | churn_score__c (from ssot__Individual__dlm direct), ltv__c, nps_score__c, policy_count__c, active_policy_count__c, total_annual_premium__c | THE retention CI — combines risk + value + policies |
| `<Slug>_PolicyTypeBreakdown__cio` | life_policy_count__c, health_policy_count__c, property_policy_count__c, vehicle_policy_count__c, pension_policy_count__c | Cross-sell: identify missing product categories |

CustomerRiskProfile reads enrichment fields from ssot__Individual__dlm directly (no IndividualProfile join):
```sql
SELECT
    UnifiedssotIndividualRt__dlm.ssot__Id__c AS unified_individual__c,
    MAX(ssot__Individual__dlm.ChurnScore__c) AS churn_score__c,
    MAX(ssot__Individual__dlm.Ltv__c) AS ltv__c,
    MAX(ssot__Individual__dlm.NpsScore__c) AS nps_score__c,
    COUNT(InsurancePolicy__dlm.Id__c) AS policy_count__c,
    SUM(CASE WHEN InsurancePolicy__dlm.Status__c = 'Active' THEN 1 ELSE 0 END) AS active_policy_count__c,
    SUM(InsurancePolicy__dlm.PremiumAnnual__c) AS total_annual_premium__c
FROM UnifiedssotIndividualRt__dlm
JOIN UnifiedLinkssotIndividualRt__dlm
    ON UnifiedssotIndividualRt__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.UnifiedRecordId__c
JOIN ssot__Individual__dlm
    ON ssot__Individual__dlm.ssot__Id__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c
JOIN InsurancePolicy__dlm
    ON InsurancePolicy__dlm.PartyId__c = UnifiedLinkssotIndividualRt__dlm.SourceRecordId__c
GROUP BY UnifiedssotIndividualRt__dlm.ssot__Id__c
```

Enrichment fields (ChurnScore__c, Ltv__c, NpsScore__c) are custom fields on ssot__Individual__dlm — no IndividualProfile join needed.

**Segments (5 — not published):**

| Segment | Include | Exclude | Expected members (N=10K) |
|---------|---------|---------|--------------------------|
| `<Slug>_ActivePolicyUpsell` | PolicySummary.active_policy_count ≥ 1 AND ≤ 3 | PolicySummary.active_policy_count ≥ 4 | ~4,000 |
| `<Slug>_ChurnRiskRetention` | CustomerRiskProfile.churn_score ≥ 50 AND active_policy_count ≥ 1 | CustomerRiskProfile.total_annual_premium ≥ 50,000 | ~2,000 |
| `<Slug>_GoldTierReengagement` | Individual.loyalty_tier IN [Gold, Platinum] AND EngagementScore.emails_received ≥ 2 | EngagementScore.emails_opened ≥ 2 | ~500 |
| `<Slug>_PremiumRenewalTargets` | CustomerRiskProfile.total_annual_premium ≥ 5,000 AND CustomerRiskProfile.nps_score ≥ 7 | CustomerRiskProfile.churn_score ≥ 70 | ~1,500 |
| `<Slug>_DormantHighValue` | CustomerRiskProfile.ltv ≥ 20,000 AND PolicySummary.active_policy_count ≥ 2 AND EngagementScore.emails_received ≥ 4 | EngagementScore.emails_opened ≥ 1 AND ClaimsSummary.open_claims_count ≥ 3 | ~700 |

---

### BANKING (B2C)

Industry key: `"banking"` — detected from bank name keywords.

**Streams:**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, birth_date, gender, city, country, customer_since, loyalty_tier, ltv, churn_score, nps_score, source, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email, is_primary | N |
| `<Slug>_Financial_Accounts` | **Other** | `account_id` | contact_id→Contacts, account_type (Checking/Savings/Mortgage/Credit/Investment), balance, credit_limit, interest_rate, opened_date (Date), status | N×1.5 |
| `<Slug>_Transactions` | **Engagement** | `tx_id` | account_id→Accounts, contact_id→Contacts, **tx_datetime (DateTime REQUIRED, max 2y)**, category, amount, channel (App/Web/Branch/ATM) | N×20 |
| `<Slug>_Banking_Products` | **Other** | `product_id` | contact_id→Contacts, product_type (Credit Card/Personal Loan/Mortgage/Auto Loan/Line of Credit), product_name, amount, interest_rate, status (Active/Closed/Pending), opened_date (Date) | N×1–3 |
| `<Slug>_Loyalty_Transactions` | **Engagement** | `tx_id` | contact_id→Contacts, **event_datetime (DateTime REQUIRED)**, type (earn/redeem), points, reference, balance | N×variable |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, email, campaign_name, **sent_date (DateTime)**, opened, clicked, unsubscribed | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `FinancialAccount__dlm`, `Transaction__dlm` (ENGAGEMENT), `BankingProduct__dlm`, `LoyaltyTransaction__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**CIs (5):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_AccountSummary__cio` | account_count__c, active_account_count__c, total_balance__c, avg_balance__c | Wealth segmentation |
| `<Slug>_ProductHoldings__cio` | checking_count__c, savings_count__c, investment_count__c, credit_count__c, mortgage_count__c | Cross-sell: no mortgage AND balance > 50K |
| `<Slug>_SpendingProfile__cio` | transaction_count__c, total_spend__c, avg_transaction__c, groceries_spend__c, dining_spend__c | Behavioral + channel segmentation |
| `<Slug>_CustomerRiskProfile__cio` | churn_score__c, ltv__c, nps_score__c, total_balance__c, account_count__c | Master retention CI — combines risk with balance |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_MortgageUpsell` | AccountSummary.total_balance ≥ 50,000 AND ProductHoldings.mortgage_count = 0 | High-balance customers with no mortgage |
| `<Slug>_InvestmentTargets` | ProductHoldings.savings_count ≥ 1 AND total_balance ≥ 30,000 AND investment_count = 0 | Savings holders ready for wealth management |
| `<Slug>_DigitalMigration` | SpendingProfile.transaction_count ≥ 10 AND CustomerRiskProfile.nps_score ≥ 7 | Active, satisfied — migrate from branch to digital |
| `<Slug>_AtRiskClients` | CustomerRiskProfile.churn_score ≥ 65 AND AccountSummary.account_count ≥ 1 | Retention — personalised offer or RM call |
| `<Slug>_PremiumUpgrade` | AccountSummary.total_balance ≥ 100,000 AND ProductHoldings.checking_count ≥ 1 AND CustomerRiskProfile.nps_score ≥ 8 | High-wealth satisfied clients — private banking tier |

---

### RETAIL B2C (e.g. Castro, Factory54, Renuar — fashion)

Industry key: `"retail"` — detected from fashion/apparel keywords.

**Streams:**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | standard + preferred_store, size_preference, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Sales_Orders` | **Engagement** | `order_id` | contact_id→Contacts, **order_datetime (DateTime REQUIRED)**, channel (Web/Store/Mobile/App), total_amount, status (including returns) | N×3 |
| `<Slug>_Order_Lines` | **Other** | `line_id` | order_id→Orders, contact_id→Contacts, product_sku, product_name, category (Apparel/Footwear/Accessories/Bags/Sportswear), qty, unit_price, line_total, is_promotional | orders×3.5 |
| `<Slug>_Loyalty_Transactions` | **Engagement** | `tx_id` | contact_id→Contacts, **event_datetime (DateTime REQUIRED)**, type (earn/redeem), points, reference, balance | N×variable |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `SalesOrder__dlm` (ENGAGEMENT), `OrderLine__dlm`, `LoyaltyTransaction__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**CIs (5):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_PurchaseSummary__cio` | order_count__c, total_spend__c, avg_order_value__c, returned_order_count__c | High returners: returned_order_count ≥ 2 |
| `<Slug>_CategoryAffinity__cio` | apparel_spend__c, footwear_spend__c, accessories_spend__c, bags_spend__c, sportswear_spend__c | Category cross-sell: high apparel, zero bags |
| `<Slug>_ChannelProfile__cio` | web_orders__c, store_orders__c, mobile_orders__c, app_orders__c | Online-to-store migration, mobile-first |
| `<Slug>_CustomerValue__cio` | churn_score__c, ltv__c, nps_score__c | Master retention CI |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_VIPReactivation` | CustomerValue.ltv ≥ 500 AND churn_score ≥ 50 | High-LTV drifting — personalised VIP retention offer |
| `<Slug>_CategoryExpansion` | CategoryAffinity.apparel_spend ≥ 200 AND bags_spend = 0 | Apparel buyers who never bought bags — styled outfit recommendation |
| `<Slug>_OnlineToStore` | ChannelProfile.web_orders ≥ 3 AND store_orders = 0 | Web-only buyers — invite to in-store experience |
| `<Slug>_HighReturnRate` | PurchaseSummary.returned_order_count ≥ 2 | High returners — fit consultation, sizing guide |
| `<Slug>_FrequentMobileShoppers` | ChannelProfile.mobile_orders ≥ 3 AND CustomerValue.ltv ≥ 200 | Mobile-native high-value — app-exclusive offers |

---

### FOOD B2C (e.g. Shufersal, Rami Levy, Carrefour — supermarkets)

Industry key: `"food"` — detected from supermarket/retailer keywords (shufersal, rami levy, carrefour, victory…).

**Streams:**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | standard + dietary_preference, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Purchase_Orders` | **Engagement** | `order_id` | contact_id→Contacts, store_name, store_type, **order_datetime (DateTime REQUIRED)**, total_amount, channel (Online/InStore/App), loyalty_points_earned | N×4 |
| `<Slug>_Order_Lines` | **Other** | `line_id` | order_id→Orders, contact_id→Contacts, product_sku, product_name, category (Dairy/Meat/Bakery/Produce/Beverages/Snacks), quantity, unit_price, line_total, is_promotional | orders×3 |
| `<Slug>_Loyalty_Transactions` | **Engagement** | `txn_id` | contact_id→Contacts, **event_datetime (DateTime REQUIRED, max 2y)**, txn_type (earn/redeem), points_earned, points_redeemed, current_balance | N×6 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `PurchaseOrder__dlm` (ENGAGEMENT), `OrderLine__dlm`, `LoyaltyTransaction__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**CIs (5):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_PurchaseSummary__cio` | order_count__c, total_spend__c, avg_basket__c, total_points_earned__c | Frequent buyers: order_count ≥ 8 |
| `<Slug>_CategorySpend__cio` | dairy_spend__c, meat_spend__c, bakery_spend__c, produce_spend__c, beverages_spend__c, snacks_spend__c | Dairy loyalists: dairy_spend ≥ 200 · Cross-category upsell |
| `<Slug>_LoyaltyProfile__cio` | total_earned__c, total_redeemed__c, current_points_balance__c | Dormant points: balance ≥ 200 AND redeemed = 0 |
| `<Slug>_CustomerValue__cio` | churn_score__c, ltv__c, nps_score__c (from ssot__Individual__dlm) | Master retention CI |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email dormancy |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_LapsedHighSpenders` | PurchaseSummary.order_count ≥ 5 AND CustomerValue.churn_score ≥ 50 | Heavy buyers drifting — win-back with personalised offer |
| `<Slug>_DairyLoyalists` | CategorySpend.dairy_spend ≥ 200 | Heavy dairy buyers — dairy promo, new product launch |
| `<Slug>_UnactivatedLoyalty` | LoyaltyProfile.current_points_balance ≥ 200 AND total_redeemed = 0 | Activate dormant points to drive next purchase |
| `<Slug>_FrequencyBuyers` | PurchaseSummary.order_count ≥ 8 | Most active shoppers — loyalty tier upgrade |
| `<Slug>_DormantReactivation` | PurchaseSummary.order_count ≥ 2 AND CustomerValue.churn_score ≥ 60 | Had history but high risk — time-limited reactivation offer |

---

### PHARMA (B2C) — e.g. SuperPharm, GoodPharm

Industry key: `"pharma"` — detected from pharmacy/drug company keywords.

**Streams:**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | standard + age_group, chronic_condition_flag, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Prescriptions` | **Engagement** | `rx_id` | contact_id→Contacts, drug_name, therapeutic_area (Cardiovascular/Diabetes/Respiratory/Pain Relief/Psychiatry/Gastroenterology), diagnosis, **fill_datetime (DateTime REQUIRED)**, status (Active/Discontinued/Expired) | N×3 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `Prescription__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**CIs (5):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_PrescriptionSummary__cio` | rx_count__c, active_rx_count__c, discontinued_rx_count__c, expired_rx_count__c | Lapsed patients: rx_count ≥ 2 AND active = 0 |
| `<Slug>_TherapeuticProfile__cio` | cardiovascular_rx__c, diabetes_rx__c, respiratory_rx__c, pain_rx__c, psychiatry_rx__c, gastro_rx__c | Condition-specific campaigns |
| `<Slug>_AdherenceProfile__cio` | adherence_rate__c, active_rx_count__c, total_rx__c | Low adherence: adherence_rate ≤ 0.5 AND active_rx ≥ 1 |
| `<Slug>_CustomerHealthValue__cio` | churn_score__c, ltv__c, nps_score__c, rx_count__c, active_rx_count__c | Master patient value CI |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_AdherenceRisk` | AdherenceProfile.adherence_rate ≤ 0.5 AND active_rx_count ≥ 1 | Refill reminders, adherence support programme |
| `<Slug>_PolyPharmacy` | PrescriptionSummary.active_rx_count ≥ 3 | Complex patients — care coordination, refill sync |
| `<Slug>_CardiovascularCare` | TherapeuticProfile.cardiovascular_rx ≥ 1 AND CustomerHealthValue.churn_score ≥ 50 | Cardiovascular at-risk — specialist care coordination |
| `<Slug>_LapsedPatients` | PrescriptionSummary.rx_count ≥ 2 AND active_rx_count = 0 | Had prescriptions but none active — re-engagement |
| `<Slug>_DiabeticEngagement` | TherapeuticProfile.diabetes_rx ≥ 1 AND EngagementScore.emails_received ≥ 2 AND emails_opened = 0 | Diabetic patients not engaging — wellness programme outreach |

---

### TELCO (B2C) — e.g. Orange, Partner, Hot, Pelephone

Industry key: `"telco"` — detected from telco keywords (orange, partner, hot, pelephone, bezeq…).

**Streams:**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | standard + plan_tier, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Service_Contracts` | **Other** | `contract_id` | contact_id→Contacts, plan_name, plan_type (Mobile/Broadband/TV/Bundle), monthly_fee, start_date (Date), end_date (Date, nullable), status, data_allowance_gb | N×1.3 |
| `<Slug>_Usage_Records` | **Other** | `usage_id` | contract_id→Contracts, contact_id→Contacts, usage_date (Date, YYYY-MM-01 — first day of month, enables native range filtering), data_used_gb, voice_minutes_used, sms_count, overage_charge | contracts×12 months |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `ServiceContract__dlm`, `UsageRecord__dlm`, `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**CIs (5):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_ServiceSummary__cio` | contract_count__c, active_contract_count__c, total_monthly_fee__c, mobile_count__c, broadband_count__c, tv_count__c, bundle_count__c | Bundle upsell: mobile AND no broadband |
| `<Slug>_UsageProfile__cio` | avg_data_used_gb__c, avg_voice_minutes__c, avg_sms_count__c, total_overage_charge__c | Overage payers → upgrade campaign |
| `<Slug>_ChurnRisk__cio` | churn_score__c, nps_score__c, ltv__c, active_contract_count__c, total_monthly_fee__c | Master retention CI |
| `<Slug>_ProductBundle__cio` | has_mobile__c, has_broadband__c, has_tv__c, bundle_count__c | Bundle completeness scoring |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_ChurnRisk` | ChurnRisk.churn_score ≥ 60 AND ServiceSummary.active_contract_count ≥ 1 | Proactive retention — service call before expiry |
| `<Slug>_BundleUpsell` | ServiceSummary.mobile_count ≥ 1 AND broadband_count = 0 | Mobile-only → bundle offer with discount |
| `<Slug>_OverageAlert` | UsageProfile.total_overage_charge ≥ 20 | Regularly paying overage → upgrade to higher plan |
| `<Slug>_DataHeavyUsers` | UsageProfile.avg_data_used_gb ≥ 15 | Power data users → premium data plan offer |
| `<Slug>_ContractRenewal` | ServiceSummary.active_contract_count ≥ 1 AND ChurnRisk.churn_score ≤ 40 | Happy customers → proactive renewal with loyalty reward |

---

### FOOD B2B (e.g. Tnuva, Strauss — manufacturer selling to stores)

Industry key: `"food_b2b"` — always B2B, auto-detected from name.

**Story:** *"Our sales team manages store accounts across the country. Data Cloud gives each rep a
360° view — which stores are dormant, which categories have SKU gaps, which accounts are at risk of
switching to a competitor."*

**Model:** 1:1 contact = store buyer (one Individual represents one store account).
Contacts CSV is shared with the standard pipeline. Transactional data links to `contact_id`.

**Streams (N = number of store contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, loyalty_tier, churn_score, ltv, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range, number_of_employees, annual_revenue | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Wholesale_Orders` | **Engagement** | `order_id` | contact_id→Contacts, **order_datetime (DateTime REQUIRED)**, total_amount, item_count, status (Pending/Processing/Delivered/Cancelled), payment_terms (NET30/NET60/NET45/COD), sales_rep | N×(4–10)/year |
| `<Slug>_Wholesale_Order_Lines` | **Other** | `line_id` | order_id→Orders, contact_id→Contacts, product_sku, product_name, category (Dairy/Bakery/Meat/Produce/Snacks), quantity, unit_price, line_total, is_promotional | orders×4 |
| `<Slug>_Loyalty_Transactions` | **Engagement** | `tx_id` | contact_id→Contacts, **event_datetime (DateTime REQUIRED)**, type (earn/redeem), points, reference, balance | N×variable |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `WholesaleOrder__dlm` (ENGAGEMENT), `WholesaleOrderLine__dlm`, `LoyaltyTransaction__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**Relationships (B2B Account model — parent = `ssot__Account__dlm`):**
- `WholesaleOrder → Account` (PartyId__c → ssot__Id__c)
- `WholesaleOrderLine → WholesaleOrder` (OrderId__c → Id__c)
- `WholesaleOrderLine → Account` (PartyId__c → ssot__Id__c)
- `LoyaltyTransaction → Account` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Account` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Account` (ssot__IndividualId__c → ssot__Id__c)

**CIs (5) — use B2B Account join pattern (`unified_account__c` dimension):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_WholesaleSummary__cio` | order_count__c, total_revenue__c, avg_order_value__c, delivered_order_count__c | Dormancy detection, revenue tiers |
| `<Slug>_CategoryPenetration__cio` | dairy_spend__c, bakery_spend__c, meat_spend__c, produce_spend__c, snacks_spend__c, promo_item_count__c | SKU gap analysis, cross-category upsell |
| `<Slug>_AccountHealth__cio` | churn_score__c, ltv__c, nps_score__c, order_count__c, total_revenue__c | Master retention CI — combines churn risk with revenue |
| `<Slug>_OrderFrequency__cio` | order_count__c, delivered_count__c, cancelled_count__c, total_items__c | High-frequency vs dormant accounts |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email channel effectiveness per account |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_DormantAccounts` | WholesaleSummary.order_count ≥ 3 AND AccountHealth.churn_score ≥ 60 | Stores were active but ordering less — rep intervention |
| `<Slug>_UpsellCandidates` | CategoryPenetration.dairy_spend ≥ 500 AND snacks_spend = 0 | Dairy buyers with zero snack purchases — category expansion |
| `<Slug>_HighFrequencyAccounts` | WholesaleSummary.order_count ≥ 12 | Key accounts — premium service tier, dedicated rep |
| `<Slug>_PromoSensitiveStores` | CategoryPenetration.promo_item_count ≥ 5 | Promo-driven buyers — target with seasonal bundles |
| `<Slug>_AtRiskRenewals` | AccountHealth.churn_score ≥ 55 AND WholesaleSummary.delivered_order_count ≥ 2 | Active but showing risk — proactive outreach before renewal |

---

### HIGHTECH B2B — SaaS (e.g. Hibob, Cellebrite, Taboola)

Industry key: `"hightech"` — always B2B, auto-detected from name.

**Story:** *"Our CS team of 30 manages 1,500 SaaS accounts. Data Cloud surfaces which accounts have
low product adoption (churn risk), which are ready to expand seats, and which have open critical
tickets that need urgent attention."*

**Model:** 1:1 contact = account representative (one Individual per company).
Contacts CSV is shared. All transactional objects link via `contact_id = PartyId__c`.

**Streams (N = number of account contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, nps_score, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range, number_of_employees, annual_revenue | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Ht_Subscriptions` | **Other** | `sub_id` | contact_id→Contacts, product_name (Platform Starter/Professional/Enterprise/Analytics Add-on/API Access Pro), plan_tier, status (Active/Trial/Churned/Suspended), seats, mrr, start_date (Date), renewal_date (Date), days_until_renewal | N×1–2 |
| `<Slug>_Ht_Usage_Records` | **Other** | `usage_id` | sub_id→Subscriptions, contact_id, usage_date (Date, YYYY-MM-01 — first day of month for native range filtering), active_users, login_count, feature_adoption_score (0–100), data_volume_gb | Active subs × 12 months |
| `<Slug>_Ht_Support_Tickets` | **Other** | `ticket_id` | contact_id, created_date (Date), **days_since_opened (Number — pre-computed integer)**, status (Open/In Progress/Resolved/Closed), severity (Low/Medium/High/Critical), category (Bug/Feature Request/Billing/Onboarding/Performance), resolution_days, csat_score (1–5) | N×0–3 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id→Contacts, session_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `HtSubscription__dlm`, `HtUsageRecord__dlm`, `HtSupportTicket__dlm`, `ssot__EmailEngagement__dlm` (ENGAGEMENT, standard+extended), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT, standard+extended)

**Relationships (B2B Account model — parent = `ssot__Account__dlm`):**
- `HtSubscription → Account` (PartyId__c → ssot__Id__c)
- `HtUsageRecord → HtSubscription` (SubscriptionId__c → Id__c)
- `HtUsageRecord → Account` (PartyId__c → ssot__Id__c)
- `HtSupportTicket → Account` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Account` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Account` (ssot__IndividualId__c → ssot__Id__c)

**CIs (5) — use B2B Account join pattern (`unified_account__c` dimension):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_SubscriptionSummary__cio` | active_sub_count__c, total_mrr__c, total_seats__c, renewal_within_90_days__c | Renewal pipeline, expansion revenue targets |
| `<Slug>_UsageHealthScore__cio` | avg_active_users__c, avg_login_count__c, avg_feature_adoption_score__c, total_data_volume_gb__c | Health scoring — low adoption = churn risk |
| `<Slug>_SupportProfile__cio` | total_tickets__c, open_ticket_count__c, critical_ticket_count__c, avg_csat_score__c, avg_resolution_days__c, **recent_ticket_count__c** (tickets opened in last 60 days) | Support burden accounts, CSAT-based prioritization; `recent_ticket_count = 0` → no ticket in last 2 months segment |
| `<Slug>_AccountHealthProfile__cio` | churn_score__c, nps_score__c, ltv__c, active_sub_count__c, total_mrr__c | Master account health CI — combines risk + revenue |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement per account contact |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_ChurnRisk90` | SubscriptionSummary.renewal_within_90_days ≥ 1 AND AccountHealthProfile.churn_score ≥ 55 | Renewal due soon + high churn risk — CS intervention NOW |
| `<Slug>_ExpansionCandidates` | UsageHealthScore.avg_feature_adoption ≥ 70 AND SubscriptionSummary.active_sub_count ≥ 1 AND total_mrr ≤ 5,000 | High adoption, room to grow — seat expansion or tier upgrade |
| `<Slug>_LowAdoptionIntervention` | UsageHealthScore.avg_login_count ≤ 5 AND SubscriptionSummary.active_sub_count ≥ 1 | Paying but not using — onboarding check-in, training offer |
| `<Slug>_SupportBurdenAccounts` | SupportProfile.open_ticket_count ≥ 2 AND critical_ticket_count ≥ 1 | Active critical tickets — urgent CS escalation |
| `<Slug>_NoRecentTicket` | SupportProfile.recent_ticket_count = 0 AND SubscriptionSummary.active_sub_count ≥ 1 | No ticket opened in last 60 days — proactive health check outreach |
| `<Slug>_ChampionProgram` | AccountHealthProfile.nps_score ≥ 9 AND UsageHealthScore.avg_feature_adoption ≥ 75 | Highly satisfied power users — reference programme, advisory board |

---

### UTILITIES (B2C) — e.g. Israel Electric, Cellcom Infra, Bezeq

Industry key: `"utilities"` — always B2C.

**Story:** *"We supply electricity, gas, or water to hundreds of thousands of households. Data Cloud
lets us identify customers at risk of churn (switching provider), detect overage patterns before they
complain, and personalise smart-meter and tariff upgrade campaigns."*

**Model:** B2C — one Individual per household account. All objects link via `contact_id = PartyId__c`.
No loyalty programme. Contracts are long-lived mutable records (OTHER); monthly meter readings
are also OTHER (they change/accumulate but have no hard P2Y lookback risk).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, nps_score, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Utility_Contracts` | **Other** | `contract_id` | contact_id→Contacts, plan_name (Basic/Standard/Premium/Green Tariff/Smart Meter Plan), fuel_type (Electricity/Gas/Water), status (Active/Suspended/Terminated/Pending), start_date (Date), monthly_rate, consumption_avg_kwh | N×1–2 |
| `<Slug>_Consumption_Records` | **Other** | `record_id` | contract_id→UtilityContract, contact_id, reading_month (YYYY-MM), kwh_consumed, kwh_baseline, overage_kwh, overage_flag (0/1), billing_amount | Contracts × 12 months |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `UtilityContract__dlm` (OTHER), `ConsumptionRecord__dlm` (OTHER), `ssot__EmailEngagement__dlm` (ENGAGEMENT), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT)

**Relationships:**
- `UtilityContract → Individual` (PartyId__c → ssot__Id__c)
- `ConsumptionRecord → UtilityContract` (ContractId__c → Id__c)
- `ConsumptionRecord → Individual` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)

**CIs (3):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_ConsumptionProfile__cio` | total_contracts__c, active_contracts__c, total_kwh__c, avg_monthly_kwh__c, overage_months__c, max_overage__c | Usage profiling — smart meter upsell, tariff optimisation |
| `<Slug>_CustomerRiskProfile__cio` | suspended_contracts__c, churn_score__c, days_since_last_payment__c, overage_months__c, max_overage__c | At-risk: churn_score ≥ 65 · Payment issues: suspended_contracts ≥ 1 · High overage: overage_months ≥ 4 AND max_overage > 30 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c, web_sessions__c, web_pages_viewed__c | Email + web engagement for campaign targeting |

**Segments (examples):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_ChurnRisk` | CustomerRiskProfile.churn_score ≥ 65 | High churn risk — personalised retention offer |
| `<Slug>_OverageAlerts` | ConsumptionProfile.overage_months ≥ 4 AND max_overage > 30 | Persistent overage — smart meter / upgrade campaign |
| `<Slug>_PaymentIssues` | CustomerRiskProfile.suspended_contracts ≥ 1 | Payment suspended — win-back or payment plan outreach |
| `<Slug>_GreenTariffCandidates` | ConsumptionProfile.avg_monthly_kwh ≥ 300 AND CustomerRiskProfile.churn_score < 50 | High-usage loyal customers — green tariff upsell |

---

### AIRLINES (B2C) — e.g. El Al, Arkia, Israir

Industry key: `"airlines"` — always B2C.

**Story:** *"We have millions of FFP members. Data Cloud helps us identify dormant members with
expiring miles, reward our highest-value business travellers, and re-engage lapsed flyers with
targeted upgrade and destination campaigns."*

**Model:** B2C — one Individual per FFP member. FlightBooking is ENGAGEMENT (immutable booking events
with DateTime, subject to P2Y lookback). LoyaltyTransaction is ENGAGEMENT (FFP miles earn/redeem).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, nps_score, loyalty_points_balance, points_earned_ytd, points_redeemed_ytd, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Flight_Bookings` | **Engagement** | `booking_id` | contact_id→Contacts, **booking_datetime (DateTime REQUIRED)**, route (e.g. TLV-LHR), origin, destination, cabin_class (Economy/Business/First/Premium Economy), status (Confirmed/Completed/Cancelled/No-Show), base_fare, taxes, total_fare, miles_earned, flight_date (Date) | N×1–8 |
| `<Slug>_Loyalty_Transactions` | **Engagement** | `tx_id` | contact_id→Contacts, **event_datetime (DateTime REQUIRED)**, tx_type (Earn/Redeem/Expire/Bonus), miles_delta, balance_after, description | N×2–15 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `FlightBooking__dlm` (ENGAGEMENT), `LoyaltyTransaction__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT)

**Relationships:**
- `FlightBooking → Individual` (PartyId__c → ssot__Id__c)
- `LoyaltyTransaction → Individual` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_FlightProfile__cio` | total_flights__c, completed_flights__c, total_spend__c, avg_fare__c, premium_flights__c, total_miles_earned__c | Business travelers: premium_flights ≥ 3 · High LTV: total_spend > 3000 · Frequent flyers: completed_flights ≥ 8 |
| `<Slug>_LoyaltyProfile__cio` | total_earned__c, total_redeemed__c, current_balance__c, earn_events__c, redeem_events__c | Miles expiry re-engagement · High-balance premium targeting |
| `<Slug>_CustomerRiskProfile__cio` | total_bookings__c, cancelled_bookings__c, churn_score__c, miles_balance__c, days_since_last_flight__c | Dormant with miles: days_since_last_flight > 180 AND miles_balance > 5000 · High churn risk: churn_score ≥ 70 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c, web_sessions__c, web_pages_viewed__c | Email + web engagement for campaign targeting |

**Segments (examples):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_DormantWithMiles` | CustomerRiskProfile.days_since_last_flight > 180 AND miles_balance > 5000 | Dormant FFP members with expiring miles — re-engagement + miles expiry alert |
| `<Slug>_BusinessTravelers` | FlightProfile.premium_flights ≥ 3 AND total_spend > 3000 | High-value business travellers — lounge access / upgrade offer |
| `<Slug>_FrequentFlyers` | FlightProfile.completed_flights ≥ 8 | Frequent flyers — status tier upgrade campaign |
| `<Slug>_ChurnRiskFlyers` | CustomerRiskProfile.churn_score ≥ 70 AND total_bookings ≥ 2 | Likely to switch — flash sale or bonus miles win-back |

---

### Healthcare

Industry key: `"healthcare"` — always B2C.

**Story:** *"We have millions of HMO members. Data Cloud helps us identify members who haven't had a preventive check-up, flag those with abnormal lab results for follow-up, and proactively retain members at risk of switching to a competing HMO."*

**Model:** B2C — one Individual per HMO member. MedicalVisit and LabResult are OTHER (mutable records, no P2Y lookback).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, nps_score, days_since_last_purchase, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Medical_Visits` | **Other** | `visit_id` | contact_id, visit_date (Date), specialty, visit_type (Clinic/Telemedicine/Emergency/Follow-up), copay_amount, diagnosis_code | N×2–12 |
| `<Slug>_Lab_Results` | **Other** | `result_id` | contact_id, test_date (Date), test_type, result_status (Normal/Borderline/Abnormal/Critical), is_abnormal (0/1) | N×1–6 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `MedicalVisit__dlm` (OTHER), `LabResult__dlm` (OTHER), `ssot__EmailEngagement__dlm` (ENGAGEMENT), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT)

**Relationships:**
- `MedicalVisit → Individual` (PartyId__c → ssot__Id__c)
- `LabResult → Individual` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)

**CIs (3):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_VisitProfile__cio` | total_visits__c, er_visits__c, telemedicine_visits__c, avg_copay__c, total_copay__c, days_since_last_visit__c | High utilisation: total_visits ≥ 8 · Preventive care gap: days_since_last_visit ≥ 365 · ER frequent: er_visits ≥ 2 |
| `<Slug>_HealthRiskProfile__cio` | total_tests__c, abnormal_results__c, churn_score__c, days_since_last_visit__c | Abnormal results ≥ 1 · Renewal at risk: churn_score ≥ 60 · Long gap: days_since_last_visit ≥ 365 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c, web_sessions__c, web_pages_viewed__c | Email + web engagement for campaign targeting |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_HighUtilization` | VisitProfile.total_visits ≥ 8 (excl. churn_score ≥ 90) | Proactively manage high-utilisation members and reduce ER dependency |
| `<Slug>_PreventiveCareGap` | VisitProfile.days_since_last_visit ≥ 365 | Remind members about routine check-ups and preventive screenings |
| `<Slug>_ERFrequent` | VisitProfile.er_visits ≥ 2 | Connect with a primary care doctor to reduce avoidable ER usage |
| `<Slug>_AbnormalResults` | HealthRiskProfile.abnormal_results ≥ 1 | Trigger follow-up appointment and specialist referral workflow |
| `<Slug>_RenewalAtRisk` | HealthRiskProfile.churn_score ≥ 60 (excl. ≥ 90) | Proactive retention with personalised health benefits |

---

### Sports Club

Industry key: `"sports_club"` — always B2C.

**Story:** *"We have hundreds of thousands of gym/sports club members. Data Cloud helps us re-engage dormant members before they cancel, identify upgrade candidates, and lock in renewals before the contract expires."*

**Model:** B2C — one Individual per club member. Membership is OTHER (contractual, mutable). ActivityRecord is ENGAGEMENT (immutable gym visit events with DateTime, subject to P2Y lookback).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, nps_score, days_since_last_purchase, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Memberships` | **Other** | `membership_id` | contact_id, plan_type (Basic/Standard/Premium/VIP/Family), monthly_fee, start_date (Date), renewal_date (Date), renewing_soon (0/1), membership_age_months, status (Active/Suspended/Cancelled), tier (Bronze/Silver/Gold/Platinum) | N×1 |
| `<Slug>_Activity_Records` | **Engagement** | `activity_id` | contact_id, **activity_date (DateTime REQUIRED)**, activity_type (Gym Floor/Group Class/Swimming/…), duration_minutes, location, calories_burned | N×0–100 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `Membership__dlm` (OTHER), `ActivityRecord__dlm` (ENGAGEMENT), `ssot__EmailEngagement__dlm` (ENGAGEMENT), `ssot__WebsiteEngagement__dlm` (ENGAGEMENT)

**Relationships:**
- `Membership → Individual` (PartyId__c → ssot__Id__c)
- `ActivityRecord → Individual` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_MembershipProfile__cio` | total_memberships__c, active_memberships__c, monthly_fee__c, membership_age_months__c, renewal_within_90_days__c | New members: age ≤ 3 months · Renewal risk: renewing_soon ≥ 1 · Budget plan: monthly_fee ≤ 30 |
| `<Slug>_ActivityProfile__cio` | total_sessions__c, total_minutes__c, avg_session_minutes__c, days_since_last_activity__c | Dormant: days_since_last_activity ≥ 60 · High activity: total_sessions ≥ 30 · Upgrade ready: sessions ≥ 20 AND fee ≤ 30 |
| `<Slug>_CustomerRiskProfile__cio` | churn_score__c, days_since_last_activity__c, renewal_within_90_days__c | Churn risk: churn_score ≥ 60 · Renewal + churn: renewing_soon ≥ 1 AND churn_score ≥ 50 · Long inactive: days_since_last_activity ≥ 90 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c, web_sessions__c, web_pages_viewed__c | Email + web engagement for campaign targeting |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_DormantMembers` | ActivityProfile.days_since_last_activity ≥ 60 (excl. churn_score ≥ 90) | Re-engage with a free PT session or class trial before they cancel |
| `<Slug>_RenewalRisk` | MembershipProfile.renewal_within_90_days ≥ 1 AND CustomerRiskProfile.churn_score ≥ 50 | Priority outreach to lock in renewal with a loyalty discount |
| `<Slug>_HighActivity` | ActivityProfile.total_sessions ≥ 30 (excl. churn_score ≥ 60) | Premium plan upgrade, locker room access, or brand ambassador programme |
| `<Slug>_PremiumUpgrade` | ActivityProfile.total_sessions ≥ 20 AND MembershipProfile.monthly_fee ≤ 30 | High-frequency budget-plan members — strong upsell signal |
| `<Slug>_NewMembers` | MembershipProfile.membership_age_months ≤ 3 | Onboarding campaign: welcome pack, free class, facility tour |

---

### Ecommerce

Industry key: `"ecommerce"` — always B2C.

**Story:** *"We run an online marketplace with millions of shoppers. Data Cloud helps us re-engage cart abandoners, reward high-LTV customers, win back dormant shoppers, and identify churn risk before it happens."*

**Model:** B2C — one Individual per shopper. EcomOrder is ENGAGEMENT (immutable purchase event). EcomOrderLine is OTHER (mutable line-item detail). CartAbandonment is ENGAGEMENT (immutable behavioural event).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, days_since_last_purchase, income_range | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Ecom_Orders` | **Engagement** | `order_id` | contact_id, **order_datetime (DateTime REQUIRED)**, total_amount, item_count, channel (web/mobile_app/mobile_web), payment_method, delivery_type, status (Completed/Returned/Cancelled) | N×1–8 avg N×4 |
| `<Slug>_Ecom_Order_Lines` | **Other** | `line_id` | order_id→Orders, contact_id, product_sku, product_name, category, quantity, unit_price, line_total | varies |
| `<Slug>_Cart_Abandonments` | **Engagement** | `abandonment_id` | contact_id, **abandonment_datetime (DateTime REQUIRED)**, product_count, cart_value, device_type, session_id | ~40% of N × 1–3 avg |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `EcomOrder__dlm` (ENGAGEMENT), `EcomOrderLine__dlm` (OTHER), `CartAbandonment__dlm` (ENGAGEMENT)

**Relationships:**
- `EcomOrder → Individual` (PartyId__c → ssot__Id__c)
- `EcomOrderLine → Individual` (PartyId__c → ssot__Id__c)
- `CartAbandonment → Individual` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_OrderProfile__cio` | total_orders__c, total_spend__c, avg_basket_size__c, days_since_last_order__c | High LTV: total_spend ≥ 500 · Frequent: total_orders ≥ 5 · Dormant: days_since_last_order ≥ 90 |
| `<Slug>_CartAbandonmentProfile__cio` | abandoned_carts__c, avg_cart_value__c, total_abandoned_value__c | Cart abandoners: abandoned_carts ≥ 1 · High-value abandoned: avg_cart_value ≥ 100 |
| `<Slug>_CustomerValue__cio` | churn_score__c, predicted_ltv__c, ltv__c | Churn risk: churn_score ≥ 65 · High predicted LTV: predicted_ltv ≥ 800 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Unreachable: emails_received > 0 AND emails_opened = 0 |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_CartAbandoners` | CartAbandonmentProfile.abandoned_carts ≥ 1 AND OrderProfile.total_orders ≥ 1 | Cart-recovery email or limited-time discount on abandoned items |
| `<Slug>_HighLTV` | OrderProfile.total_spend ≥ 500 (excl. churn_score ≥ 80) | VIP early access, exclusive promotions, priority service |
| `<Slug>_FrequentBuyers` | OrderProfile.total_orders ≥ 5 (excl. churn_score ≥ 70) | Loyalty programme invitation or tiered discount |
| `<Slug>_ChurnRisk` | CustomerValue.churn_score 65–89 | Time-limited win-back offer before lapse |
| `<Slug>_DormantShoppers` | OrderProfile.days_since_last_order ≥ 90 (excl. churn_score ≥ 65) | "We miss you" campaign + personalised product recommendation |

---

### Hospitality

Industry key: `"hospitality"` — always B2C.

**Story:** *"We run a hotel chain with guests across multiple properties. Data Cloud helps us reward frequent guests, win back dormant loyalty members, prevent cancellations, and upsell suite upgrades to high-revenue guests."*

**Model:** B2C — one Individual per guest. HotelStay is ENGAGEMENT (immutable stay event, checkin_datetime is event datetime). LoyaltyTransaction is ENGAGEMENT (earn/redeem events).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, loyalty_tier, loyalty_points_balance, days_since_last_purchase | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Hotel_Stays` | **Engagement** | `stay_id` | contact_id, **checkin_datetime (DateTime REQUIRED)**, checkout_date (Date), hotel_name, city, room_type (Standard/Superior/Deluxe/Junior Suite/Suite), nights_stayed, room_revenue, fnb_revenue, total_revenue, status (Completed/Cancelled/No-show), loyalty_points_earned | N×1–6 avg N×3 |
| `<Slug>_Loyalty_Transactions` | **Engagement** | `tx_id` | contact_id, **event_datetime (DateTime REQUIRED)**, type (earn/redeem), points, balance, reference | N×variable |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `HotelStay__dlm` (ENGAGEMENT), `LoyaltyTransaction__dlm` (ENGAGEMENT — shared with food/retail/banking/airlines, idempotent)

**Relationships:**
- `HotelStay → Individual` (PartyId__c → ssot__Id__c)
- `LoyaltyTransaction → Individual` (PartyId__c → ssot__Id__c)
- `ssot__EmailEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)
- `ssot__WebsiteEngagement → Individual` (ssot__IndividualId__c → ssot__Id__c)

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_StayProfile__cio` | total_stays__c, total_revenue__c, avg_revenue_per_stay__c, suite_stays__c, cancelled_stays__c, days_since_last_stay__c | Frequent: total_stays ≥ 3 · Suite upgrade: avg_revenue ≥ 200 AND suite_stays = 0 · Cancellation prone: cancelled_stays ≥ 2 |
| `<Slug>_LoyaltyProfile__cio` | current_points_balance__c, total_earned__c, total_redeemed__c, transaction_count__c | Unactivated: points ≥ 200 AND redeemed = 0 · Dormant: balance ≥ 500 AND days_since_last_stay ≥ 180 |
| `<Slug>_CustomerValue__cio` | churn_score__c, predicted_ltv__c, ltv__c | Churn risk: churn_score ≥ 65 · High predicted LTV: predicted_ltv ≥ 800 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Unreachable: emails_received > 0 AND emails_opened = 0 |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_FrequentGuests` | StayProfile.total_stays ≥ 3 (excl. churn_score ≥ 80) | Loyalty tier upgrade, complimentary breakfast, or early check-in |
| `<Slug>_DormantLoyalty` | LoyaltyProfile.current_points_balance ≥ 500 AND StayProfile.days_since_last_stay ≥ 180 | Points-expiry warning + exclusive returning-guest rate |
| `<Slug>_ChurnRisk` | CustomerValue.churn_score 65–89 | Limited-time member rate or complimentary upgrade offer |
| `<Slug>_SuiteUpgrade` | StayProfile.avg_revenue_per_stay ≥ 200 (excl. suite_stays ≥ 1) | Targeted suite upgrade offer at next booking |
| `<Slug>_CancellationProne` | StayProfile.cancelled_stays ≥ 2 | Flexible rate options + personalised pre-arrival message |

### Media / Streaming

Industry key: `"media"` — always B2C.

**Story:** *"We run a streaming service with subscribers across Basic, Standard, and Premium plans. Data Cloud helps us convert trial users, reduce churn among low-engagement subscribers, and upsell plan upgrades to binge watchers."*

**Model:** B2C — one Individual per subscriber. ContentView is ENGAGEMENT (immutable view event, view_datetime). Subscription is OTHER (mutable plan holding).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, loyalty_tier, days_since_last_purchase | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Subscriptions` | **Other** | `subscription_id` | contact_id, plan_name (Basic/Standard/Premium/Sports/Trial), plan_type (SVOD/Trial), monthly_fee, start_date (Date), status (Active 70%/Paused 10%/Cancelled 20%) | N |
| `<Slug>_Content_Views` | **Engagement** | `view_id` | contact_id, **view_datetime (DateTime REQUIRED)**, content_id, title, genre, duration_minutes, device_type (Smart TV/Mobile/Tablet/Desktop/Console), completed (true/false) | N×1–30 avg N×15 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `Subscription__dlm` (OTHER), `ContentView__dlm` (ENGAGEMENT)

**Relationships:**
- `Subscription → Individual` (PartyId__c → ssot__Id__c)
- `ContentView → Individual` (PartyId__c → ssot__Id__c)
- Standard EmailEngagement → Individual, WebsiteEngagement → Individual

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_SubscriptionProfile__cio` | plan_name__c, plan_type__c, monthly_fee__c, subscription_status__c, subscription_start__c | Active Premium: status = Active AND plan_type = Premium |
| `<Slug>_ContentProfile__cio` | total_views__c, total_watch_minutes__c, completion_rate__c, top_genre__c | Binge watchers: completion_rate ≥ 0.8 · Low engagement: total_watch_minutes < 120 |
| `<Slug>_CustomerValue__cio` | churn_score__c, predicted_ltv__c, ltv__c | Churn risk: churn_score ≥ 65 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_ActivePremium` | SubscriptionProfile.status = Active AND ContentProfile.total_views ≥ 20 | Cross-sell add-ons or annual plan upgrade |
| `<Slug>_ChurnRisk` | CustomerValue.churn_score 65–89 | Win-back with personalised content recommendation or discount |
| `<Slug>_BingeWatchers` | ContentProfile.completion_rate ≥ 0.8 | Promote new series releases and exclusive early access |
| `<Slug>_TrialConverts` | SubscriptionProfile.plan_type = Trial AND ContentProfile.total_views ≥ 5 | Convert to paid with personalised offer before trial expires |
| `<Slug>_Churned` | SubscriptionProfile.status = Cancelled | Win-back campaign with discounted reactivation rate |

---

### Automotive

Industry key: `"automotive"` — always B2C.

**Story:** *"We are a automotive dealership group (or OEM). Data Cloud helps us surface customers due for service, reward loyal service customers, upsell premium models, and retain high-value buyers."*

**Model:** B2C — one Individual per buyer. Vehicle is OTHER (mutable ownership record). ServiceRecord is OTHER (mutable service history).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, days_since_last_purchase | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Vehicles` | **Other** | `vehicle_id` | contact_id, vin, make, model, year, trim, color, purchase_date (Date), purchase_price, status (Active/Sold/Scrapped) | N×1–3 avg N×1.3 |
| `<Slug>_Service_Records` | **Other** | `service_id` | contact_id, vehicle_id→Vehicles, service_date (Date), service_type (Oil Change/MOT/Full Service/…), mileage, labor_cost, parts_cost, total_cost, technician | N×1–6 avg N×4 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `Vehicle__dlm` (OTHER), `ServiceRecord__dlm` (OTHER)

**Relationships:**
- `Vehicle → Individual` (PartyId__c → ssot__Id__c)
- `ServiceRecord → Individual` (PartyId__c → ssot__Id__c)
- Standard EmailEngagement → Individual, WebsiteEngagement → Individual

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_VehicleProfile__cio` | vehicles_owned__c, primary_make__c, primary_model__c, total_vehicle_value__c, latest_purchase_date__c | Premium buyers: total_vehicle_value ≥ 60K · Multi-vehicle: vehicles_owned ≥ 2 |
| `<Slug>_ServiceProfile__cio` | total_service_visits__c, total_service_spend__c, avg_service_cost__c, days_since_last_service__c | Service due: days_since_last_service ≥ 180 · Loyal: visits ≥ 4 |
| `<Slug>_CustomerValue__cio` | churn_score__c, predicted_ltv__c, ltv__c | Churn risk: churn_score ≥ 65 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_ServiceDue` | ServiceProfile.days_since_last_service ≥ 180 | Personalised service invitation + online booking link |
| `<Slug>_LoyalService` | ServiceProfile.total_service_visits ≥ 4 (excl. churn_score ≥ 80) | Preferred customer discount or free vehicle health check |
| `<Slug>_PremiumBuyers` | VehicleProfile.total_vehicle_value ≥ 60,000 | Exclusive service packages, accessories, and VIP preview events |
| `<Slug>_MultiVehicle` | VehicleProfile.vehicles_owned ≥ 2 | Fleet servicing packages or family vehicle add-ons |
| `<Slug>_ChurnRisk` | CustomerValue.churn_score 65–89 | Win-back with personalised service offer or trade-in appraisal |

---

### Real Estate

Industry key: `"real_estate"` — always B2C.

**Story:** *"We are a property agency. Data Cloud helps us prioritise active property searchers, surface luxury seekers for off-market listings, identify investors, convert renters to buyers, and re-engage lapsed clients."*

**Model:** B2C — one Individual per buyer/renter. PropertyInquiry is ENGAGEMENT (immutable inquiry event). PropertyTransaction is OTHER (mutable closed deal).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, days_since_last_purchase | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Property_Inquiries` | **Engagement** | `inquiry_id` | contact_id, **inquiry_datetime (DateTime REQUIRED)**, property_id, property_type (Apartment/House/Villa/Studio/Penthouse/Townhouse), listing_price, bedrooms, city, channel | N×1–8 avg N×4 |
| `<Slug>_Property_Transactions` | **Other** | `transaction_id` | contact_id, property_id, transaction_type (Purchase/Rental), close_date (Date), sale_price, property_type, bedrooms, city, agent_name, commission | N×0–3 avg N×0.6 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `PropertyInquiry__dlm` (ENGAGEMENT), `PropertyTransaction__dlm` (OTHER)

**Relationships:**
- `PropertyInquiry → Individual` (PartyId__c → ssot__Id__c)
- `PropertyTransaction → Individual` (PartyId__c → ssot__Id__c)
- Standard EmailEngagement → Individual, WebsiteEngagement → Individual

**CIs (4):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_InquiryProfile__cio` | total_inquiries__c, avg_inquiry_price__c, preferred_property_type__c, preferred_city__c, latest_inquiry_date__c | Active: inquiries ≥ 3 · Luxury: avg_price ≥ 1M |
| `<Slug>_TransactionProfile__cio` | total_transactions__c, total_sale_value__c, avg_sale_price__c, primary_transaction_type__c, latest_close_date__c | Repeat buyers: transactions ≥ 2 · Renters: type = Rental |
| `<Slug>_CustomerValue__cio` | churn_score__c, predicted_ltv__c, ltv__c | Churn risk: churn_score ≥ 65 |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_ActiveSearchers` | InquiryProfile.total_inquiries ≥ 3 | Personalised property alerts + dedicated agent assignment |
| `<Slug>_LuxurySeekers` | InquiryProfile.avg_inquiry_price ≥ 1,000,000 | Exclusive off-market listings and premium concierge service |
| `<Slug>_RepeatBuyers` | TransactionProfile.total_transactions ≥ 2 | Investment property insights and portfolio management services |
| `<Slug>_Renters` | TransactionProfile.primary_transaction_type = Rental | First-time buyer guide and mortgage pre-approval offer |
| `<Slug>_ChurnRisk` | CustomerValue.churn_score 65–89 | Personalised property match + follow-up call from agent |

---

### Betting

Industry key: `"betting"` — **always B2C** (even enterprise-sounding names map to B2C player model).

**Story:** *"We are a sports betting and casino operator. Data Cloud helps us reward VIP players, re-activate dormant accounts, identify at-risk players for responsible gaming, complete KYC flows, and reduce churn among high-value bettors."*

**Model:** B2C — one Individual per player. BettingTransaction is ENGAGEMENT (immutable bet event). BettingAccount is OTHER (mutable account holding).

**Streams (N = number of contacts):**

| Stream | Category | PK | Key fields | Rows |
|--------|----------|----|-----------|------|
| `<Slug>_Contacts` | Profile | `id` | first_name, last_name, email, city, churn_score, ltv, days_since_last_purchase | N |
| `<Slug>_Contact_Emails` | Profile | `id` | contact_id, email | N |
| `<Slug>_Betting_Accounts` | **Other** | `account_id` | contact_id, account_type (Sports/Casino/Combined), registration_date (Date), kyc_status (Verified/Pending/Failed), deposit_limit, balance, status (Active/Suspended/Closed), responsible_gaming_flag (true/false) | N |
| `<Slug>_Betting_Transactions` | **Engagement** | `tx_id` | contact_id, **transaction_datetime (DateTime REQUIRED)**, game_type (Sports Betting/Casino/Poker/Lottery/Virtual Sports), game_name, stake, payout, net_result, channel (Web/Mobile App/Retail) | N×1–40 avg N×20 |
| `<Slug>_Email_Engagement` | **Engagement** | `event_id` | contact_id, **sent_date (DateTime)**, campaign_name, opened, clicked | N×8 |
| `<Slug>_Web_Engagement` | **Engagement** | `event_id` | contact_id, **event_datetime (DateTime REQUIRED)**, page_url, page_category, event_type, device_type, duration_seconds | N×10 avg |

**Custom DMOs:** `BettingAccount__dlm` (OTHER), `BettingTransaction__dlm` (ENGAGEMENT)

**Relationships:**
- `BettingAccount → Individual` (PartyId__c → ssot__Id__c)
- `BettingTransaction → Individual` (PartyId__c → ssot__Id__c)
- Standard EmailEngagement → Individual, WebsiteEngagement → Individual

**CIs (3):**
| CI | Key measures | Demo use |
|----|-------------|---------|
| `<Slug>_PlayerProfile__cio` | total_bets__c, total_staked__c, total_payout__c, net_result__c, win_rate__c | VIP: total_staked ≥ 5000 · Inactive: total_bets < 3 |
| `<Slug>_RiskProfile__cio` | account_balance__c, deposit_limit__c, kyc_status__c, responsible_gaming_flag__c, churn_score__c | At-risk: rg_flag = true · KYC pending |
| `<Slug>_EngagementScore__cio` | emails_received__c, emails_opened__c, emails_clicked__c | Email engagement |

**Segments (5):**
| Segment | Logic | Story |
|---------|-------|-------|
| `<Slug>_VIPPlayers` | PlayerProfile.total_staked ≥ 5,000 (excl. responsible_gaming_flag = true) | VIP bonuses, dedicated account manager, exclusive promotions |
| `<Slug>_InactivePlayers` | PlayerProfile.total_bets < 3 | Welcome-back bonus or free bet offer |
| `<Slug>_AtRiskPlayers` | RiskProfile.responsible_gaming_flag = true | Responsible gaming messaging + deposit limit review flow |
| `<Slug>_KYCPending` | RiskProfile.kyc_status = Pending | Prompt to complete identity verification |
| `<Slug>_ChurnRisk` | CustomerValue.churn_score 65–89 | Personalised bonus tied to preferred game type |

---

## IMPORTANT GOTCHAS (handle silently)

### 1. DLO Category — the most common demo-breaking mistake

| Category | Use for | DateTime required? | Segment lookback? |
|----------|---------|-------------------|------------------|
| `Profile` | Static person/account data (who they are) | No | No |
| `Engagement` | Immutable timestamped events: email sends/opens, web visits, loyalty earn/redeem, banking transactions | **YES — DateTime field** | **YES — P2Y** |
| `Other` | Contracts, product holdings, mutable records: policies, orders, claims, prescriptions, financial accounts, banking products | No | No |

**Which streams are Engagement:**
- `email_engagement` → `sent_date` (DateTime)
- `web_engagement` → `event_datetime` (DateTime)
- `loyalty_transactions` (food B2C, banking, retail, food_b2b) → `event_datetime` (DateTime)
- `transactions` (banking) → `tx_datetime` (DateTime)
- `purchase_orders` (food B2C) → `order_datetime` (DateTime) — Transaction Journal pattern
- `sales_orders` (retail) → `order_datetime` (DateTime) — Transaction Journal pattern
- `wholesale_orders` (food B2B) → `order_datetime` (DateTime) — Transaction Journal pattern
- `prescriptions` (pharma) → `fill_datetime` (DateTime) — Transaction Journal pattern
- `activity_records` (sports_club) → `activity_date` (DateTime)
- `ecom_orders` (ecommerce) → `order_datetime` (DateTime) — Transaction Journal pattern
- `cart_abandonments` (ecommerce) → `abandonment_datetime` (DateTime)
- `hotel_stays` (hospitality) → `checkin_datetime` (DateTime)
- `loyalty_transactions` (hospitality) → `event_datetime` (DateTime)
- `content_views` (media) → `view_datetime` (DateTime)
- `property_inquiries` (real_estate) → `inquiry_datetime` (DateTime)
- `betting_transactions` (betting) → `transaction_datetime` (DateTime)

**Which streams are Other (not Engagement):**
- Everything contractual or mutable: policies, claims, order lines, financial accounts, banking products, service contracts, subscriptions, usage records, memberships
- Orders and prescriptions are **Engagement** (they are immutable timestamped fills/purchases)
- `ecom_order_lines` is **Other** — mutable line-item detail (no DateTime event)

**Symptom of wrong category:** A filter like "has at least 1 active policy" returns ~15 members.
This is because Engagement DLOs apply the segment's `lookbackPeriod` (default P2Y) to the
`eventDateTimeFieldName`. Any record older than 2 years is completely invisible.

**Fix pattern for existing wrong-category stream:** You cannot change a DLO category after creation.
Delete the stream and recreate with the correct category. If the DMO is also locked as Engagement
(due to DemoBuilder or a previous wrong mapping), create a new DMO with a V2 suffix.

### 2. CI join returns 0 rows — data consistency issue

If `SELECT COUNT(*) FROM <CI>` returns 0 after IR is done, the `PartyId__c` values in the
custom DMO don't match any `SourceRecordId__c` in the unified link table
(`UnifiedLinkssotIndividualRt__dlm` for B2C, `UnifiedLinkssotAccountRt__dlm` for B2B).

**Root cause:** The transactional CSVs and contacts CSV were generated in different sessions
(different random seeds → different UUIDs). They must be generated together in one `gen_data.py`
run to share the same contact IDs.

**Emergency fix (if data was uploaded with wrong IDs):**
1. Fetch the valid contact IDs from the org:
   ```python
   SELECT ssot__Id__c FROM ssot__Individual__dlm LIMIT 200 OFFSET {n}
   ```
2. Regenerate the transactional CSVs using those IDs as `contact_id`
3. Delete old transactional streams and re-upload new CSVs
4. Re-trigger CIs

### 3. DMO name conflicts with DemoBuilder

On a **clean org** (no previous DemoBuilder), `InsurancePolicy__dlm` and `InsuranceClaim__dlm` are fine.

On an org where **DemoBuilder previously created** these DMOs as Engagement category (locked),
you cannot change a DMO's category via API after a DLO is mapped to it.

**Fix for locked-DMO orgs:** Rename to `InsurancePolicyV2__dlm` / `InsuranceClaimV2__dlm` by
modifying `INDUSTRY_DMOS` in `create_dmos.py`. The label can still be "Insurance Policy" — only
the developer name needs to be unique. Also update the matching entries in `create_mappings.py`,
`create_relationships.py`, and `create_calculated_insights.py`.

### 4. Ingestion trigger endpoint

- ✅ `POST /ssot/data-streams/{name}/actions/run` → 201 `{"success": true}`
- ❌ `POST /ssot/data-streams/{name}/actions/trigger-refresh` → 404
- ❌ `POST /ssot/data-streams/{name}/actions/trigger` → 404
- ❌ `POST /ssot/data-streams/{name}/actions/start` → 404

### 5. IR run trigger endpoint

- ✅ `POST /ssot/identity-resolutions/{id}/actions/run-now` (Storm/Hyperforce orgs)
- ❌ `POST /ssot/identity-resolutions/{id}/actions/run` → 404 on Storm

### 6. Boolean CSV fields → Number DMO fields ONLY

CSV `opened`/`clicked`/`unsubscribed` are 0/1 integers. The DLO infers them as Number.
**You cannot map Number DLO → Boolean DMO.** The platform hard-rejects with:
`"clicked__c 's type Number is different from Clicked__c 's type Boolean"`

Solution: create Number alias fields (`OpenedCount__c`, `ClickedCount__c`, `UnsubscribedCount__c`)
via DMO PATCH. Reference `*Count__c` in CIs.

### 7. CI schedule — valid intervals only

- ✅ `"SIX"` — every 6 hours (closest available to "daily")
- ✅ `"SYSTEM_MANAGED"` — system decides frequency
- ❌ `"DAILY"` — rejected
- ❌ `"HOURLY"` — rejected
- ❌ `"NOT_SCHEDULED"` — rejected

### 8. Segment creation — segmentCreationFlow mandatory

Without `"segmentCreationFlow": "Datakit"`:
`403 "UI based segment creation is forbidden for external users"`

Required fields: `segmentType: "Ui"`, `segmentCreationFlow: "Datakit"`,
`segmentOnApiName: "UnifiedssotIndividualRt__dlm"` (B2C) or `"UnifiedssotAccountRt__dlm"` (B2B),
`publishSchedule: "TwentyFour"`.

### 9. CI SQL constraints

- Column aliases must end with `__c`
- `CASE WHEN <aggregation>` is not supported. Use plain `SUM(CASE WHEN ...)` instead.
- `MAX()`/`MIN()` work on Number and Date only — NOT on Text fields
- Do NOT use table aliases in FROM clause — use full DMO names
- CI can only reference fields that have an active DLO→DMO mapping

### 10. Relationships — direction matters

Always deploy **child → parent (N:1)**. E.g. `InsurancePolicy → Individual`.
NEVER `Individual → InsurancePolicy` (parent → child is wrong and causes phantom ~15-member counts).

Pre-check existing relationships before deploying:
```python
GET /ssot/data-model-objects/{child_dmo}/relationships?dataspace=default
```
Redeploy an existing relationship → creates an INACTIVE duplicate → skip.

### 11. API field naming

- `dataSpaceName` (capital N in body) — wrong key causes 500 UNKNOWN_EXCEPTION
- `category: "OTHER"` UPPERCASE only — "Other" causes 500
- `fieldMapping` singular in mapping POST body
- Source fields in mapping body carry `__c` suffix
- `?dataspace=default` always as URL query param
- CI POST: `?dataspace=default` in URL, NOT in body

### 12. Relationships deploy via sf CLI

The REST endpoint for creating relationships returns UNKNOWN_EXCEPTION.
Use `sf project deploy start` with `.fieldSrcTrgtRelationship-meta.xml`.
Always pass `SFDX_DISABLE_DNS_CHECK=true` for DNS timeout issues on certain networks.

### 13. CI blocked by segment

`DELETE /ssot/calculated-insights/{name}` fails when a segment references the CI.
Use PATCH with new `expression` instead — works even with active segment dependencies.

### 14. Segment count ~15 = broken, not low

A segment member count of exactly 15 (or single-digit) almost always means:
- A transactional DMO was accidentally mapped as Engagement
- A relationship was deployed in the wrong direction (parent→child)
- The CI uses a direct DMO filter on an Engagement DLO
- Data IDs are inconsistent (UUID mismatch between contacts and policies)

Any count under 50 for a population of 7,000+ should be treated as a bug, not a low count.

### 15. Field renames after schema update

If you are re-seeding an org that was previously seeded with an **older version** of this skill,
be aware of these column renames in the CSV data:

| Stream | Old field name | New field name | Reason |
|--------|---------------|---------------|--------|
| `loyalty_transactions` (food B2C, banking, retail, food_b2b) | `date` | `event_datetime` | Engagement requires DateTime, not Date |
| `transactions` (banking) | `tx_date` | `tx_datetime` | Engagement requires DateTime, not Date |
| `purchase_orders` (food B2C) | `order_date` | `order_datetime` | Promoted to Engagement — DateTime required |
| `sales_orders` (retail) | `order_date` | `order_datetime` | Promoted to Engagement — DateTime required |
| `wholesale_orders` (food B2B) | `order_date` | `order_datetime` | Promoted to Engagement — DateTime required |
| `prescriptions` (pharma) | `prescribed_date` | `fill_datetime` | Promoted to Engagement — DateTime required; also renamed to reflect "fill" event |
| `usage_records` (telco) | `month` | `usage_date` | Changed from Text (YYYY-MM) to Date (YYYY-MM-01) for native range filtering |
| `ht_usage_records` (hightech) | `usage_month` | `usage_date` | Changed from Text (YYYY-MM) to Date (YYYY-MM-01) for native range filtering |

These renamed fields are also reflected in:
- `create_dmos.py` — DMO field renamed (e.g. `OrderDate__c` → `OrderDatetime__c`, `PrescribedDate__c` → `FillDatetime__c`, `UsageMonth__c` → `UsageDate__c`)
- `create_mappings.py` — mapping pairs updated to new field names
- `upload_and_stream.py` — `CATEGORY_MAP` and `EVENT_DATE_MAP` entries updated

**If the old stream still exists in the org**, you must delete it and recreate (the field name in the
DLO is fixed at creation time and the category cannot be changed after mapping).
Run `upload_and_stream.py` to recreate — it will skip if the stream name already exists, so delete
it first from Data Cloud → Data Streams.

### 16. Enrichment fields live on standard DMOs — shared across demos

Enrichment fields (ChurnScore__c, LoyaltyTier__c, Ltv__c, NpsScore__c, etc.) are now custom fields
on `ssot__Individual__dlm` (B2C) or `ssot__Account__dlm` (B2B) rather than a separate
`IndividualProfile__dlm` custom DMO.

`create_dmos.py` calls `extend_standard_dmo()` which POSTs each field to:
  POST /ssot/data-model-objects/{dmo}/fields

This is idempotent — if a field already exists (409 / DUPLICATE response), it is treated as
success and skipped. This means:
- Running multiple demos on the same org is safe — no duplicate DMOs or fields
- The standard DMO is shared: all demos that use the same org will see these fields
- CIs that reference ssot__Individual__dlm.ChurnScore__c work across all industries

For B2B (food_b2b, hightech): the same fields are added to ssot__Account__dlm.

If API returns 500 on field creation, check if the field already exists with:
  GET /ssot/data-model-objects/ssot__Individual__dlm/fields

### 17. Engagement event dates — 720-day cap (not 730)

Engagement DLO lookback is exactly 2 years (730 days). However, to avoid edge-case data loss
from clock skew, timezone differences, or slow ingestion, all gen_data.py event dates are
capped at **720 days** (not 730). This gives a 10-day safety buffer.

Data > 720 days old is still valid — it just won't appear in Segment Builder filters that
use the P2Y lookback. For demo purposes this is fine since all filters use the last 2 years.
