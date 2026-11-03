---
name: smartsheet-to-sigma
description: >-
  Audit and perform a one-time migration of a Smartsheet sheet, report,
  dashboard, or complete workspace into Sigma. Use when the user wants to
  preserve editable grid data with Sigma input tables, translate supported
  formulas, or rebuild Smartsheet reports and dashboards in Sigma. Requires
  read access to Smartsheet and a user-provided Sigma destination.
---

# Smartsheet → Sigma

This skill creates a **one-time, source-bound migration**. It does not install
webhooks and does not promise continuing synchronization.

Start with the router. Do not read every ref first:

```bash
python scripts/migrate.py plan <Smartsheet-ID-or-URL> \
  --scope asset|workspace \
  --asset-type sheet|report|dashboard \
  --out-dir <workdir>
```

`--asset-type` is required for a bare asset ID and ignored for workspace scope.
It can usually be inferred from an app URL. For an offline or previously
captured source, add `--snapshot <source-snapshot.json>`; no Smartsheet
credential is then required.

The router writes:

- `source-snapshot.json` — canonical API response bundle and `sourceSha256`;
- `migration-audit.json` — `AUTO`, `REVIEW_REQUIRED`, or `BLOCKED`;
- `discovery.json` — typed asset, dependency, formula, and feature inventory;
- `plan.json` — destination-neutral routes plus Sigma build intent;
- `review.template.json` — source-bound dispositions to complete before a
  production migration.
- `workflow-capture-plan.json` — paginated Smartsheet MCP `get_workflows`
  calls required to inventory full automation trees.

It prints `readRefs`, `askUser`, `nextCommands`, and `userHandoffs`. Read only
the selected refs, answer the listed decisions, then run commands in order.

| When | Read |
|---|---|
| Always | `refs/coverage-contract.md` |
| Mapping sheet columns, formulas, reports, or dashboards | `refs/smartsheet-translation.md` |
| Emitting or executing Sigma artifacts | `refs/sigma-build-gotchas.md` |

## Source credentials

Use a read-only OAuth access token or personal access token in the environment:

```bash
export SMARTSHEET_ACCESS_TOKEN='...'
```

`SMARTSHEET_API_TOKEN` is accepted as a compatibility alias. Never put tokens
on the command line, in plan JSON, or in committed files. The client retries
bounded `429` and transient `5xx` responses and follows API pagination.
Python 3.10 or newer is required.

For automations, load the Smartsheet MCP server's `automations` toolset with
`READ_SHEETS` access. This skill never installs or authenticates that server.

## Scope

### Asset

Migrate one sheet, report, or dashboard. Dashboard capture also retrieves
referenced source assets when their IDs are available and accessible.

### Workspace

Capture all accessible sheets, reports, dashboards, and nested folders in the
workspace. The plan deduplicates source assets and orders them:

1. source sheets and editable grids;
2. reports derived from those sheets;
3. dashboards that depend on sheets or reports.

The workspace maps to one Sigma folder. Shared source sheets are built once.

## Preserve editability

Classify source columns and cells by behavior:

| Smartsheet behavior | Sigma route |
|---|---|
| literal values users edit | empty/CSV input-table data-entry columns |
| uniform supported column formula | computed Sigma column |
| mixed literals and formulas | split input provenance from calculation; review overrides |
| report result | derived workbook table when reconstructable, otherwise an explicit snapshot route |
| dashboard widget | native workbook element when supported |
| automation workflow | automated-action intent plus trigger/effect handoffs |
| comments, proofs, attachments, sharing policy | review/handoff; never silently omit |

Rows retain `ROW_ID`, `PARENT_ROW_ID`, sibling order, and hierarchy depth.
Stable row IDs are natural-key candidates. Dates and calculated row numbers are
not stable keys.

## Formula policy

The source snapshot stores both formula text and Smartsheet's computed value.
The translator only emits formulas from its tested subset. Cross-sheet ranges,
row-number ranges, hierarchy-sensitive expressions, unsupported functions,
formula errors, or nonuniform per-cell logic remain explicit review items.
Computed values are parity targets, not substitutes for live calculations.

Do not call a migration complete because baseline values match while formulas
were frozen.

## Automation workflow capture and translation

The REST automation-rules endpoint is insufficient: it omits multistep
workflows. After the first router pass, execute every call in
`workflow-capture-plan.json` with Smartsheet MCP `get_workflows`, following
`last_key` until absent. Materialize the specified output contract with an
unbroken page cursor chain and ordered workflow IDs, then rerun:

```bash
python scripts/migrate.py plan <source-label> \
  --scope asset|workspace \
  --snapshot <workdir>/source-snapshot.json \
  --automation-workflows <workdir>/automation-workflows.json \
  --out-dir <enriched-workdir>
```

The enriched snapshot hash includes the full workflow trees. The translator
emits Sigma effect intents for:

| Smartsheet behavior | Sigma route |
|---|---|
| email/notification | notification action effect |
| conditions / if-else | conditional action sequence |
| API/HTTP call | API-call effect; connector credential handoff |
| change/clear/assign/record-date | input-table update effect |
| copy row | input-table insert effect |
| move row | insert-then-delete scaffold with transactional parity |
| approval/update request | input state + notification + user-action scaffold |
| schedule/date trigger | scheduled automated action; timezone/cron confirmation |
| row/cell change trigger | external event bridge to a Sigma webhook |

Smartsheet MCP does not return reminder workflows, so a manual reminder
inventory remains mandatory. Sigma webhook HMAC, API connector credentials,
publishing, and event-bridge deployment remain explicit handoffs. Unknown node
types fail into `AUTOMATION_EFFECT_REVIEW_REQUIRED`. Production completion
requires a complete MCP capture for every sheet; a disposition cannot substitute
for missing workflow trees.

## Review gate

`BLOCKED` always stops. Local build generation can proceed with
`REVIEW_REQUIRED`, but production completion requires source-bound dispositions:

```json
{
  "sourceSha256": "<canonical snapshot hash>",
  "planSha256": "<final plan hash after mixed-column policy>",
  "issues": [
    {
      "issueId": "AUTOMATION_REVIEW_REQUIRED:source-bound-id",
      "code": "AUTOMATION_REVIEW_REQUIRED",
      "owner": "Named migration owner",
      "disposition": "Reimplemented as a Sigma action and tested",
      "verified": true,
      "evidence": {
        "status": "passed",
        "evidenceId": "runtime-check-123",
        "targetId": "sigma-element-id"
      }
    }
  ]
}
```

Every current issue instance has a stable `issueId`; each instance needs a
nonempty owner and disposition plus
`verified: true` and source/target runtime evidence. A changed snapshot has a
different hash and invalidates the approval. The final `planSha256` also binds
evidence to the selected mixed-column policy and emitted formulas; use the hash
from the draft build manifest after applying `--mixed-policy`.

## Build

Generate reviewable Sigma artifacts:

```bash
python scripts/build-migration.py <workdir>/plan.json \
  --out-dir <builddir>
```

The bundle contains input-table definitions, data-model intents, workbook code
representation, captured source rows, a Sigma MCP row-seeding program, a
source-to-target manifest, `automation-intents.json`, and baseline parity
targets. Workbook artifacts retain these under `deferredAutomationIntents`;
they are not placed in `contents.automatedActions` until an agent compiles them
against the tenant's current OpenAPI schema. Generation is local and
non-mutating. If the source mixes formulas and literals in one column, select
`--mixed-policy override-wins|formula-wins`; production completion refuses an
unresolved policy. Review the bundle before executing it with the Sigma tools
supplied by the user or agent.

To check final production evidence, rerun with the exact snapshot and reviewed
dispositions:

```bash
python scripts/build-migration.py <workdir>/plan.json \
  --out-dir <builddir> \
  --mixed-policy override-wins \
  --source-snapshot <workdir>/source-snapshot.json \
  --review-dispositions review.json \
  --production
```

Collect UI-only or environment-specific actions—write connection selection,
warehouse view creation, publishing, and security assignment—into one user
handoff rather than interleaving them.

## Completion contract

A migration is complete only when:

1. every captured asset has a target or verified disposition;
2. editable columns remain editable at the intended grain;
3. live supported formulas match captured computed values;
4. report rows and dashboard metrics meet parity targets;
5. every captured automation has a target sequence or verified disposition;
6. automated triggers/effects pass dry-run and live side-effect checks;
7. every review issue has source-bound evidence;
8. source-to-target IDs and user handoffs are recorded.

Smartsheet sharing is not Sigma row-level security. Inventory access metadata,
but require the destination owner to choose Sigma folder/workbook permissions
and any warehouse RLS/CLS policy.
