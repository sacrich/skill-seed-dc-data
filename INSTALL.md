# Install — seed-demo-data skill

A conversational wizard that seeds a complete, production-quality synthetic dataset into
Salesforce Data Cloud in one session. Supports 18 verticals.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| [Claude Code](https://claude.ai/code) | latest | `npm install -g @anthropic-ai/claude-code` |
| [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) | ≥ 2.x | `npm install -g @salesforce/cli` |
| Python | ≥ 3.9 | [python.org](https://www.python.org/downloads/) |
| Python `requests` | any | `pip3 install requests` |

The target org must have **Salesforce Data Cloud** provisioned and at least one Data Stream
ingestion credential (S3 or direct upload).

---

## Install (one-time per machine)

```bash
git clone git@github.com:sacrich/skill-seed-dc-data.git \
  ~/.claude/skills/skill-seed-dc-data
```

---

## Authenticate your org (before each demo setup)

```bash
sf org login web --alias <demo-org>
sf org display --target-org <demo-org>   # verify it works
```

---

## Run

Open Claude Code in any directory and type:

```
/seed-demo-data
```

or just say:

> *"Seed a Data Cloud demo for [client name]"*

The wizard will:
1. Ask which org alias to use
2. Auto-detect the industry from the client name
3. Show a full data plan for your confirmation
4. Generate CSVs, upload, create DMOs/mappings/IR/CIs/segments end-to-end

---

## Update

Pull the latest verticals and fixes:

```bash
cd ~/.claude/skills/skill-seed-dc-data && git pull
```

---

## Supported verticals (18)

insurance · food · food_b2b · retail · banking · pharma · telco · hightech ·
utilities · airlines · healthcare · sports_club · ecommerce · hospitality ·
media · automotive · real_estate · betting

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `sf` not found | `npm install -g @salesforce/cli` |
| `ModuleNotFoundError: requests` | `pip3 install requests` |
| Org auth expired | `sf org login web --alias <demo-org>` |
| Segments show 0 members | IR not yet completed — wait and re-run `create_segments.py` |
| CI has no data | IR must finish before CIs compute — wizard gates this automatically |
