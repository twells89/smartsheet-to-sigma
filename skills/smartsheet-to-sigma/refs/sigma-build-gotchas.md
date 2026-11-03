# Sigma build gotchas

The local build bundle is an intent and code-representation bundle. Validate it
against the current Sigma OpenAPI and the destination tenant before mutation.

## Data entry

- Use stable Smartsheet row IDs as natural keys where appropriate.
- Do not use dates, timestamps, calculated row numbers, or mutable names as
  linked input-table primary keys.
- Input tables require a write-enabled connection and a designated write
  schema.
- Input-table records are not embedded in REST workbook code representation.
  Seed or update rows through Sigma MCP Code Mode or a reviewed UI import.
- Pre-query natural keys before upsert; inserts do not inherently deduplicate
  business keys.
- A warehouse view over an input table remains an environment-specific handoff.

## Workbooks and data models

- Dry-run the current workbook `contents` request before creation.
- Read back server-assigned element and column IDs after creation; authored IDs
  are not durable target identity.
- Build layout after elements. Layout owns page membership.
- Preserve formula parity with live calculations, not cached source values.
- Publish, folder permissions, and tenant security are explicit user handoffs.

## Reports and dashboards

Smartsheet report criteria and dashboard widgets can be only partially visible
through the API. Do not infer missing logic from returned rows. Build supported
elements and retain a review record for everything else.

## Automated actions

- `automation-intents.json` preserves every workflow's source-tree digest,
  mapping decision, handoff, and parity target. Destination intents redact
  recipients and credential-like values into bindings; raw trees remain only
  in owner-readable source/capture artifacts.
- `workbooks.json` carries those intents under `deferredAutomationIntents`,
  outside the POST body. Compile only supported intents into
  `contents.automatedActions` after checking the tenant's current OpenAPI;
  never POST the intent schema as if it were Sigma code representation.
- Smartsheet row/cell events require an external bridge to invoke a Sigma
  webhook; code representation alone cannot subscribe to Smartsheet events.
- Configure webhook HMAC and API connector credentials through Sigma UI/admin
  surfaces where code representation omits them.
- Approval and update-request workflows require explicit input-table state and
  user-action design. Move-row behavior requires insert-before-delete testing.
- Publish the workbook before automated sequences can run, then verify every
  effect and failure path.

## Execution separation

`build-migration.py` is non-mutating. It emits:

- `input-tables.json`;
- `data-models.json`;
- `workbooks.json`;
- `source-data.json`;
- `dashboard-review.json`;
- `automation-intents.json`;
- `mcp-seed-program.py`;
- `migration-manifest.json`;
- `parity.json`.

The seed program requires assigned page, element, and editable-column IDs plus
a source-bound prequery receipt proving the target has no existing natural
keys. Nonempty targets require a separate reconciliation step; the generated
program refuses to insert into them.

The agent must inspect these artifacts, choose destination connection/folder
IDs, validate against the current Sigma surface, execute through user-provided
Sigma tools, and record live IDs and parity evidence.
