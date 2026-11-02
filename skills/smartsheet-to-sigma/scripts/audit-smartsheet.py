#!/usr/bin/env python3
"""Audit a canonical Smartsheet source snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from source_audit import audit_snapshot  # noqa: E402
from source_model import build_discovery  # noqa: E402
from source_snapshot import SnapshotError, load_snapshot, write_json  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot)
        audit = audit_snapshot(snapshot, build_discovery(snapshot))
    except (SnapshotError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.out:
        write_json(args.out, audit)
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 2 if audit["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
