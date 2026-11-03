#!/usr/bin/env python3
"""Plan, validate, or merge Smartsheet MCP workflow captures."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from automation_workflows import (  # noqa: E402
    WorkflowCaptureError,
    merge_workflow_capture,
    validate_workflow_capture,
    workflow_capture_plan,
)
from source_snapshot import SnapshotError, load_snapshot, write_json  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "validate", "merge"):
        child = subparsers.add_parser(command)
        child.add_argument("snapshot")
        if command in {"validate", "merge"}:
            child.add_argument("--capture", required=True)
        if command in {"plan", "merge"}:
            child.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot)
        if args.command == "plan":
            result = workflow_capture_plan(snapshot)
            write_json(args.out, result)
            summary = {
                "status": "CAPTURE_PLANNED",
                "baseSourceSha256": result["baseSourceSha256"],
                "calls": len(result["calls"]),
                "out": str(Path(args.out).resolve()),
            }
        else:
            capture = json.loads(Path(args.capture).read_text(encoding="utf-8"))
            if args.command == "validate":
                result = validate_workflow_capture(capture, snapshot)
                summary = {
                    "status": (
                        "CAPTURE_COMPLETE"
                        if result["complete"]
                        else "CAPTURE_INCOMPLETE"
                    ),
                    "baseSourceSha256": result["baseSourceSha256"],
                    "sheets": len(result["sheets"]),
                    "workflows": sum(
                        len(record["workflows"])
                        for record in result["sheets"]
                    ),
                    "missingSheetIds": result["missingSheetIds"],
                }
            else:
                result, normalized = merge_workflow_capture(snapshot, capture)
                write_json(args.out, result)
                summary = {
                    "status": (
                        "CAPTURE_MERGED"
                        if normalized["complete"]
                        else "CAPTURE_MERGED_INCOMPLETE"
                    ),
                    "sourceSha256": result["sourceSha256"],
                    "sheets": len(normalized["sheets"]),
                    "workflows": sum(
                        len(record["workflows"])
                        for record in normalized["sheets"]
                    ),
                    "out": str(Path(args.out).resolve()),
                }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (
        OSError,
        json.JSONDecodeError,
        SnapshotError,
        WorkflowCaptureError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
