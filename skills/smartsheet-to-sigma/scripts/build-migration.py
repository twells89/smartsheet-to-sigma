#!/usr/bin/env python3
"""Emit a non-mutating Sigma build bundle from a Smartsheet migration plan."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from migration_gate import MigrationGateError, gate_plan  # noqa: E402
from planner import apply_mixed_policy, validate_plan  # noqa: E402
from sigma_builder import build_bundle  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source-snapshot")
    parser.add_argument("--review-dispositions")
    parser.add_argument(
        "--mixed-policy",
        choices=("override-wins", "formula-wins"),
        help="required for production when source columns mix formulas and literals",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="require complete source-bound review dispositions",
    )
    args = parser.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        validate_plan(plan)
        if args.mixed_policy:
            plan = apply_mixed_policy(plan, args.mixed_policy)
        gate = gate_plan(
            plan,
            snapshot_path=args.source_snapshot,
            dispositions_path=args.review_dispositions,
            production=args.production,
        )
        manifest = build_bundle(plan, args.out_dir, gate=gate)
    except (OSError, json.JSONDecodeError, ValueError, MigrationGateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "reviewStatus": manifest["reviewStatus"],
                "pendingReview": manifest["pendingReview"],
                "targets": manifest["targets"],
                "outDir": str(Path(args.out_dir).resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
