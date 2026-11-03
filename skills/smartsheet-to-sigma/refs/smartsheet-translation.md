# Smartsheet translation

## Canonical source model

Columns use their Smartsheet IDs as immutable identity and titles as display
names. Rows retain:

- `ROW_ID`;
- `PARENT_ROW_ID`;
- zero-based sibling order;
- hierarchy depth;
- expanded/locked flags when returned;
- typed cell values, display values, formulas, object values, and formats.

The source snapshot preserves raw API objects. The intermediate model is a
deterministic projection used for planning and parity.

## Column types

| Smartsheet type | Sigma intent |
|---|---|
| `TEXT_NUMBER` | Text unless all nonblank literal values are numeric |
| `DATE`, `DATETIME`, `ABSTRACT_DATETIME` | Date or timestamp |
| `CHECKBOX` | Boolean/checkbox |
| `PICKLIST` | Text with optional allowed-values validation |
| `MULTI_PICKLIST` | Multi-select input column |
| `CONTACT_LIST` | Text/email; multi-contact values remain structured review data |
| `DURATION`, `PREDECESSOR` | Project semantics; review before flattening |

Always prefer `objectValue` over display text for structured values. Keep the
display value as parity evidence.

## Editability and formulas

A column with literal cells and no formulas is an input-table candidate. A
column with one normalized, supported formula shape is a computed-column
candidate. A column mixing formulas and literals has override semantics and
requires an explicit decision.

System-managed (`systemColumnType` values such as `AUTO_NUMBER` and
created/modified date/by), column-locked, and
cell/row-locked fields are not editable candidates. A formula shape is not
considered uniform when formulas occur in only some rows; partial coverage is a
review item because expanding the formula would change blank or literal rows.

Supported initial expression shapes include scalar arithmetic, comparisons,
boolean expressions, same-row column references, and a limited scalar function
set: `IF`, `IFERROR`, `AND`, `OR`, `NOT`, `ABS`, `ROUND`, and `TODAY`.
Smartsheet's scalar/list semantics for `SUM`, `AVERAGE`, `MIN`, `MAX`, and
`COUNT` do not map directly to Sigma's aggregate functions, so those functions
remain review-required until an AST-based translation is implemented.

The translator intentionally rejects:

- row-number and row-range references whose meaning depends on source order;
- `{Cross Sheet Range}` references;
- `CHILDREN`, `PARENT`, `ANCESTORS`, and `DESCENDANTS`;
- unsupported functions or malformed expressions.

Rejected formulas remain in the model with their computed Smartsheet values and
a review route. They are not frozen silently.

## Hierarchy

Sigma tables are flat. Preserve hierarchy columns and optionally derive a path
or depth presentation column. Do not aggregate parent/child values implicitly:
Smartsheet formulas may already include child totals, and summing both parent
and child rows can double count.

## Reports

Reports are filtered projections over one or more sheets. Use source sheet IDs
to deduplicate the data layer. If report criteria are reconstructable from
captured metadata, plan a derived table with filters/sort/group intent. If not,
plan the returned rows as a snapshot and add `REPORT_LOGIC_REVIEW_REQUIRED`.

Report row limits and inaccessible source sheets are blockers for parity when
the captured row count is incomplete.

## Dashboards

Map the initial supported widget families:

- `TITLE` and `RICHTEXT` → text element;
- `METRIC`, `CHART`, `GRIDGANTT`, and `SHEETSUMMARY` → recognized source
  configuration and parity evidence, but review-required until a native Sigma
  element translator covers the full behavior;
- image, shortcut, and web-content widgets → explicit review.

Dashboard layout coordinates are presentation hints, not exact pixel parity.
Record unsupported widget types and any missing source IDs.

## Workspace order

Flatten nested workspace folders while retaining source paths. Build sheets
first, reports second, and dashboards last. A source sheet referenced by several
reports is represented once in the data layer.

## Automations

Use Smartsheet MCP `get_workflows`, not only the REST automation-rules endpoint.
The MCP result preserves multistep `ROOT_NODE` trigger/action trees and supports
`last_key` pagination. It does not return reminder-type workflows.

The translator preserves the original tree and emits a second typed intent:

- conditions and notifications are code-representation candidates;
- API calls require a pre-created Sigma API connector and credentials;
- row updates and copies target Sigma input tables;
- moves become insert/delete scaffolds with transactional parity;
- approvals and update requests become input-state and notification scaffolds;
- schedules require timezone/cron confirmation;
- row/cell events require a service that forwards the event to a Sigma webhook;
- unknown node types remain source-specific review items.

The final source hash includes the captured workflow trees. Never reuse review
evidence after changing a workflow, event bridge, policy, or generated effect.
