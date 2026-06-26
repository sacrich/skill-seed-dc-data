# seed-demo-data

> Conversational wizard that seeds a complete, production-quality synthetic dataset into **Salesforce Data Cloud** — end to end, in one session.

Covers profiles, transactions, engagement events, custom DMOs, field mappings, Identity Resolution, Calculated Insights, and Segments. No manual configuration. No SQL writing. No UI clicking.

---

## Supported verticals (18)

| Vertical | Example clients |
|----------|----------------|
| Insurance | Migdal, Clal, Allianz, AXA |
| Food B2C | Shufersal, Rami Levy, Carrefour |
| Food B2B | Tnuva, Strauss, Unilever |
| Retail | Zara, Castro, H&M |
| Banking | Hapoalim, Leumi, Santander |
| Pharma | Teva, SuperPharm, Pfizer |
| Telco | Partner, Bezeq, Vodafone |
| Hightech B2B | Hibob, Monday.com, Salesforce |
| Utilities | Israel Electric, E.ON, EDF |
| Airlines | El Al, Iberia, Wizz Air |
| Healthcare | Clalit, Maccabi Health, Kaiser |
| Sports Club | Holmes Place, FC Barcelona, Basic-Fit |
| Ecommerce | Amazon, Zalando, ASOS |
| Hospitality | Marriott, Fattal, Leonardo Hotels |
| Media | Netflix, YES, Spotify |
| Automotive | Toyota Israel, Hyundai, BMW |
| Real Estate | Re/Max, Zillow, Idealista |
| Betting | Mifal HaPais, Bet365, 888sport |

---

## What it builds

For every client, the wizard creates:

- **Synthetic data** — 10,000 profiles + all industry-specific transactional tables (CSVs)
- **Data Streams** — uploaded and ingested into Data Cloud
- **DMOs** — custom Data Model Objects with descriptions and field types, plus custom fields on standard DMOs (`ssot__Individual__dlm` / `ssot__Account__dlm`)
- **Field mappings** — every CSV column mapped to the correct DMO field
- **DMO relationships** — correct cardinality, traversable in Segment Builder
- **Identity Resolution** — B2C (Individual) or B2B (Account) mode, triggered automatically
- **Calculated Insights** — 2–3 industry-relevant CIs per vertical, 6-hour refresh
- **Segments** — 5 actionable segments per vertical, member count verified

**Total time:** ~20–35 minutes (most of it waiting for IR and CIs to compute).

---

## Install

**Prerequisites:** [Claude Code](https://claude.ai/code) · [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) ≥ 2.x · Python ≥ 3.9 · `pip3 install requests`

```bash
git clone https://github.com/sacrich/skill-seed-dc-data.git \
  ~/.claude/skills/skill-seed-dc-data
```

See [INSTALL.md](INSTALL.md) for full setup.

---

## Usage

Open Claude Code and type:

```
/seed-demo-data
```

or

> *"Seed a Data Cloud demo for Clalit"*

The wizard asks which org to use, auto-detects the industry, shows a full data plan for your approval, then runs the complete pipeline.

---

## Utilities

| Script | Purpose |
|--------|---------|
| `scripts/gen_data.py` | Generate synthetic CSVs |
| `scripts/upload_and_stream.py` | Upload CSVs and create Data Streams |
| `scripts/create_dmos.py` | Create custom DMOs |
| `scripts/create_mappings.py` | Create field mappings |
| `scripts/create_relationships.py` | Create DMO relationships |
| `scripts/setup_ir.py` | Set up Identity Resolution |
| `scripts/create_calculated_insights.py` | Create Calculated Insights |
| `scripts/create_segments.py` | Create Segments |
| `scripts/cleanup.py` | **Delete all demo artifacts for a client slug** |
| `scripts/verify_ingestion.py` | Verify all streams have rows |
| `scripts/verify_ir.py` | Wait for IR completion |
| `scripts/verify_cis.py` | Verify CIs have output rows |

---

## Update

```bash
cd ~/.claude/skills/skill-seed-dc-data && git pull
```

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
