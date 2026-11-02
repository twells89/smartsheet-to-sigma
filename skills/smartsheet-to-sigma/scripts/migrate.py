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

    discovery = build_discovery(snapshot)
    audit = audit_snapshot(snapshot, discovery)
    plan = build_plan(snapshot, discovery, audit)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "source-snapshot.json", snapshot)
    write_json(out_dir / "migration-audit.json", audit)
    write_json(out_dir / "discovery.json", discovery)
    write_json(out_dir / "plan.json", plan)
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
    plan.add_argument("--out-dir", required=True)
    plan.set_defaults(handler=plan_command)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (SnapshotError, SmartsheetAuthError, SmartsheetApiError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
