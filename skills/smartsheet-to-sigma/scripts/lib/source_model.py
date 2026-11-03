"""Build a deterministic discovery model from a Smartsheet snapshot."""
from __future__ import annotations

from collections import Counter, defaultdict

try:
    from .automation_translation import translate_sheet_workflows
    from .formula_translation import translate_formula
except ImportError:
    from automation_translation import translate_sheet_workflows
    from formula_translation import translate_formula


def build_discovery(snapshot: dict) -> dict:
    assets = [_discover_asset(asset) for asset in snapshot.get("assets") or []]
    by_id = {asset["id"]: asset for asset in assets}
    missing_dependencies = sorted(
        {
            dependency["to"]
            for dependency in snapshot.get("dependencies") or []
            if dependency.get("to") not in by_id
            and str(dependency.get("to")) != str((snapshot.get("source") or {}).get("id"))
        }
    )
    return {
        "schemaVersion": 1,
        "sourceSha256": snapshot.get("sourceSha256"),
        "scope": snapshot.get("scope"),
        "source": snapshot.get("source"),
        "assets": assets,
        "dependencies": snapshot.get("dependencies") or [],
        "missingDependencies": missing_dependencies,
        "dependencyCycles": _dependency_cycles(
            set(by_id),
            snapshot.get("dependencies") or [],
        ),
        "summary": {
            "assets": len(assets),
            "sheets": sum(asset["type"] == "sheet" for asset in assets),
            "reports": sum(asset["type"] == "report" for asset in assets),
            "dashboards": sum(asset["type"] == "dashboard" for asset in assets),
            "rows": sum(asset.get("rowCount", 0) for asset in assets),
            "formulas": sum(asset.get("formulaCount", 0) for asset in assets),
            "unsupportedFormulas": sum(
                asset.get("unsupportedFormulaCount", 0) for asset in assets
            ),
        },
    }


def _discover_asset(asset: dict) -> dict:
    if asset["type"] == "sheet":
        return _discover_grid(asset, report=False)
    if asset["type"] == "report":
        return _discover_grid(asset, report=True)
    return _discover_dashboard(asset)


def _discover_grid(asset: dict, *, report: bool) -> dict:
    raw = asset["raw"]
    columns = raw.get("columns") or []
    rows = raw.get("rows") or []
    column_by_id = {
        _source_column_id(column, report=report): column for column in columns
    }
    formulas: dict[str, list[dict]] = defaultdict(list)
    literal_counts: Counter = Counter()
    blank_counts: Counter = Counter()
    locked_counts: Counter = Counter()
    error_cells: list[dict] = []
    parity_targets: list[dict] = []
    hierarchy = _hierarchy(rows)

    for row in rows:
        row_id = str(row.get("id"))
        for cell in row.get("cells") or []:
            column_id = _source_cell_column_id(cell, report=report)
            if row.get("locked") or cell.get("locked"):
                locked_counts[column_id] += 1
            formula = cell.get("formula")
            if formula:
                translation = translate_formula(formula)
                record = {
                    **translation,
                    "rowId": row_id,
                    "columnId": column_id,
                    "expected": _cell_value(cell),
                }
                formulas[column_id].append(record)
                parity_targets.append(
                    {
                        "assetId": asset["id"],
                        "rowId": row_id,
                        "columnId": column_id,
                        "expected": _cell_value(cell),
                        "formula": formula,
                    }
                )
            elif _has_value(cell):
                literal_counts[column_id] += 1
            else:
                blank_counts[column_id] += 1
            value = _cell_value(cell)
            if isinstance(value, str) and value.startswith("#"):
                error_cells.append(
                    {
                        "rowId": row_id,
                        "columnId": column_id,
                        "value": value,
                    }
                )

    discovered_columns = []
    for position, column in enumerate(columns):
        column_id = _source_column_id(column, report=report)
        formula_records = formulas[column_id]
        supported = [record for record in formula_records if record["supported"]]
        unsupported = [record for record in formula_records if not record["supported"]]
        shapes = sorted({record["normalized"] for record in formula_records})
        sigma_shapes = sorted(
            {record["sigma"] for record in supported if record.get("sigma")}
        )
        literals = literal_counts[column_id]
        formula_count = len(formula_records)
        system_column_type = str(column.get("systemColumnType") or "").upper()
        system_managed = system_column_type in {
            "AUTO_NUMBER",
            "CREATED_DATE",
            "MODIFIED_DATE",
            "CREATED_BY",
            "MODIFIED_BY",
        }
        column_locked = bool(column.get("locked"))
        partial_formula = bool(formula_count) and formula_count != len(rows)
        if formula_count and literals:
            behavior = "mixed"
        elif formula_count:
            behavior = "computed"
        elif system_managed or column_locked:
            behavior = "read-only"
        else:
            behavior = "editable"
        discovered_columns.append(
            {
                "id": column_id,
                "title": str(column.get("title") or column_id),
                "position": position,
                "primary": bool(column.get("primary")),
                "smartsheetType": column.get("type"),
                "systemColumnType": column.get("systemColumnType"),
                "sigmaType": _sigma_type(column, rows, report=report),
                "options": column.get("options") or [],
                "behavior": behavior,
                "systemManaged": system_managed,
                "columnLocked": column_locked,
                "lockedCellCount": locked_counts[column_id],
                "literalCount": literals,
                "formulaCount": formula_count,
                "blankCount": blank_counts[column_id],
                "formulaShapes": shapes,
                "uniformFormula": len(shapes) == 1,
                "formulaCoverageUniform": not partial_formula,
                "supportedFormula": bool(formula_count)
                and not unsupported
                and len(sigma_shapes) == 1
                and not partial_formula,
                "sigmaFormula": sigma_shapes[0] if len(sigma_shapes) == 1 else None,
                "unsupportedFormulaReasons": sorted(
                    {record["reason"] for record in unsupported}
                ),
            }
        )

    feature_counts = {
        "attachments": _count_nested(raw, "attachments"),
        "discussions": _count_nested(raw, "discussions"),
        "proofs": _count_nested(raw, "proofs") + _count_nested(raw, "proof"),
        "automationRules": len(raw.get("_automationRules") or []),
        "automationWorkflows": len(raw.get("_workflows") or []),
        "shares": len(raw.get("_shares") or []),
        "summaryFields": len(raw.get("summary", {}).get("fields", []) or [])
        if isinstance(raw.get("summary"), dict)
        else len(raw.get("summaryFields") or []),
        "crossSheetReferences": len(raw.get("crossSheetReferences") or []),
        "filters": len(raw.get("filters") or raw.get("filterDefinitions") or []),
    }
    automation = (
        translate_sheet_workflows(
            asset["id"],
            raw.get("_workflows") or [],
            capture=raw.get("_workflowCapture"),
        )
        if not report
        else None
    )
    return {
        "id": asset["id"],
        "type": asset["type"],
        "name": asset["name"],
        "path": asset.get("path") or "",
        "access": _access_metadata(asset),
        "rowCount": len(rows),
        "reportedRowCount": raw.get("totalRowCount", len(rows)),
        "captureComplete": raw.get("_captureComplete", True),
        "consistencyVerified": raw.get("_consistencyVerified", True),
        "columnCount": len(columns),
        "columns": discovered_columns,
        "hierarchy": hierarchy,
        "formulaCount": sum(len(items) for items in formulas.values()),
        "unsupportedFormulaCount": sum(
            not record["supported"] for items in formulas.values() for record in items
        ),
        "formulaErrors": error_cells,
        "parityTargets": parity_targets,
        "featureCounts": feature_counts,
        "automation": automation,
        "projectSettings": {
            key: raw.get(key)
            for key in ("ganttEnabled", "dependenciesEnabled", "resourceManagementEnabled")
            if key in raw
        },
        "sourceSheetIds": sorted(
            {
                str(item.get("id") if isinstance(item, dict) else item)
                for item in raw.get("sourceSheets") or []
            }
            | {
                str(item.get("assetId"))
                for item in raw.get("_scope") or []
                if isinstance(item, dict)
                and str(item.get("assetType") or "").lower() == "sheet"
                and item.get("assetId") is not None
            }
        ),
        "sourceWorkspaceIds": sorted(
            {
                str(item.get("assetId"))
                for item in raw.get("_scope") or []
                if isinstance(item, dict)
                and str(item.get("assetType") or "").lower() == "workspace"
                and item.get("assetId") is not None
            }
        ),
        "reportDefinition": raw.get("_definition") if report else None,
        "featureErrors": asset.get("featureErrors") or [],
        "columnIds": sorted(column_by_id),
    }


def _discover_dashboard(asset: dict) -> dict:
    raw = asset["raw"]
    widgets = raw.get("widgets") or []
    discovered = []
    supported_types = {"TITLE": "text", "RICHTEXT": "text"}
    recognized_types = {
        "CHART",
        "GRIDGANTT",
        "IMAGE",
        "METRIC",
        "RICHTEXT",
        "SHEETSUMMARY",
        "SHORTCUT",
        "SHORTCUTICON",
        "SHORTCUTLIST",
        "TITLE",
        "WEBCONTENT",
    }
    for index, widget in enumerate(widgets):
        widget_type = str(
            widget.get("type")
            or widget.get("widgetType")
            or widget.get("contents", {}).get("type")
            or "UNKNOWN"
        ).upper()
        discovered.append(
            {
                "id": str(widget.get("id") or index),
                "type": widget_type,
                "recognized": widget_type in recognized_types,
                "supported": widget_type in supported_types,
                "sigmaKind": supported_types.get(widget_type),
                "title": widget.get("title") or widget.get("name"),
                "sourceIds": sorted(
                    _dashboard_data_source_ids(
                        widget_type,
                        widget.get("contents") or {},
                    )
                ),
                "layout": widget.get("layout")
                or widget.get("position")
                or {
                    key: widget.get(key)
                    for key in ("xPosition", "yPosition", "width", "height")
                    if key in widget
                },
                "contents": widget.get("contents"),
            }
        )
    return {
        "id": asset["id"],
        "type": "dashboard",
        "name": asset["name"],
        "path": asset.get("path") or "",
        "access": _access_metadata(asset),
        "widgetCount": len(discovered),
        "widgets": discovered,
        "unsupportedWidgetCount": sum(not widget["supported"] for widget in discovered),
        "rowCount": 0,
        "formulaCount": 0,
        "unsupportedFormulaCount": 0,
        "featureErrors": asset.get("featureErrors") or [],
    }


def _hierarchy(rows: list[dict]) -> dict:
    parent_by_id = {
        str(row.get("id")): str(row["parentId"]) if row.get("parentId") is not None else None
        for row in rows
    }

    def depth(row_id: str) -> int:
        seen = {row_id}
        current = parent_by_id.get(row_id)
        level = 0
        while current is not None and current not in seen:
            seen.add(current)
            level += 1
            current = parent_by_id.get(current)
        return level

    sibling_counts: Counter = Counter()
    hierarchy_rows = []
    for row in rows:
        row_id = str(row.get("id"))
        parent = str(row["parentId"]) if row.get("parentId") is not None else None
        sibling_order = sibling_counts[parent]
        sibling_counts[parent] += 1
        hierarchy_rows.append(
            {
                "rowId": row_id,
                "parentRowId": parent,
                "siblingOrder": sibling_order,
                "depth": depth(row_id),
            }
        )
    return {
        "hasHierarchy": any(parent is not None for parent in parent_by_id.values()),
        "maxDepth": max((depth(row_id) for row_id in parent_by_id), default=0),
        "rows": hierarchy_rows,
    }


def _cell_value(cell: dict):
    if "objectValue" in cell:
        return cell["objectValue"]
    if "value" in cell:
        return cell["value"]
    return cell.get("displayValue")


def _has_value(cell: dict) -> bool:
    value = _cell_value(cell)
    return value is not None and value != ""


def _sigma_type(column: dict, rows: list[dict], *, report: bool) -> str:
    source_type = str(column.get("type") or "TEXT_NUMBER").upper()
    system_type = str(column.get("systemColumnType") or "").upper()
    mapped = {
        "DATE": "date",
        "DATETIME": "timestamp",
        "ABSTRACT_DATETIME": "timestamp",
        "CHECKBOX": "boolean",
        "MULTI_PICKLIST": "multi-select",
        "PICKLIST": "text",
        "CONTACT_LIST": "text",
        "DURATION": "text",
        "PREDECESSOR": "text",
        "AUTO_NUMBER": "text",
        "CREATED_DATE": "timestamp",
        "MODIFIED_DATE": "timestamp",
        "CREATED_BY": "text",
        "MODIFIED_BY": "text",
    }
    if source_type in mapped:
        return mapped[source_type]
    if system_type in mapped:
        return mapped[system_type]
    column_id = _source_column_id(column, report=report)
    values = [
        _cell_value(cell)
        for row in rows
        for cell in row.get("cells") or []
        if _source_cell_column_id(cell, report=report) == column_id and _has_value(cell)
    ]
    if values and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        return "number"
    return "text"


def _count_nested(value, key: str) -> int:
    if isinstance(value, dict):
        found = value.get(key)
        count = len(found) if isinstance(found, list) else (1 if isinstance(found, dict) else 0)
        return count + sum(_count_nested(item, key) for name, item in value.items() if name != key)
    if isinstance(value, list):
        return sum(_count_nested(item, key) for item in value)
    return 0


def _source_column_id(column: dict, *, report: bool) -> str:
    value = column.get("virtualId") if report else column.get("id")
    if value is None:
        value = column.get("id") if report else column.get("virtualId")
    return str(value)


def _source_cell_column_id(cell: dict, *, report: bool) -> str:
    value = cell.get("virtualColumnId") if report else cell.get("columnId")
    if value is None:
        value = cell.get("columnId") if report else cell.get("virtualColumnId")
    return str(value)


def _dashboard_data_source_ids(widget_type: str, contents: dict) -> set[str]:
    keys_by_type = {
        "CHART": ("sheetId", "reportId"),
        "GRIDGANTT": ("reportId",),
        "METRIC": ("sheetId", "reportId"),
        "SHEETSUMMARY": ("sheetId",),
    }
    return {
        str(contents[key])
        for key in keys_by_type.get(widget_type, ())
        if contents.get(key) is not None
    }


def _access_metadata(asset: dict) -> dict:
    raw = asset.get("raw") or {}
    inventory = asset.get("inventory") or {}
    return {
        key: value
        for key, value in {
            "accessLevel": raw.get("accessLevel") or inventory.get("accessLevel"),
            "ownerInfo": raw.get("ownerInfo") or inventory.get("ownerInfo"),
            "createdAt": raw.get("createdAt") or inventory.get("createdAt"),
            "modifiedAt": raw.get("modifiedAt") or inventory.get("modifiedAt"),
        }.items()
        if value is not None
    }


def _dependency_cycles(asset_ids: set[str], dependencies: list[dict]) -> list[list[str]]:
    graph = {
        asset_id: {
            str(item["to"])
            for item in dependencies
            if str(item.get("from")) == asset_id and str(item.get("to")) in asset_ids
        }
        for asset_id in asset_ids
    }
    cycles: set[tuple[str, ...]] = set()

    def visit(node: str, path: list[str]):
        if node in path:
            cycle = path[path.index(node):]
            rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
            cycles.add(min(rotations))
            return
        for target in graph.get(node, set()):
            visit(target, [*path, node])

    for asset_id in sorted(asset_ids):
        visit(asset_id, [])
    return [list(cycle) for cycle in sorted(cycles)]
