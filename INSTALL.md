# Install — seed-demo-data skill

A conversational wizard that seeds a complete, production-quality synthetic dataset into
Salesforce Data Cloud in one session. Supports 19 verticals.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| [Claude Code](https://claude.ai/code) | latest | `npm install -g @anthropic-ai/claude-code` |
| [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) | ≥ 2.x | `npm install -g @salesforce/cli` |
| Python | ≥ 3.9 | [python.org](https://www.python.org/downloads/) |

No extra Python packages needed — scripts use the standard library only.

### Org requirements

The target org must have **Salesforce Data Cloud provisioned and licensed**.
The upload step uses Salesforce Drive (S3 presigned URLs) — **no AWS account needed**.
This works on any properly licensed Data Cloud org (Storm / Hyperforce instances).

> **Verify before starting:** run `preflight.py` (see below) to confirm the org is ready
> before going through the full wizard.

---

## Install (one-time per machine)

```bash
git clone https://github.com/sacrich/skill-seed-dc-data.git \
  ~/.claude/skills/seed-demo-data
```

---

## Authenticate your org (before each demo setup)

Log in once per org — a browser window opens for SSO:

```bash
sf org login web --alias etoro-demo
sf org display --target-org etoro-demo   # verify it works
```

Pick any short alias you like (`lot-demo`, `varonis-demo`, etc.) — the wizard will ask for it.

---

## Pre-flight check (recommended before every new demo)

Verifies Python, sf CLI, org auth, and Data Cloud access in one shot:

```bash
python3 ~/.claude/skills/seed-demo-data/scripts/preflight.py --alias etoro-demo
```

Expected output:
```
✅  Python 3.11.x (OK)
✅  Salesforce CLI 2.x.x (OK)
✅  Org 'etoro-demo' authenticated: se@demo.com (https://etoro.my.salesforce.com)
✅  Data Cloud API accessible
✅  All checks passed — ready to seed.
```

---

## Run

Open Claude Code in any directory and type:

```
/seed-demo-data
```

or just say:

> *"Seed a Data Cloud demo for eToro"*

The wizard will:
1. Ask which org alias to use
2. Auto-detect the industry from the client name
3. Show a full data plan for your confirmation
4. Generate CSVs, upload, create DMOs / mappings / IR / CIs / segments end-to-end

---

## Update

Pull the latest verticals and fixes:

```bash
cd ~/.claude/skills/seed-demo-data && git pull
```

---

## Supported verticals (19)

insurance · food · food_b2b · retail · banking · pharma · telco · hightech ·
utilities · airlines · healthcare · sports_club · ecommerce · hospitality ·
media · automotive · real_estate · betting · **trading**

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `sf` not found | `npm install -g @salesforce/cli` |
| `sf` version < 2.x | `npm install -g @salesforce/cli` (upgrades in place) |
| Org auth expired | `sf org login web --alias etoro-demo` |
| Upload fails with `presigned URL` error | Re-run immediately — S3 URLs expire in ~15 min |
| Upload fails with `403` | Org may not have Data Cloud licensed — run `preflight.py` |
| Segments show 0 members | IR not yet completed — wizard auto-retries, or re-run `create_segments.py` |
| CI has no data | IR must finish before CIs compute — wizard gates this automatically |
| `permission denied (publickey)` on git clone | Use HTTPS clone URL (already shown above) |
