# Coverage contract

The skill is fail-loud. Every captured Smartsheet feature must be built,
represented as a parity target, or listed as a source-bound review issue.

## Status

- `AUTO`: the snapshot contains only routes supported by the local planner.
- `REVIEW_REQUIRED`: local artifacts can be generated, but named review and
  runtime evidence are required before completion.
- `BLOCKED`: the source is incomplete, inaccessible, malformed, or cannot be
  identified safely.

## Required inventory

For each source asset record:

- stable ID, name, type, owner/access metadata, and modified timestamp;
- typed columns, rows, cell values, formulas, object values, and formatting;
- parent row, sibling order, and hierarchy depth;
- cross-sheet references, report source sheets, or dashboard widget sources;
- summary fields, project/Gantt settings, filters, attachments, discussions,
  proofs, automation rules, and sharing metadata when available;
- API capture errors or features unavailable through the API.

An empty API result is not evidence that a feature does not exist when the API
documents that feature as inaccessible.

## Hard completion gates

1. Snapshot hash in the plan equals the canonical captured source hash.
2. Every source asset has a migration-manifest record.
3. Every editable source grain has a Sigma input route or explicit disposition.
4. Formula routes remain live; cached values are used only for parity.
5. Reports and dashboard metrics are checked against captured values.
6. Every `REVIEW_REQUIRED` issue instance has its own stable `issueId`,
   source-bound owner, disposition, verification, and runtime evidence.
7. `BLOCKED` cannot be overridden by a disposition file.

## Known review surfaces

- cross-sheet and hierarchy-sensitive formulas;
- nonuniform formulas mixed with literal overrides;
- report criteria the API does not expose sufficiently to reconstruct;
- dashboard content/image/web widgets without a native Sigma equivalent;
- attachments, discussions, proofs, update requests, and collaboration history;
- automation rules, especially multistep workflows unavailable through the API;
- Smartsheet sharing, ownership, and publishing settings;
- Gantt dependencies, baselines, resource management, and project settings.

## Security boundary

Smartsheet permissions describe source access; they are not Sigma RLS or CLS.
The skill records source access metadata and requires the destination owner to
choose Sigma permissions and warehouse policies. It never invents security
rules from sharing data.
