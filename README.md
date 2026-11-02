# Smartsheet → Sigma

A coding-agent skill for one-time migration of Smartsheet sheets, reports,
dashboards, or complete workspaces into Sigma.

The migration starts with a source-bound Smartsheet API snapshot. Editable sheet
columns become Sigma input-table candidates, supported formulas become live
calculations, and reports and dashboards become workbook plans. Features the API
cannot fully describe—such as multistep automations—are surfaced as review items
instead of silently omitted.

## Install

### Claude Code

```text
/plugin marketplace add twells89/smartsheet-to-sigma
/plugin install smartsheet-to-sigma@smartsheet-to-sigma
```

Invoke `/smartsheet-to-sigma:smartsheet-to-sigma` or ask Claude to migrate a
Smartsheet asset or workspace.

### Other coding agents

Clone this repository and point the agent at
[`skills/smartsheet-to-sigma/SKILL.md`](skills/smartsheet-to-sigma/SKILL.md).
For a standalone installation, copy the entire `skills/smartsheet-to-sigma/`
directory so its `SKILL.md`, `refs/`, `scripts/`, and requirements remain
together.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export SMARTSHEET_ACCESS_TOKEN='...'

cd skills/smartsheet-to-sigma
python scripts/migrate.py plan 'https://app.smartsheet.com/sheets/SHEET_ID' \
  --scope asset --asset-type sheet --out-dir /tmp/smartsheet-plan
python scripts/build-migration.py /tmp/smartsheet-plan/plan.json \
  --out-dir /tmp/smartsheet-build
```

Use `--scope workspace` with a workspace ID or URL to capture and plan all
accessible sheets, reports, and dashboards in dependency order. The workflow is
a one-time migration; it does not create webhooks or an ongoing sync.
Python 3.10 or newer is required.

See [`skills/smartsheet-to-sigma/QUICKSTART.md`](skills/smartsheet-to-sigma/QUICKSTART.md)
for fixture-based and authenticated examples.

## What it preserves

- stable Smartsheet row IDs, parent IDs, sibling order, and hierarchy depth;
- editable literal columns as Sigma input-table candidates;
- supported formulas as live Sigma calculation plans;
- report rows and source lineage;
- supported dashboard widgets as Sigma workbook element plans;
- source/API limitations, automation, sharing, and collaboration features as
  explicit review items;
- a canonical source hash binding audit, plan, review, and build artifacts.

## Repository layout

```text
.claude-plugin/                  Claude Code plugin and marketplace metadata
skills/smartsheet-to-sigma/
  SKILL.md                       agent workflow and routing contract
  QUICKSTART.md                  runnable examples
  refs/                          translation and coverage contracts
  scripts/
    migrate.py                   capture, audit, discover, and plan router
    audit-smartsheet.py          audit an API snapshot
    build-migration.py           emit Sigma build artifacts and parity report
    lib/                         source, formula, planning, and Sigma libraries
  tests/                         offline API fixtures and unit tests
```

## Credentials

Smartsheet access is read-only and resolved from `SMARTSHEET_ACCESS_TOKEN` (or
the compatibility name `SMARTSHEET_API_TOKEN`). Never put a token on the command
line. Sigma posting is intentionally separated from source capture; the emitted
build bundle can be reviewed before any destination mutation.

## License

MIT
