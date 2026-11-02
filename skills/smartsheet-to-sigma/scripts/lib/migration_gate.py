"""Source-bound review gate for generated Smartsheet plans."""
from __future__ import annotations

import json
from pathlib import Path

try:
    from .planner import build_plan, validate_plan
    from .source_audit import audit_snapshot
    from .source_model import build_discovery
    from .source_snapshot import load_snapshot
except ImportError:
    from planner import build_plan, validate_plan
    from source_audit import audit_snapshot
    from source_model import build_discovery
    from source_snapshot import load_snapshot


class MigrationGateError(RuntimeError):
    """A plan cannot proceed because identity or review evidence is invalid."""


def gate_plan(
    plan: dict,
    *,
    snapshot_path: str | None = None,
    dispositions_path: str | None = None,
    production: bool = False,
) -> dict:
    validate_plan(plan)
    if plan.get("auditStatus") == "BLOCKED":
        raise MigrationGateError("plan is blocked by its source audit")
    if production and not snapshot_path:
        raise MigrationGateError(
            "production completion requires --source-snapshot for identity revalidation"
        )
    if snapshot_path:
        snapshot = load_snapshot(snapshot_path)
        if snapshot["sourceSha256"] != plan["sourceSha256"]:
            raise MigrationGateError(
                "source snapshot hash does not match the plan sourceSha256"
            )
        if production:
            discovery = build_discovery(snapshot)
            regenerated = build_plan(
                snapshot,
                discovery,
                audit_snapshot(snapshot, discovery),
            )
            if _source_projection(plan) != _source_projection(regenerated):
                raise MigrationGateError(
                    "plan content does not match a fresh plan from --source-snapshot"
                )
    unresolved_decisions = [
        f"{asset.get('sourceId')}:{decision.get('sourceName')}"
        for asset in plan.get("assets") or []
        for decision in asset.get("mixedColumnPolicies") or []
        if decision.get("status") != "selected"
    ]
    if production and unresolved_decisions:
        raise MigrationGateError(
            "production completion requires a mixed-column policy for: "
            + ", ".join(unresolved_decisions)
        )
    pending = sorted(set(plan.get("pendingReview") or []))
    if not pending:
        return {"status": "NOT_REQUIRED", "pending": []}
    if not dispositions_path:
        if production:
            raise MigrationGateError(
                "production completion requires review dispositions for: "
                + ", ".join(pending)
            )
        return {"status": "DRAFT_PENDING", "pending": pending}
    record = _load_json(dispositions_path)
    if record.get("sourceSha256") != plan["sourceSha256"]:
        raise MigrationGateError(
            "review dispositions sourceSha256 does not match the plan"
        )
    if production and record.get("planSha256") != plan["planSha256"]:
        raise MigrationGateError(
            "review dispositions planSha256 does not match the final policy-bound plan"
        )
    indexed = {
        item.get("issueId"): item
        for item in record.get("issues") or []
        if isinstance(item, dict) and item.get("issueId")
    }
    unresolved = [
        code for code in pending if not _complete_disposition(indexed.get(code))
    ]
    if unresolved and production:
        raise MigrationGateError(
            "review dispositions are incomplete for: " + ", ".join(unresolved)
        )
    return {
        "status": "DISPOSITIONED" if not unresolved else "DRAFT_PENDING",
        "pending": unresolved,
    }


def _load_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationGateError(f"invalid review dispositions: {error}") from error
    if not isinstance(value, dict):
        raise MigrationGateError("review dispositions must be a JSON object")
    return value


def _complete_disposition(item) -> bool:
    evidence = item.get("evidence") if isinstance(item, dict) else None
    return (
        isinstance(item, dict)
        and isinstance(item.get("owner"), str)
        and bool(item["owner"].strip())
        and isinstance(item.get("disposition"), str)
        and bool(item["disposition"].strip())
        and item.get("verified") is True
        and isinstance(evidence, dict)
        and evidence.get("status") == "passed"
        and isinstance(evidence.get("evidenceId"), str)
        and bool(evidence["evidenceId"].strip())
        and isinstance(evidence.get("targetId"), str)
        and bool(evidence["targetId"].strip())
    )


def _source_projection(plan: dict) -> dict:
    """Remove allowed user decisions before source-derived plan comparison."""
    projected = json.loads(json.dumps(plan))
    projected.pop("planSha256", None)
    for asset in projected.get("assets") or []:
        for decision in asset.get("mixedColumnPolicies") or []:
            decision["status"] = "unresolved"
            decision["policy"] = None
            decision["finalFormula"] = None
        for column in asset.get("computedColumns") or []:
            for key in ("mixedPolicy", "overrideColumn", "finalFormula"):
                column.pop(key, None)
    return projected
