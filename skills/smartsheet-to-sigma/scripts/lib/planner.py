"""Destination-neutral migration plan for Smartsheet snapshots."""
from __future__ import annotations

import hashlib
import json
import re


def build_plan(snapshot: dict, discovery: dict, audit: dict) -> dict:
    discovered = {asset["id"]: asset for asset in discovery.get("assets") or []}
    raw_assets = {asset["id"]: asset for asset in snapshot.get("assets") or []}
    routes = []
    for asset_id in _ordered_asset_ids(discovery):
        asset = discovered[asset_id]
        raw_asset = raw_assets[asset_id]
        if asset["type"] == "sheet":
            routes.append(_sheet_route(asset, raw_asset))
        elif asset["type"] == "report":
            routes.append(_report_route(asset, raw_asset))
        else:
            routes.append(_dashboard_route(asset))

    pending_issues = [
        issue["issueId"]
        for issue in audit.get("issues") or []
        if issue.get("severity") != "blocker"
    ]
    plan = {
        "schemaVersion": 1,
        "kind": "smartsheet-to-sigma-plan",
        "sourceSha256": snapshot["sourceSha256"],
        "scope": snapshot["scope"],
        "source": snapshot.get("source"),
        "auditStatus": audit["status"],
        "pendingReview": pending_issues,
        "pendingReviewCodes": sorted(
            {
                issue["code"]
                for issue in audit.get("issues") or []
                if issue.get("severity") != "blocker"
            }
        ),
        "assets": routes,
        "dependencies": snapshot.get("dependencies") or [],
        "readRefs": [
            "refs/coverage-contract.md",
            "refs/smartsheet-translation.md",
            "refs/sigma-build-gotchas.md",
        ],
        "askUser": _questions(routes),
        "nextCommands": [
            "python scripts/build-migration.py <workdir>/plan.json --out-dir <builddir>"
        ],
        "userHandoffs": [
            "Choose the destination Sigma folder and write-enabled connection.",
            "Review input-table natural keys and mixed input/formula columns.",
            "Execute the reviewed Sigma MCP seed program and record assigned IDs.",
            "Create warehouse views, publish, and assign permissions where required.",
            "Verify live formulas, report rows, and dashboard metrics against parity.json.",
        ],
    }
    plan["planSha256"] = plan_sha256(plan)
    return plan


def plan_sha256(plan: dict) -> str:
    value = {key: item for key, item in plan.items() if key != "planSha256"}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def validate_plan(plan: dict) -> None:
    if plan.get("kind") != "smartsheet-to-sigma-plan":
        raise ValueError("not a smartsheet-to-sigma plan")
    source_hash = plan.get("sourceSha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("plan is missing a valid sourceSha256")
    if plan.get("planSha256") != plan_sha256(plan):
        raise ValueError("planSha256 does not match plan content")
    if not isinstance(plan.get("assets"), list):
        raise ValueError("plan assets must be an array")


def apply_mixed_policy(plan: dict, policy: str) -> dict:
    """Return a re-hashed plan with an explicit mixed-column policy."""
    if policy not in {"override-wins", "formula-wins"}:
        raise ValueError("mixed-column policy must be override-wins or formula-wins")
    updated = json.loads(json.dumps(plan))
    for asset in updated.get("assets") or []:
        by_id = {
            column["sourceColumnId"]: column
            for column in asset.get("computedColumns") or []
        }
        for decision in asset.get("mixedColumnPolicies") or []:
            formula = decision.get("sourceFormula")
            decision["policy"] = policy
            decision["status"] = "selected"
            if policy == "override-wins" and formula:
                decision["finalFormula"] = (
                    f"Coalesce([{decision['overrideColumn']}], {formula})"
                )
            elif policy == "formula-wins":
                decision["finalFormula"] = formula
            target = by_id.get(decision["sourceColumnId"])
            if target is not None:
                target["mixedPolicy"] = policy
                target["overrideColumn"] = decision["overrideColumn"]
                target["finalFormula"] = decision["finalFormula"]
    updated["planSha256"] = plan_sha256(updated)
    return updated


def _sheet_route(asset: dict, raw_asset: dict) -> dict:
    destination_names = _column_names(asset.get("columns") or [])
    editable = [
        _editable_destination_column(column, destination_names[column["id"]])
        for column in asset.get("columns") or []
        if column.get("behavior") in {"editable", "mixed"}
    ]
    computed = [
        {
            **_destination_column(column, destination_names[column["id"]]),
            "sigmaFormula": column.get("sigmaFormula"),
            "supported": bool(column.get("supportedFormula")),
            "sourceFormulaShapes": column.get("formulaShapes") or [],
        }
        for column in asset.get("columns") or []
        if column.get("formulaCount")
    ]
    formula_names = {
        column["title"].casefold(): destination_names[column["id"]]
        for column in asset.get("columns") or []
    }
    for column in computed:
        if column.get("sigmaFormula"):
            column["sigmaFormula"] = _rewrite_formula_columns(
                column["sigmaFormula"],
                formula_names,
            )
    read_only = [
        _destination_column(column, destination_names[column["id"]])
        for column in asset.get("columns") or []
        if column.get("behavior") == "read-only"
    ]
    editable, computed = _unique_route_columns(editable, computed)
    editable_by_source = {
        column["sourceColumnId"]: column for column in editable
    }
    computed_by_source = {
        column["sourceColumnId"]: column for column in computed
    }
    mixed_policies = []
    for column in asset.get("columns") or []:
        if column.get("behavior") != "mixed":
            continue
        editable_column = editable_by_source[column["id"]]
        computed_column = computed_by_source[column["id"]]
        mixed_policies.append(
            {
                "sourceColumnId": column["id"],
                "sourceName": column["title"],
                "overrideColumn": editable_column["name"],
                "computedColumn": computed_column["name"],
                "sourceFormula": computed_column.get("sigmaFormula"),
                "status": "unresolved",
                "policy": None,
                "finalFormula": None,
            }
        )
    return {
        "sourceId": asset["id"],
        "sourceType": "sheet",
        "sourceName": asset["name"],
        "sourcePath": asset.get("path") or "",
        "destinationName": _safe_name(asset["name"]),
        "route": "editable-grid" if editable else "read-only-table",
        "naturalKeys": ["ROW_ID"],
        "systemColumns": _system_columns(),
        "editableColumns": editable,
        "computedColumns": computed,
        "readOnlyColumns": read_only,
        "mixedColumnPolicies": mixed_policies,
        "rows": _source_rows(raw_asset, asset),
        "hierarchy": asset.get("hierarchy") or {},
        "parityTargets": asset.get("parityTargets") or [],
        "reviewCodes": _asset_review_codes(asset),
    }


def _report_route(asset: dict, raw_asset: dict) -> dict:
    destination_names = _column_names(asset.get("columns") or [])
    return {
        "sourceId": asset["id"],
        "sourceType": "report",
        "sourceName": asset["name"],
        "sourcePath": asset.get("path") or "",
        "destinationName": _safe_name(asset["name"]),
        "route": "derived-report" if asset.get("sourceSheetIds") else "report-snapshot",
        "sourceSheetIds": asset.get("sourceSheetIds") or [],
        "sourceWorkspaceIds": asset.get("sourceWorkspaceIds") or [],
        "reportDefinition": asset.get("reportDefinition"),
        "columns": [
            {"name": "SOURCE_SHEET_ID", "type": "text", "role": "lineage"},
            *[
                _destination_column(column, destination_names[column["id"]])
                for column in asset.get("columns") or []
            ],
        ],
        "rows": _source_rows(raw_asset, asset),
        "parityTargets": {
            "rowCount": asset.get("rowCount", 0),
            "formulaCells": asset.get("parityTargets") or [],
        },
        "limitations": [
            "Report criteria require review before the returned rows can be treated as live logic."
        ],
        "reviewCodes": ["REPORT_LOGIC_REVIEW_REQUIRED", *_asset_review_codes(asset)],
    }


def _dashboard_route(asset: dict) -> dict:
    return {
        "sourceId": asset["id"],
        "sourceType": "dashboard",
        "sourceName": asset["name"],
        "sourcePath": asset.get("path") or "",
        "destinationName": _safe_name(asset["name"]),
        "route": "workbook-dashboard",
        "widgets": asset.get("widgets") or [],
        "parityTargets": {
            "widgetCount": asset.get("widgetCount", 0),
            "supportedWidgetCount": sum(
                widget.get("supported") for widget in asset.get("widgets") or []
            ),
        },
        "reviewCodes": (
            ["UNSUPPORTED_DASHBOARD_WIDGET"]
            if asset.get("unsupportedWidgetCount")
            else []
        ),
    }


def _source_rows(raw_asset: dict, discovered_asset: dict) -> list[dict]:
    raw = raw_asset.get("raw") or {}
    report = discovered_asset["type"] == "report"
    columns = {
        str(
            column.get("virtualId")
            if report and column.get("virtualId") is not None
            else column.get("id")
        ): str(
            column.get("title")
            or column.get("virtualId")
            or column.get("id")
        )
        for column in raw.get("columns") or []
    }
    hierarchy = {
        row["rowId"]: row for row in (discovered_asset.get("hierarchy") or {}).get("rows") or []
    }
    output = []
    for index, row in enumerate(raw.get("rows") or []):
        row_id = str(row.get("id"))
        values = {}
        formulas = {}
        for cell in row.get("cells") or []:
            column_id = str(
                cell.get("virtualColumnId")
                if report and cell.get("virtualColumnId") is not None
                else cell.get("columnId")
            )
            title = columns.get(column_id, column_id)
            values[title] = _cell_value(cell)
            if cell.get("formula"):
                formulas[title] = cell["formula"]
        hierarchy_row = hierarchy.get(row_id, {})
        record = {
            "ROW_ID": row_id,
            "PARENT_ROW_ID": hierarchy_row.get("parentRowId"),
            "SIBLING_ORDER": hierarchy_row.get("siblingOrder", index),
            "HIERARCHY_DEPTH": hierarchy_row.get("depth", 0),
            "values": values,
            "formulas": formulas,
        }
        if discovered_asset["type"] == "report":
            source_sheet_id = row.get("sheetId") or row.get("sourceSheetId")
            record["SOURCE_SHEET_ID"] = (
                str(source_sheet_id) if source_sheet_id is not None else None
            )
        output.append(record)
    return output


def _destination_column(column: dict, destination_name: str | None = None) -> dict:
    return {
        "sourceColumnId": column["id"],
        "sourceName": column["title"],
        "name": destination_name or column["title"],
        "type": column["sigmaType"],
        "required": bool(column.get("primary")),
        "options": column.get("options") or [],
        "sourceBehavior": column.get("behavior"),
    }


def _editable_destination_column(column: dict, destination_name: str) -> dict:
    destination = _destination_column(column, destination_name)
    if column.get("behavior") == "mixed":
        destination["name"] = destination["name"] + "_OVERRIDE"
        destination["overrideFor"] = column["title"]
    return destination


def _column_names(columns: list[dict]) -> dict[str, str]:
    reserved = {"row_id", "parent_row_id", "sibling_order", "hierarchy_depth"}
    used: set[str] = set()
    output: dict[str, str] = {}
    for column in columns:
        base = str(column.get("title") or column["id"]).strip() or str(column["id"])
        if base.casefold() in reserved:
            base += "_SOURCE"
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        output[column["id"]] = candidate
    return output


def _unique_route_columns(
    editable: list[dict], computed: list[dict]
) -> tuple[list[dict], list[dict]]:
    used = {column["name"].casefold() for column in computed}
    fixed = []
    for column in editable:
        item = dict(column)
        base = item["name"]
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        item["name"] = candidate
        used.add(candidate.casefold())
        fixed.append(item)
    return fixed, computed


def _system_columns() -> list[dict]:
    return [
        {"name": "ROW_ID", "type": "text", "role": "natural-key"},
        {"name": "PARENT_ROW_ID", "type": "text", "role": "hierarchy"},
        {"name": "SIBLING_ORDER", "type": "number", "role": "hierarchy"},
        {"name": "HIERARCHY_DEPTH", "type": "number", "role": "hierarchy"},
    ]


def _ordered_asset_ids(discovery: dict) -> list[str]:
    assets = discovery.get("assets") or []
    priority = {"sheet": 0, "report": 1, "dashboard": 2}
    by_id = {asset["id"]: asset for asset in assets}
    dependencies = {
        asset_id: {
            str(item["to"])
            for item in discovery.get("dependencies") or []
            if str(item.get("from")) == asset_id and str(item.get("to")) in by_id
        }
        for asset_id in by_id
    }

    def sort_key(asset_id: str):
        item = by_id[asset_id]
        return (
            priority.get(item["type"], 9),
            item.get("path") or "",
            item.get("name") or "",
            asset_id,
        )

    remaining = set(by_id)
    ordered = []
    while remaining:
        ready = sorted(
            [
                asset_id
                for asset_id in remaining
                if not (dependencies.get(asset_id, set()) & remaining)
            ],
            key=sort_key,
        )
        if not ready:
            ordered.extend(sorted(remaining, key=sort_key))
            break
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _asset_review_codes(asset: dict) -> list[str]:
    codes = []
    if asset.get("unsupportedFormulaCount"):
        codes.append("UNSUPPORTED_FORMULA")
    if any(
        column.get("behavior") == "mixed" for column in asset.get("columns") or []
    ):
        codes.append("MIXED_INPUT_FORMULA_COLUMN")
    if asset.get("hierarchy", {}).get("hasHierarchy"):
        codes.append("PROJECT_OR_HIERARCHY_REVIEW_REQUIRED")
    return codes


def _questions(routes: list[dict]) -> list[str]:
    questions = [
        "Which Sigma folder should contain the migration?",
        "Which write-enabled Sigma connection should back editable input tables?",
    ]
    if any(
        column.get("sourceBehavior") == "mixed"
        for route in routes
        for column in route.get("editableColumns") or []
    ):
        questions.append(
            "For mixed literal/formula columns, should literal cells remain explicit overrides?"
        )
    if any(route["sourceType"] == "report" for route in routes):
        questions.append(
            "Should report logic be reconstructed from source sheets or accepted as a one-time snapshot?"
        )
    return questions


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(value).strip()).strip("_")
    return (cleaned or "Smartsheet_Asset")[:120]


def _rewrite_formula_columns(formula: str, names: dict[str, str]) -> str:
    output = []
    index = 0
    in_string = False
    while index < len(formula):
        character = formula[index]
        if character == '"':
            if in_string and index + 1 < len(formula) and formula[index + 1] == '"':
                output.extend(('\"', '\"'))
                index += 2
                continue
            in_string = not in_string
            output.append(character)
            index += 1
            continue
        if not in_string and character == "[":
            closing = formula.find("]", index + 1)
            if closing != -1:
                source_name = formula[index + 1:closing]
                output.append(
                    f"[{names.get(source_name.casefold(), source_name)}]"
                )
                index = closing + 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _cell_value(cell: dict):
    if "objectValue" in cell:
        return cell["objectValue"]
    if "value" in cell:
        return cell["value"]
    return cell.get("displayValue")
