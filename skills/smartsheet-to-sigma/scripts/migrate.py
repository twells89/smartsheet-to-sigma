#!/usr/bin/env python3
"""Router for one-time Smartsheet-to-Sigma migration planning."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from planner import build_plan  # noqa: E402
from automation_workflows import (  # noqa: E402
    WorkflowCaptureError,
    embedded_workflow_capture,
    merge_workflow_capture,
    workflow_capture_plan,
)
from smartsheet_auth import SmartsheetAuthError, resolve_access_token  # noqa: E402
from smartsheet_client import SmartsheetApiError, SmartsheetClient  # noqa: E402
from source_audit import audit_snapshot, review_template  # noqa: E402
from source_model import build_discovery  # noqa: E402
from source_snapshot import (  # noqa: E402
    SnapshotError,
    capture_snapshot,
    load_snapshot,
    parse_source,
    write_json,
)


def plan_command(args) -> int:
    if args.snapshot:
        snapshot = load_snapshot(args.snapshot)
        if snapshot["scope"] != args.scope:
            raise SnapshotError(
                f"snapshot scope is {snapshot['scope']}, not requested {args.scope}"
            )
    else:
        source = parse_source(args.source, scope=args.scope, asset_type=args.asset_type)
        client = SmartsheetClient(resolve_access_token())
        snapshot = capture_snapshot(client, source, scope=args.scope)
    capture_supplied = bool(args.automation_workflows)
    normalized_capture = None
    base_snapshot = snapshot
    if capture_supplied:
        capture = json.loads(
            Path(args.automation_workflows).read_text(encoding="utf-8")
        )
        snapshot, normalized_capture = merge_workflow_capture(snapshot, capture)
    sheet_assets = [
        asset for asset in snapshot.get("assets") or []
        if asset.get("type") == "sheet"
    ]
    embedded_complete = not sheet_assets or all(
        ((asset.get("raw") or {}).get("_workflowCapture") or {}).get("status")
        == "complete"
        for asset in sheet_assets
    )
    if not capture_supplied and embedded_complete:
        normalized_capture = embedded_workflow_capture(snapshot)
    capture_available = capture_supplied or embedded_complete

    discovery = build_discovery(snapshot)
    audit = audit_snapshot(snapshot, discovery)
    plan = build_plan(snapshot, discovery, audit)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stale_capture_plan = out_dir / "workflow-capture-plan.json"
    normalized_capture_path = out_dir / "automation-workflows.normalized.json"
    base_snapshot_path = out_dir / "base-source-snapshot.json"
    capture_complete = bool(
        normalized_capture and normalized_capture.get("complete")
    )
    if capture_complete and stale_capture_plan.exists():
        stale_capture_plan.unlink()
    if not capture_available:
        for obsolete in (normalized_capture_path, base_snapshot_path):
            if obsolete.exists():
                obsolete.unlink()
    write_json(out_dir / "source-snapshot.json", snapshot)
    write_json(out_dir / "migration-audit.json", audit)
    write_json(out_dir / "discovery.json", discovery)
    write_json(out_dir / "plan.json", plan)
    if not capture_available:
        write_json(
            out_dir / "workflow-capture-plan.json",
            workflow_capture_plan(snapshot),
        )
    else:
        if not capture_complete:
            write_json(
                out_dir / "workflow-capture-plan.json",
                workflow_capture_plan(base_snapshot),
            )
        if not capture_supplied and base_snapshot_path.exists():
            try:
                existing_base = load_snapshot(base_snapshot_path)
            except SnapshotError:
                base_snapshot_path.unlink()
            else:
                if (
                    existing_base["sourceSha256"]
                    != normalized_capture["baseSourceSha256"]
                ):
                    base_snapshot_path.unlink()
        if capture_supplied:
            write_json(base_snapshot_path, base_snapshot)
        write_json(
            normalized_capture_path,
            normalized_capture,
        )
    template = review_template(audit)
    template["planSha256"] = plan["planSha256"]
    write_json(out_dir / "review.template.json", template)
    result = {
        "auditStatus": audit["status"],
        "sourceSha256": snapshot["sourceSha256"],
        "scope": snapshot["scope"],
        "assets": discovery["summary"]["assets"],
        "readRefs": plan["readRefs"],
        "askUser": plan["askUser"],
        "nextCommands": [
            command.replace("<workdir>", str(out_dir)) for command in plan["nextCommands"]
        ],
        "userHandoffs": plan["userHandoffs"],
        "outDir": str(out_dir.resolve()),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if audit["status"] == "BLOCKED" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="capture, audit, discover, and plan")
    plan.add_argument("source", help="Smartsheet ID or URL (label only with --snapshot)")
    plan.add_argument("--scope", choices=("asset", "workspace"), required=True)
    plan.add_argument(
        "--asset-type",
        choices=("sheet", "report", "dashboard"),
        help="required for bare IDs in asset scope",
    )
    plan.add_argument("--snapshot", help="use an existing canonical snapshot")
    plan.add_argument(
        "--automation-workflows",
        help="merge an agent-materialized Smartsheet MCP get_workflows capture",
    )
    plan.add_argument("--out-dir", required=True)
    plan.set_defaults(handler=plan_command)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (
        OSError,
        json.JSONDecodeError,
        SnapshotError,
        WorkflowCaptureError,
        SmartsheetAuthError,
        SmartsheetApiError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
