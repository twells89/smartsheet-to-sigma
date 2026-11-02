# Quick start

Run commands from `skills/smartsheet-to-sigma`.

## Offline fixture

No credentials are needed to exercise the full local workflow:

```bash
python scripts/migrate.py plan fixture-sheet \
  --scope asset \
  --asset-type sheet \
  --snapshot tests/fixtures/asset-snapshot.json \
  --out-dir /tmp/smartsheet-plan

python scripts/build-migration.py /tmp/smartsheet-plan/plan.json \
  --out-dir /tmp/smartsheet-build
```

Inspect:

```text
/tmp/smartsheet-plan/migration-audit.json
/tmp/smartsheet-plan/discovery.json
/tmp/smartsheet-plan/plan.json
/tmp/smartsheet-build/input-tables.json
/tmp/smartsheet-build/data-models.json
/tmp/smartsheet-build/workbooks.json
/tmp/smartsheet-build/source-data.json
/tmp/smartsheet-build/dashboard-review.json
/tmp/smartsheet-build/mcp-seed-program.py
/tmp/smartsheet-build/migration-manifest.json
/tmp/smartsheet-build/parity.json
```

## Authenticated asset

```bash
export SMARTSHEET_ACCESS_TOKEN='read-only token'

python scripts/migrate.py plan \
  'https://app.smartsheet.com/sheets/1234567890123456' \
  --scope asset \
  --asset-type sheet \
  --out-dir /tmp/customer-sheet
```

For a bare numeric ID, always provide `--asset-type`. URL parsing is a
convenience, not authorization; the token must be able to read the asset.

## Authenticated workspace

```bash
python scripts/migrate.py plan 1234567890123456 \
  --scope workspace \
  --out-dir /tmp/customer-workspace
```

Workspace capture fetches each accessible sheet, report, and dashboard and
records inaccessible children as blockers. It does not modify Smartsheet and
does not create webhooks.

## Review-required plans

Copy `review.template.json`, fill each owner/disposition, and set `verified` to
true only after the target behavior has actually been tested. The file is bound
to the canonical source snapshot hash.

## Tests

From the skill directory (also valid after copying the skill by itself):

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

Python 3.10 or newer is required. In the full repository, run
`python3 -m unittest discover -s ../../tests -v` from the skill directory for
the additional plugin packaging checks.
