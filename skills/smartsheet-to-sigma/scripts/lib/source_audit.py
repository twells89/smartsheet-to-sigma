"""Fail-loud audit for canonical Smartsheet snapshots."""
from __future__ import annotations

import hashlib
import json


def audit_snapshot(snapshot: dict, discovery: dict) -> dict:
    issues: list[dict] = []

    def add(code: str, severity: str, route: str, message: str, evidence=None):
        issues.append(
            {
                "code": code,
                "severity": severity,
                "route": route,
                "message": message,
                "evidence": list(evidence or []),
            }
        )

    if not snapshot.get("sourceSha256"):
        add(
            "SOURCE_IDENTITY_MISSING",
            "blocker",
            "recapture-source",
            "The snapshot has no canonical source identity.",
        )
    if not discovery.get("assets"):
        add(
            "NO_ASSETS_CAPTURED",
            "blocker",
            "check-source-access",
            "No Smartsheet assets were captured.",
        )
    for error in snapshot.get("errors") or []:
        add(
            "SOURCE_CAPTURE_FAILED",
            "blocker",
            "restore-access-and-recapture",
            "One or more workspace assets could not be captured.",
            [
                f"{error.get('assetType')}:{error.get('assetId')}:"
                f"{error.get('status')}"
            ],
        )
    if discovery.get("missingDependencies"):
        add(
            "DEPENDENCY_NOT_CAPTURED",
            "review",
            "capture-or-disposition-dependency",
            "Referenced source assets are outside the captured source bundle.",
            discovery["missingDependencies"],
        )
    if discovery.get("dependencyCycles"):
        add(
            "DEPENDENCY_CYCLE_REVIEW_REQUIRED",
            "review",
            "cycle-aware-build-order",
            "Captured assets contain a dependency cycle.",
            [" -> ".join(cycle) for cycle in discovery["dependencyCycles"]],
        )
    source_shares = (
        ((snapshot.get("source") or {}).get("raw") or {}).get("_shares") or []
    )
    source_share_error = (
        ((snapshot.get("source") or {}).get("raw") or {}).get("_shareError")
    )
    if source_share_error:
        add(
            "FEATURE_INVENTORY_INCOMPLETE",
            "review",
            "manual-source-review",
            "Workspace sharing metadata could not be read.",
            [
                f"workspace-shares:{source_share_error.get('status')}:"
                f"{source_share_error.get('path')}"
            ],
        )
    if source_shares:
        add(
            "SECURITY_MAPPING_REQUIRED",
            "review",
            "destination-permissions-review",
            "Workspace sharing does not map directly to Sigma permissions or RLS.",
            [f"workspaceShares={len(source_shares)}"],
        )

    for asset in discovery.get("assets") or []:
        asset_label = f"{asset['type']}:{asset['id']}"
        if asset.get("featureErrors"):
            add(
                "FEATURE_INVENTORY_INCOMPLETE",
                "review",
                "manual-source-review",
                "Optional Smartsheet feature metadata could not be read.",
                [
                    f"{asset_label}:{item.get('path')}:{item.get('status')}"
                    for item in asset["featureErrors"]
                ],
            )
        if asset.get("unsupportedFormulaCount"):
            reasons = sorted(
                {
                    reason
                    for column in asset.get("columns") or []
                    for reason in column.get("unsupportedFormulaReasons") or []
                }
            )
            add(
                "UNSUPPORTED_FORMULA",
                "review",
                "source-specific-formula-reimplementation",
                "Formula cells use syntax outside the tested translation subset.",
                [asset_label, *reasons],
            )
        mixed = [
            column["title"]
            for column in asset.get("columns") or []
            if column.get("behavior") == "mixed"
        ]
        if mixed:
            add(
                "MIXED_INPUT_FORMULA_COLUMN",
                "review",
                "confirm-override-semantics",
                "Columns mix literal inputs and formulas.",
                [asset_label, *mixed],
            )
        nonuniform = [
            column["title"]
            for column in asset.get("columns") or []
            if column.get("formulaCount") and not column.get("uniformFormula")
        ]
        if nonuniform:
            add(
                "NONUNIFORM_COLUMN_FORMULA",
                "review",
                "row-specific-calculation-review",
                "Formula shape varies within a column.",
                [asset_label, *nonuniform],
            )
        partial_formula = [
            column["title"]
            for column in asset.get("columns") or []
            if column.get("formulaCount")
            and not column.get("formulaCoverageUniform")
            and column.get("behavior") != "mixed"
        ]
        if partial_formula:
            add(
                "PARTIAL_FORMULA_COVERAGE",
                "review",
                "row-specific-formula-coverage-review",
                "A formula appears in only some rows of a computed column.",
                [asset_label, *partial_formula],
            )
        locked = [
            column["title"]
            for column in asset.get("columns") or []
            if column.get("columnLocked")
            or column.get("lockedCellCount")
            or column.get("systemManaged")
        ]
        if locked:
            add(
                "SOURCE_LOCKS_REVIEW_REQUIRED",
                "review",
                "preserve-read-only-semantics",
                "System-managed or locked source fields must not become editable inputs.",
                [asset_label, *locked],
            )
        if asset.get("formulaErrors"):
            add(
                "SOURCE_FORMULA_ERROR",
                "review",
                "resolve-or-accept-source-error",
                "Smartsheet returned formula error values.",
                [
                    f"{asset_label}:{item['rowId']}:{item['columnId']}:{item['value']}"
                    for item in asset["formulaErrors"]
                ],
            )
        features = asset.get("featureCounts") or {}
        collaboration = {
            key: features.get(key, 0)
            for key in ("attachments", "discussions", "proofs")
            if features.get(key)
        }
        if collaboration:
            add(
                "COLLABORATION_CONTENT_REVIEW_REQUIRED",
                "review",
                "archive-link-or-reimplement",
                "Attachments, discussions, or proofs need an explicit destination.",
                [asset_label, *(f"{key}={value}" for key, value in collaboration.items())],
            )
        automation = asset.get("automation") or {}
        capture_status = automation.get("captureStatus")
        if asset["type"] == "sheet" and capture_status == "missing":
            add(
                "AUTOMATION_WORKFLOW_CAPTURE_REQUIRED",
                "review",
                "smartsheet-mcp-get-workflows",
                "Full workflow trees were not captured through Smartsheet MCP.",
                [asset_label, "tool=get_workflows"],
            )
        if asset["type"] == "sheet" and capture_status == "incomplete":
            add(
                "AUTOMATION_CAPTURE_INCOMPLETE",
                "blocker",
                "finish-get-workflows-pagination",
                "Smartsheet MCP workflow pagination is incomplete.",
                [asset_label],
            )
        if "REMINDER_WORKFLOWS_NOT_RETURNED" in (
            automation.get("captureLimitations") or []
        ):
            add(
                "REMINDER_AUTOMATION_MANUAL_REVIEW",
                "review",
                "smartsheet-ui-reminder-inventory",
                "Smartsheet MCP does not return reminder-type workflows.",
                [asset_label],
            )
        for workflow in automation.get("workflows") or []:
            workflow_label = (
                f"{asset_label}:workflow:{workflow['sourceWorkflowId']}:"
                f"{workflow['name']}"
            )
            if workflow["trigger"]["route"] == "external-event-bridge":
                add(
                    "AUTOMATION_EXTERNAL_TRIGGER_BRIDGE",
                    "review",
                    "smartsheet-event-to-sigma-webhook",
                    "A Smartsheet event trigger needs an external Sigma webhook bridge.",
                    [workflow_label, workflow["trigger"]["sourceType"]],
                )
            if workflow["coverage"] == "SCAFFOLD_REQUIRED":
                add(
                    "AUTOMATION_SCAFFOLD_REQUIRED",
                    "review",
                    "sigma-action-scaffold",
                    "The workflow needs a stateful Sigma action scaffold.",
                    [workflow_label],
                )
            elif workflow["coverage"] == "REVIEW_REQUIRED":
                add(
                    "AUTOMATION_EFFECT_REVIEW_REQUIRED",
                    "review",
                    "source-specific-automation",
                    "The workflow contains an effect without a safe automatic route.",
                    [workflow_label],
                )
            else:
                add(
                    "AUTOMATION_INTENT_VALIDATION_REQUIRED",
                    "review",
                    "sigma-code-rep-dry-run-and-runtime-test",
                    "The emitted effect intent requires compilation, live code-rep validation, and parity.",
                    [workflow_label, workflow["coverage"]],
                )
        if (
            capture_status != "complete"
            and (
                features.get("automationRules")
                or any(
                    "automationrules" in str(item.get("path", "")).lower()
                    for item in asset.get("featureErrors") or []
                )
            )
        ):
            add(
                "AUTOMATION_REVIEW_REQUIRED",
                "review",
                "capture-with-smartsheet-mcp",
                "REST-visible automation has not yet been translated from a full workflow tree.",
                [asset_label, f"visibleRules={features.get('automationRules', 0)}"],
            )
        if features.get("shares"):
            add(
                "SECURITY_MAPPING_REQUIRED",
                "review",
                "destination-permissions-review",
                "Smartsheet sharing does not map directly to Sigma permissions or RLS.",
                [asset_label, f"shares={features['shares']}"],
            )
        if features.get("crossSheetReferences"):
            add(
                "CROSS_SHEET_REFERENCE_REVIEW_REQUIRED",
                "review",
                "dependency-and-formula-review",
                "Cross-sheet references require source-specific reconstruction.",
                [asset_label, f"references={features['crossSheetReferences']}"],
            )
        if any((asset.get("projectSettings") or {}).values()) or asset.get(
            "hierarchy", {}
        ).get("hasHierarchy"):
            add(
                "PROJECT_OR_HIERARCHY_REVIEW_REQUIRED",
                "review",
                "hierarchy-and-project-semantics-review",
                "Hierarchy or project settings must be preserved explicitly.",
                [asset_label],
            )
        if asset["type"] in {"sheet", "report"} and not asset.get(
            "captureComplete", True
        ):
            add(
                "SOURCE_ROWS_INCOMPLETE",
                "blocker",
                "recapture-source-rows",
                "Not all source rows were returned by the API.",
                [
                    asset_label,
                    f"captured={asset.get('rowCount')}",
                    f"reported={asset.get('reportedRowCount')}",
                ],
            )
        if asset["type"] == "report":
            if not asset.get("consistencyVerified", True):
                add(
                    "REPORT_CONSISTENCY_UNVERIFIED",
                    "review",
                    "recapture-or-verify-source-versions",
                    "Workspace-scoped report rows could not be version-checked atomically.",
                    [asset_label],
                )
            add(
                "REPORT_LOGIC_REVIEW_REQUIRED",
                "review",
                "reconstruct-or-accept-snapshot",
                "Returned report rows do not necessarily expose every report criterion.",
                [asset_label],
            )
        if asset["type"] == "dashboard" and asset.get("unsupportedWidgetCount"):
            add(
                "UNSUPPORTED_DASHBOARD_WIDGET",
                "review",
                "rebuild-or-disposition-widget",
                "Dashboard widgets without a native route were found.",
                [
                    asset_label,
                    *[
                        f"{widget['id']}:{widget['type']}"
                        for widget in asset.get("widgets") or []
                        if not widget.get("supported")
                    ],
                ],
            )

    issues = _merge_issues(issues)
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    status = "BLOCKED" if blockers else ("REVIEW_REQUIRED" if issues else "AUTO")
    return {
        "schemaVersion": 1,
        "sourceSha256": snapshot.get("sourceSha256"),
        "status": status,
        "issues": issues,
        "summary": {
            "blockers": len(blockers),
            "review": len(issues) - len(blockers),
            **(discovery.get("summary") or {}),
        },
    }


def review_template(audit: dict) -> dict:
    return {
        "sourceSha256": audit.get("sourceSha256"),
        "issues": [
            {
                "issueId": issue["issueId"],
                "code": issue["code"],
                "owner": "",
                "disposition": "",
                "verified": False,
                "evidence": {
                    "status": "pending",
                    "evidenceId": "",
                    "targetId": "",
                },
            }
            for issue in audit.get("issues") or []
            if issue.get("severity") != "blocker"
        ],
    }


def _merge_issues(issues: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for issue in issues:
        key = json.dumps(issue, sort_keys=True, separators=(",", ":"))
        if key not in merged:
            merged[key] = dict(issue)
    output = []
    for issue in merged.values():
        identity = hashlib.sha256(
            json.dumps(
                issue,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        output.append({**issue, "issueId": f"{issue['code']}:{identity}"})
    return sorted(
        output,
        key=lambda issue: (
            0 if issue["severity"] == "blocker" else 1,
            issue["code"],
            issue["issueId"],
        ),
    )
