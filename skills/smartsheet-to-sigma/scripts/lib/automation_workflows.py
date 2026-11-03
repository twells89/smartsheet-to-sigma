"""Smartsheet MCP workflow capture contracts and snapshot merge."""
from __future__ import annotations

import copy
import hashlib
import json
import re

try:
    from .source_snapshot import finalize_snapshot
except ImportError:
    from source_snapshot import finalize_snapshot


CAPTURE_KIND = "smartsheet-mcp-workflows"
SCHEMA_VERSION = 1
MCP_TOOL = "get_workflows"
MCP_LIMITATIONS = ["REMINDER_WORKFLOWS_NOT_RETURNED"]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkflowCaptureError(RuntimeError):
    """A workflow capture is malformed or bound to another source."""


def workflow_capture_plan(snapshot: dict) -> dict:
    """Return paginated Smartsheet MCP calls needed for every captured sheet."""
    sheets = [
        {
            "sheetId": str(asset["id"]),
            "sheetName": asset["name"],
            "path": asset.get("path") or "",
        }
        for asset in snapshot.get("assets") or []
        if asset.get("type") == "sheet"
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "smartsheet-mcp-workflow-capture-plan",
        "baseSourceSha256": snapshot.get("sourceSha256"),
        "tool": MCP_TOOL,
        "calls": [
            {
                "sheetId": sheet["sheetId"],
                "sheetName": sheet["sheetName"],
                "path": sheet["path"],
                "arguments": {
                    "sheet_id": int(sheet["sheetId"]),
                    "max_items": 100,
                },
                "pagination": {
                    "responseCursor": "last_key",
                    "requestCursor": "last_key",
                    "until": "last_key is absent",
                },
            }
            for sheet in sheets
        ],
        "outputContract": {
            "schemaVersion": SCHEMA_VERSION,
            "kind": CAPTURE_KIND,
            "baseSourceSha256": snapshot.get("sourceSha256"),
            "sheets": [
                {
                    "sheetId": sheet["sheetId"],
                    "complete": False,
                    "workflows": [],
                    "pages": [],
                    "limitations": list(MCP_LIMITATIONS),
                }
                for sheet in sheets
            ],
        },
    }


def validate_workflow_capture(capture: dict, snapshot: dict) -> dict:
    """Validate and normalize an agent-materialized ``get_workflows`` capture."""
    if not isinstance(capture, dict):
        raise WorkflowCaptureError("workflow capture must be a JSON object")
    if capture.get("schemaVersion") != SCHEMA_VERSION:
        raise WorkflowCaptureError(
            f"workflow capture schemaVersion must be {SCHEMA_VERSION}"
        )
    if capture.get("kind") != CAPTURE_KIND:
        raise WorkflowCaptureError(f"workflow capture kind must be {CAPTURE_KIND}")
    if capture.get("baseSourceSha256") != snapshot.get("sourceSha256"):
        raise WorkflowCaptureError(
            "workflow capture baseSourceSha256 does not match the source snapshot"
        )
    records = capture.get("sheets")
    if not isinstance(records, list):
        raise WorkflowCaptureError("workflow capture sheets must be an array")
    expected = {
        str(asset["id"])
        for asset in snapshot.get("assets") or []
        if asset.get("type") == "sheet"
    }
    normalized = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("sheetId") is None:
            raise WorkflowCaptureError("workflow capture has an invalid sheet record")
        sheet_id = str(record["sheetId"])
        if sheet_id not in expected:
            raise WorkflowCaptureError(
                f"workflow capture references unknown sheet {sheet_id}"
            )
        if sheet_id in seen:
            raise WorkflowCaptureError(
                f"workflow capture repeats sheet {sheet_id}"
            )
        seen.add(sheet_id)
        workflows = record.get("workflows")
        if not isinstance(workflows, list):
            raise WorkflowCaptureError(
                f"workflow capture for sheet {sheet_id} needs a workflows array"
            )
        normalized_workflows = [
            _validate_workflow(workflow, sheet_id) for workflow in workflows
        ]
        complete = record.get("complete") is True
        pages = _validate_page_chain(
            normalized_workflows,
            record.get("pages"),
            complete=complete,
            sheet_id=sheet_id,
        )
        limitations = sorted(
            set(record.get("limitations") or []) | set(MCP_LIMITATIONS)
        )
        normalized.append(
            {
                "sheetId": sheet_id,
                "complete": complete,
                "workflows": normalized_workflows,
                "pages": pages,
                "limitations": limitations,
            }
        )
    missing = sorted(expected - seen)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": CAPTURE_KIND,
        "baseSourceSha256": snapshot["sourceSha256"],
        "complete": not missing
        and all(record["complete"] for record in normalized),
        "missingSheetIds": missing,
        "sheets": sorted(normalized, key=lambda record: record["sheetId"]),
    }


def merge_workflow_capture(snapshot: dict, capture: dict) -> tuple[dict, dict]:
    """Bind full workflow trees into sheet assets and refresh source identity."""
    normalized = validate_workflow_capture(capture, snapshot)
    records = {record["sheetId"]: record for record in normalized["sheets"]}
    merged = copy.deepcopy(snapshot)
    merged.pop("sourceSha256", None)
    for asset in merged.get("assets") or []:
        if asset.get("type") != "sheet":
            continue
        record = records.get(str(asset["id"]))
        if record is None:
            asset["raw"]["_workflowCapture"] = {
                "status": "incomplete",
                "tool": MCP_TOOL,
                "limitations": list(MCP_LIMITATIONS),
                "pageCount": 0,
                "baseSourceSha256": normalized["baseSourceSha256"],
                "pages": [],
                "evidenceSha256": None,
            }
            asset["raw"]["_workflows"] = []
            continue
        asset["raw"]["_workflowCapture"] = {
            "status": "complete" if record["complete"] else "incomplete",
            "tool": MCP_TOOL,
            "limitations": record["limitations"],
            "pageCount": len(record["pages"]),
            "baseSourceSha256": normalized["baseSourceSha256"],
            "pages": record["pages"],
            "evidenceSha256": workflow_evidence_sha256(
                {
                    **record,
                    "baseSourceSha256": normalized["baseSourceSha256"],
                }
            ),
        }
        asset["raw"]["_workflows"] = record["workflows"]
    return finalize_snapshot(merged), normalized


def validate_embedded_workflow_capture(snapshot: dict) -> None:
    """Revalidate cursor evidence embedded in an enriched source snapshot."""
    base_hashes: set[str] = set()
    for asset in snapshot.get("assets") or []:
        if asset.get("type") != "sheet":
            continue
        raw = asset.get("raw") or {}
        metadata = raw.get("_workflowCapture")
        if not isinstance(metadata, dict) or metadata.get("status") != "complete":
            raise WorkflowCaptureError(
                f"sheet {asset.get('id')} lacks complete embedded workflow capture"
            )
        base_hash = metadata.get("baseSourceSha256")
        if not isinstance(base_hash, str) or not SHA256_RE.fullmatch(base_hash):
            raise WorkflowCaptureError(
                f"sheet {asset.get('id')} workflow capture lacks base source identity"
            )
        base_hashes.add(base_hash)
        workflows = [
            _validate_workflow(workflow, str(asset["id"]))
            for workflow in raw.get("_workflows") or []
        ]
        pages = _validate_page_chain(
            workflows,
            metadata.get("pages"),
            complete=True,
            sheet_id=str(asset["id"]),
        )
        record = {
            "sheetId": str(asset["id"]),
            "complete": True,
            "workflows": workflows,
            "pages": pages,
            "limitations": sorted(metadata.get("limitations") or []),
            "baseSourceSha256": base_hash,
        }
        if metadata.get("evidenceSha256") != workflow_evidence_sha256(record):
            raise WorkflowCaptureError(
                f"sheet {asset.get('id')} workflow capture evidence hash is stale"
            )
    if base_hashes:
        if len(base_hashes) != 1:
            raise WorkflowCaptureError(
                "embedded workflow captures do not share one base source identity"
            )
        base_snapshot = copy.deepcopy(snapshot)
        base_snapshot.pop("sourceSha256", None)
        for asset in base_snapshot.get("assets") or []:
            raw = asset.get("raw") or {}
            raw.pop("_workflows", None)
            raw.pop("_workflowCapture", None)
        if finalize_snapshot(base_snapshot)["sourceSha256"] != next(iter(base_hashes)):
            raise WorkflowCaptureError(
                "embedded workflow capture baseSourceSha256 does not match source content"
            )


def embedded_workflow_capture(snapshot: dict) -> dict:
    """Reconstruct the normalized evidence contract from an enriched snapshot."""
    validate_embedded_workflow_capture(snapshot)
    records = []
    base_hashes = set()
    for asset in snapshot.get("assets") or []:
        if asset.get("type") != "sheet":
            continue
        raw = asset["raw"]
        metadata = raw["_workflowCapture"]
        base_hashes.add(metadata["baseSourceSha256"])
        records.append(
            {
                "sheetId": str(asset["id"]),
                "complete": True,
                "workflows": copy.deepcopy(raw.get("_workflows") or []),
                "pages": copy.deepcopy(metadata["pages"]),
                "limitations": sorted(metadata.get("limitations") or []),
            }
        )
    if not records:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": CAPTURE_KIND,
            "baseSourceSha256": snapshot.get("sourceSha256"),
            "complete": True,
            "missingSheetIds": [],
            "sheets": [],
        }
    if len(base_hashes) != 1:
        raise WorkflowCaptureError(
            "embedded workflow captures do not share one base source identity"
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": CAPTURE_KIND,
        "baseSourceSha256": next(iter(base_hashes)),
        "complete": True,
        "missingSheetIds": [],
        "sheets": sorted(records, key=lambda record: record["sheetId"]),
    }


def workflow_evidence_sha256(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "sheetId": str(record["sheetId"]),
                "complete": record.get("complete") is True,
                "workflows": record.get("workflows") or [],
                "pages": record.get("pages") or [],
                "limitations": sorted(record.get("limitations") or []),
                "baseSourceSha256": record.get("baseSourceSha256"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_workflow(workflow: dict, sheet_id: str) -> dict:
    if not isinstance(workflow, dict):
        raise WorkflowCaptureError(
            f"workflow capture for sheet {sheet_id} contains a non-object workflow"
        )
    if workflow.get("id") is None:
        raise WorkflowCaptureError(
            f"workflow on sheet {sheet_id} needs its Smartsheet workflow ID"
        )
    if not isinstance(workflow.get("enabled"), bool):
        raise WorkflowCaptureError(
            f"workflow {workflow.get('id')} on sheet {sheet_id} needs boolean enabled"
        )
    if (
        not isinstance(workflow.get("frequency"), str)
        or not workflow["frequency"].strip()
    ):
        raise WorkflowCaptureError(
            f"workflow {workflow.get('id')} on sheet {sheet_id} needs frequency"
        )
    root = workflow.get("root_node")
    if root is None:
        root = workflow.get("rootNode")
    if (
        not isinstance(root, dict)
        or str(root.get("type") or "").upper() != "ROOT_NODE"
        or not isinstance(root.get("trigger"), dict)
        or not isinstance(root.get("childNode"), dict)
    ):
        raise WorkflowCaptureError(
            f"workflow on sheet {sheet_id} needs ROOT_NODE trigger and childNode"
        )
    name = workflow.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowCaptureError(
            f"workflow on sheet {sheet_id} needs a nonempty name"
        )
    output = copy.deepcopy(workflow)
    output["name"] = name.strip()
    output["root_node"] = copy.deepcopy(root)
    output.pop("rootNode", None)
    output["frequency"] = output["frequency"].strip()
    return output


def _validate_page_chain(
    workflows: list[dict],
    pages,
    *,
    complete: bool,
    sheet_id: str,
) -> list[dict]:
    if not isinstance(pages, list) or not pages:
        raise WorkflowCaptureError(
            f"workflow capture for sheet {sheet_id} needs page cursor evidence"
        )
    normalized = []
    expected_request = None
    seen_response_cursors: set[str] = set()
    captured_ids: list[str] = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise WorkflowCaptureError(
                f"workflow capture page {index} for sheet {sheet_id} is invalid"
            )
        request_cursor = page.get("request_last_key")
        response_cursor = page.get("response_last_key")
        if request_cursor not in (None, ""):
            request_cursor = str(request_cursor)
        if request_cursor != expected_request:
            raise WorkflowCaptureError(
                f"workflow capture page {index} for sheet {sheet_id} breaks last_key chain"
            )
        is_final = index == len(pages) - 1
        if not is_final and response_cursor in (None, ""):
            raise WorkflowCaptureError(
                f"workflow capture page {index} for sheet {sheet_id} terminates before final page"
            )
        if response_cursor not in (None, ""):
            response_cursor = str(response_cursor)
            if response_cursor in seen_response_cursors:
                raise WorkflowCaptureError(
                    f"workflow capture for sheet {sheet_id} repeats last_key"
                )
            seen_response_cursors.add(response_cursor)
        workflow_ids = page.get("workflow_ids")
        if not isinstance(workflow_ids, list):
            raise WorkflowCaptureError(
                f"workflow capture page {index} for sheet {sheet_id} needs workflow_ids"
            )
        ids = [str(workflow_id) for workflow_id in workflow_ids]
        captured_ids.extend(ids)
        normalized.append(
            {
                "request_last_key": request_cursor,
                "response_last_key": response_cursor,
                "workflow_ids": ids,
            }
        )
        expected_request = response_cursor
    if complete and normalized[-1]["response_last_key"] not in (None, ""):
        raise WorkflowCaptureError(
            f"workflow capture for sheet {sheet_id} is complete before last_key ended"
        )
    if not complete and normalized[-1]["response_last_key"] in (None, ""):
        raise WorkflowCaptureError(
            f"workflow capture for sheet {sheet_id} is incomplete but pagination ended"
        )
    actual_ids = [str(workflow["id"]) for workflow in workflows]
    if captured_ids != actual_ids:
        raise WorkflowCaptureError(
            f"workflow capture page IDs for sheet {sheet_id} do not match workflows"
        )
    if len(actual_ids) != len(set(actual_ids)):
        raise WorkflowCaptureError(
            f"workflow capture for sheet {sheet_id} contains duplicate workflow IDs"
        )
    if actual_ids != sorted(actual_ids, key=_workflow_id_key):
        raise WorkflowCaptureError(
            f"workflow capture for sheet {sheet_id} is not ordered by workflow ID"
        )
    return normalized


def _workflow_id_key(value: str):
    return (0, int(value)) if value.isdigit() else (1, value)
