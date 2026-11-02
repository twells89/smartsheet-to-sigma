"""Emit reviewable Sigma build artifacts from a migration plan."""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    from .planner import validate_plan
    from .source_snapshot import write_json
except ImportError:
    from planner import validate_plan
    from source_snapshot import write_json


def build_bundle(plan: dict, out_dir: str | Path, *, gate: dict) -> dict:
    """Write a non-mutating Sigma intent bundle and return its manifest."""
    validate_plan(plan)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    input_tables = _input_tables(plan)
    data_models = _data_models(plan)
    workbooks = _workbooks(plan)
    source_data = _source_data(plan)
    dashboard_review = _dashboard_review(plan)
    parity = _parity(plan)
    manifest = _manifest(
        plan,
        input_tables,
        data_models,
        workbooks,
        source_data,
        dashboard_review,
        parity,
        gate,
    )
    write_json(destination / "input-tables.json", input_tables)
    write_json(destination / "data-models.json", data_models)
    write_json(destination / "workbooks.json", workbooks)
    write_json(destination / "source-data.json", source_data)
    write_json(destination / "dashboard-review.json", dashboard_review)
    write_json(destination / "parity.json", parity)
    write_json(destination / "migration-manifest.json", manifest)
    program_path = destination / "mcp-seed-program.py"
    program_path.write_text(
        _seed_program(input_tables), encoding="utf-8"
    )
    program_path.chmod(0o600)
    return manifest


def _input_tables(plan: dict) -> dict:
    tables = []
    for asset in plan["assets"]:
        if asset["sourceType"] != "sheet" or not asset.get("editableColumns"):
            continue
        editable_names = {
            column["name"]: column.get("sourceName", column["name"])
            for column in asset["editableColumns"]
        }
        rows = []
        for row in asset.get("rows") or []:
            values = {
                destination_name: (row.get("values") or {}).get(source_name)
                for destination_name, source_name in editable_names.items()
                if source_name in (row.get("values") or {})
                and source_name not in (row.get("formulas") or {})
            }
            rows.append(
                {
                    "ROW_ID": row["ROW_ID"],
                    "PARENT_ROW_ID": row.get("PARENT_ROW_ID"),
                    "SIBLING_ORDER": row.get("SIBLING_ORDER"),
                    "HIERARCHY_DEPTH": row.get("HIERARCHY_DEPTH"),
                    **values,
                }
            )
        tables.append(
            {
                "logicalId": f"input_{_identifier(asset['sourceId'])}",
                "name": asset["destinationName"] + "_INPUT",
                "sourceAssetId": asset["sourceId"],
                "connectionId": "${SIGMA_WRITE_CONNECTION_ID}",
                "kind": "empty-input-table",
                "naturalKeys": asset["naturalKeys"],
                "columns": [*asset.get("systemColumns", []), *asset["editableColumns"]],
                "seedRows": rows,
                "requires": [
                    "write-enabled connection",
                    "assigned workbook/element IDs",
                    "natural-key prequery before upsert",
                ],
            }
        )
    return {
        "schemaVersion": 1,
        "intentSchema": "smartsheet-to-sigma/input-tables/v1",
        "sourceSha256": plan["sourceSha256"],
        "tables": tables,
    }


def _data_models(plan: dict) -> dict:
    models = []
    for asset in plan["assets"]:
        if asset["sourceType"] == "sheet":
            models.append(
                {
                    "logicalId": f"model_{_identifier(asset['sourceId'])}",
                    "name": asset["destinationName"] + "_MODEL",
                    "sourceAssetId": asset["sourceId"],
                    "source": (
                        (
                            {
                                "kind": "input-table-with-read-only-snapshot",
                                "editableRelation": (
                                    "${WAREHOUSE_VIEW_"
                                    + _identifier(asset["sourceId"])
                                    + "}"
                                ),
                                "readOnlySourceDataLogicalId": (
                                    f"data_{_identifier(asset['sourceId'])}"
                                ),
                                "join": {
                                    "editableKey": "ROW_ID",
                                    "readOnlyKey": "ROW_ID",
                                    "type": "left",
                                },
                            }
                            if asset.get("readOnlyColumns")
                            else {
                                "kind": "warehouse-view",
                                "relation": (
                                    "${WAREHOUSE_VIEW_"
                                    + _identifier(asset["sourceId"])
                                    + "}"
                                ),
                            }
                        )
                        if asset.get("editableColumns")
                        else {
                            "kind": "captured-snapshot",
                            "sourceDataLogicalId": f"data_{_identifier(asset['sourceId'])}",
                        }
                    ),
                    "columns": [
                        *asset.get("systemColumns", []),
                        *asset.get("editableColumns", []),
                        *asset.get("computedColumns", []),
                        *asset.get("readOnlyColumns", []),
                    ],
                    "computedColumns": asset.get("computedColumns", []),
                }
            )
        elif asset["sourceType"] == "report":
            models.append(
                {
                    "logicalId": f"report_{_identifier(asset['sourceId'])}",
                    "name": asset["destinationName"] + "_REPORT",
                    "sourceAssetId": asset["sourceId"],
                    "sourceSheetIds": asset.get("sourceSheetIds") or [],
                    "sourceWorkspaceIds": asset.get("sourceWorkspaceIds") or [],
                    "reportDefinition": asset.get("reportDefinition"),
                    "route": asset["route"],
                    "columns": asset.get("columns") or [],
                    "limitations": asset.get("limitations") or [],
                    "sourceDataLogicalId": f"data_{_identifier(asset['sourceId'])}",
                }
            )
    return {
        "schemaVersion": 1,
        "intentSchema": "smartsheet-to-sigma/data-models/v1",
        "sourceSha256": plan["sourceSha256"],
        "models": models,
    }


def _workbooks(plan: dict) -> dict:
    pages = []
    elements = []
    layout_pages = []
    for page_index, asset in enumerate(plan["assets"], start=1):
        page_id = f"page_{_identifier(asset['sourceId'])}"
        page_element_ids = []
        if asset["sourceType"] == "sheet":
            element_id = f"table_{_identifier(asset['sourceId'])}"
            elements.append(
                {
                    "id": element_id,
                    "kind": "input-table" if asset.get("editableColumns") else "table",
                    "name": asset["sourceName"],
                    "sourceAssetId": asset["sourceId"],
                    "modelLogicalId": f"model_{_identifier(asset['sourceId'])}",
                    "requiresBinding": True,
                }
            )
            page_element_ids.append(element_id)
        elif asset["sourceType"] == "report":
            element_id = f"report_{_identifier(asset['sourceId'])}"
            elements.append(
                {
                    "id": element_id,
                    "kind": "table",
                    "name": asset["sourceName"],
                    "sourceAssetId": asset["sourceId"],
                    "modelLogicalId": element_id,
                    "requiresBinding": True,
                }
            )
            page_element_ids.append(element_id)
        else:
            for widget in asset.get("widgets") or []:
                if not widget.get("supported"):
                    continue
                element_id = (
                    f"widget_{_identifier(asset['sourceId'])}_{_identifier(widget['id'])}"
                )
                elements.append(
                    {
                        "id": element_id,
                        "kind": widget["sigmaKind"],
                        "name": widget.get("title") or widget["type"],
                        "sourceAssetId": asset["sourceId"],
                        "sourceWidgetId": widget["id"],
                        "sourceIds": widget.get("sourceIds") or [],
                        "sourceConfig": widget.get("contents"),
                        "sourceLayout": widget.get("layout"),
                        "requiresBinding": True,
                    }
                )
                page_element_ids.append(element_id)
        pages.append(
            {
                "id": page_id,
                "name": asset["sourceName"],
                "sourceAssetId": asset["sourceId"],
            }
        )
        layout_pages.append(_layout_page(page_id, page_element_ids, page_index))
    name = str((plan.get("source") or {}).get("name") or "Smartsheet Migration")
    workbook = {
        "name": name,
        "folderId": "${SIGMA_FOLDER_ID}",
        "contents": {
            "schemaVersion": 1,
            "kind": "workbook",
            "pages": pages,
            "elements": elements,
            "layout": '<?xml version="1.0" encoding="utf-8"?>\n'
            + "\n".join(layout_pages),
        },
        "intentOnly": True,
        "requiresCurrentOpenApiValidation": True,
    }
    return {
        "schemaVersion": 1,
        "intentSchema": "smartsheet-to-sigma/workbooks/v1",
        "sourceSha256": plan["sourceSha256"],
        "workbooks": [workbook],
    }


def _layout_page(page_id: str, element_ids: list[str], page_index: int) -> str:
    lines = [
        f'<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" '
        f'gridTemplateRows="auto" id="{page_id}">'
    ]
    cursor = 1
    for element_id in element_ids:
        lines.append(
            f'  <Element elementId="{element_id}" '
            f'gridColumn="1 / 25" gridRow="{cursor} / {cursor + 12}"/>'
        )
        cursor += 12
    lines.append("</Page>")
    return "\n".join(lines)


def _parity(plan: dict) -> dict:
    assets = []
    for asset in plan["assets"]:
        targets = asset.get("parityTargets")
        if asset["sourceType"] == "sheet":
            targets = {
                "rowCount": len(asset.get("rows") or []),
                "formulaCells": targets or [],
            }
        elif asset["sourceType"] == "dashboard":
            targets = {
                **(targets or {}),
                "widgets": [
                    {
                        "id": widget["id"],
                        "type": widget["type"],
                        "supported": widget.get("supported"),
                        "contents": widget.get("contents"),
                    }
                    for widget in asset.get("widgets") or []
                ],
            }
        assets.append(
            {
                "sourceAssetId": asset["sourceId"],
                "sourceType": asset["sourceType"],
                "status": "PENDING_LIVE_CHECK",
                "targets": targets,
            }
        )
    return {
        "schemaVersion": 1,
        "sourceSha256": plan["sourceSha256"],
        "assets": assets,
    }


def _source_data(plan: dict) -> dict:
    return {
        "schemaVersion": 1,
        "intentSchema": "smartsheet-to-sigma/source-data/v1",
        "sourceSha256": plan["sourceSha256"],
        "assets": [
            {
                "logicalId": f"data_{_identifier(asset['sourceId'])}",
                "sourceAssetId": asset["sourceId"],
                "sourceType": asset["sourceType"],
                "rows": asset.get("rows") or [],
                "loadRoute": (
                    (
                        ["input-table-seed", "immutable-snapshot-load"]
                        if asset.get("readOnlyColumns")
                        else ["input-table-seed"]
                    )
                    if asset["sourceType"] == "sheet" and asset.get("editableColumns")
                    else ["reviewed-snapshot-load"]
                ),
            }
            for asset in plan["assets"]
            if asset["sourceType"] in {"sheet", "report"}
        ],
    }


def _dashboard_review(plan: dict) -> dict:
    return {
        "schemaVersion": 1,
        "intentSchema": "smartsheet-to-sigma/dashboard-review/v1",
        "sourceSha256": plan["sourceSha256"],
        "dashboards": [
            {
                "sourceAssetId": asset["sourceId"],
                "sourceName": asset["sourceName"],
                "widgets": asset.get("widgets") or [],
            }
            for asset in plan["assets"]
            if asset["sourceType"] == "dashboard"
        ],
    }


def _manifest(
    plan: dict,
    input_tables: dict,
    data_models: dict,
    workbooks: dict,
    source_data: dict,
    dashboard_review: dict,
    parity: dict,
    gate: dict,
) -> dict:
    return {
        "schemaVersion": 1,
        "sourceSha256": plan["sourceSha256"],
        "planSha256": plan["planSha256"],
        "reviewStatus": gate["status"],
        "pendingReview": gate["pending"],
        "status": "PLANNED",
        "targets": {
            "inputTables": len(input_tables["tables"]),
            "dataModels": len(data_models["models"]),
            "workbooks": len(workbooks["workbooks"]),
            "sourceDataAssets": len(source_data["assets"]),
            "dashboardReviewAssets": len(dashboard_review["dashboards"]),
            "parityAssets": len(parity["assets"]),
        },
        "assets": [
            {
                "sourceAssetId": asset["sourceId"],
                "sourceType": asset["sourceType"],
                "sourceName": asset["sourceName"],
                "route": asset["route"],
                "targetDocumentId": None,
                "targetElementIds": [],
                "parityStatus": "PENDING_LIVE_CHECK",
            }
            for asset in plan["assets"]
        ],
        "userHandoffs": plan.get("userHandoffs") or [],
    }


def _seed_program(input_tables: dict) -> str:
    tables = input_tables["tables"]
    payload = repr(tables)
    targets = {
        table["logicalId"]: {
            "pageId": None,
            "elementId": None,
            "columnIds": {
                column["name"]: None for column in table["columns"]
            },
        }
        for table in tables
    }
    return f'''"""Generated Sigma MCP Code Mode seed program.

Populate TARGETS from creation/readback and PREQUERY_RECEIPTS from a live query.
This program only seeds an empty target; it refuses to insert when the prequery
found existing natural keys. Run reconciliation separately for nonempty targets.
"""

TABLES = {payload}
TARGETS = {targets!r}
PREQUERY_RECEIPTS = {{}}

results = []
for spec in TABLES:
    logical_id = spec["logicalId"]
    target = TARGETS[logical_id]
    receipt = PREQUERY_RECEIPTS.get(logical_id)
    if not receipt or receipt.get("sourceSha256") != {input_tables["sourceSha256"]!r}:
        raise ValueError(f"Add source-bound prequery receipt for {{logical_id}}")
    if receipt.get("existingNaturalKeys") != []:
        raise ValueError(f"Target {{logical_id}} is not empty; reconcile before writing")
    if not target.get("pageId") or not target.get("elementId"):
        raise ValueError(f"Bind pageId and elementId for {{logical_id}}")
    host = sigma.workbook.get_page(target["pageId"])
    table = host.get_element(target["elementId"])
    columns = {{
        name: table.get_column(column_id)
        for name, column_id in target["columnIds"].items()
        if column_id
    }}
    required_names = {{column["name"] for column in spec["columns"]}}
    if set(columns) != required_names:
        raise ValueError(f"Bind every editable column ID for {{logical_id}}")
    rows = [
        {{columns[name]: value for name, value in row.items()}}
        for row in spec["seedRows"]
    ]
    result = table.upsert_rows(rows)
    results.append({{"logicalId": spec["logicalId"], "result": result}})
print(results)
'''


def _identifier(value) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value))
