"""Translate captured Smartsheet workflow trees into Sigma automation intents.

The output is intentionally an intent layer: current Sigma workbook code
representation supports action effects, but tenant assets, trigger credentials,
and some automated-action settings still need live OpenAPI validation or UI
binding.
"""
from __future__ import annotations

import hashlib
import json
import re


COVERAGE_RANK = {
    "EFFECT_INTENT_READY": 0,
    "EFFECT_INTENT_WITH_HANDOFF": 1,
    "SCAFFOLD_REQUIRED": 2,
    "REVIEW_REQUIRED": 3,
}

CONDITION_TYPES = {
    "CONDITION",
    "CONDITION_NODE",
    "IF",
    "IF_ELSE",
    "IF_ELSE_NODE",
}


def translate_sheet_workflows(
    sheet_id: str,
    workflows: list[dict],
    *,
    capture: dict | None = None,
) -> dict:
    """Translate every captured workflow on one sheet."""
    translated = [
        translate_workflow(sheet_id, workflow, index=index)
        for index, workflow in enumerate(workflows)
    ]
    capture = capture or {}
    handoffs = sorted(
        {
            handoff
            for workflow in translated
            for handoff in workflow["userHandoffs"]
        }
        | set(
            ["Manually inventory reminder workflows in Smartsheet."]
            if "REMINDER_WORKFLOWS_NOT_RETURNED"
            in (capture.get("limitations") or [])
            else []
        )
    )
    return {
        "sheetId": str(sheet_id),
        "captureStatus": capture.get("status") or "missing",
        "captureLimitations": capture.get("limitations") or [],
        "workflowCount": len(translated),
        "workflows": translated,
        "summary": {
            "effectIntentReady": sum(
                workflow["coverage"] == "EFFECT_INTENT_READY"
                for workflow in translated
            ),
            "effectIntentWithHandoff": sum(
                workflow["coverage"] == "EFFECT_INTENT_WITH_HANDOFF"
                for workflow in translated
            ),
            "scaffoldRequired": sum(
                workflow["coverage"] == "SCAFFOLD_REQUIRED"
                for workflow in translated
            ),
            "reviewRequired": sum(
                workflow["coverage"] == "REVIEW_REQUIRED"
                for workflow in translated
            ),
        },
        "userHandoffs": handoffs,
    }


def translate_workflow(
    sheet_id: str,
    workflow: dict,
    *,
    index: int = 0,
) -> dict:
    root = workflow.get("root_node") or workflow.get("rootNode") or {}
    frequency = workflow.get("frequency")
    enabled = workflow.get("enabled")
    trigger = _translate_trigger(
        root.get("trigger") or {},
        frequency=str(frequency or "UNKNOWN"),
    )
    action_tree = _translate_node(
        root.get("childNode") or {},
        path="root.childNode",
    )
    nodes = list(_flatten_translated_nodes(action_tree))
    coverage = _worst_coverage(
        [trigger["coverage"], *(node["coverage"] for node in nodes)]
    )
    workflow_id = workflow.get("id")
    if workflow_id is None:
        workflow_id = _stable_id(sheet_id, workflow, index)
    handoffs = sorted(
        set(trigger["userHandoffs"])
        | {
            handoff
            for node in nodes
            for handoff in node["userHandoffs"]
        }
        | {"Publish the Sigma workbook before enabling automated actions."}
    )
    return {
        "sourceWorkflowId": str(workflow_id),
        "sheetId": str(sheet_id),
        "name": workflow.get("name") or f"Workflow {index + 1}",
        "enabled": enabled,
        "frequency": frequency,
        "coverage": coverage,
        "trigger": trigger,
        "actionTree": action_tree,
        "nodeCounts": _node_counts(nodes),
        "userHandoffs": handoffs,
        "sourceTreeSha256": hashlib.sha256(
            json.dumps(
                root,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "codeRepresentationIntent": {
            "logicalId": f"automation_{_identifier(sheet_id)}_{_identifier(workflow_id)}",
            "name": workflow.get("name") or f"Workflow {index + 1}",
            "enabled": enabled,
            "trigger": trigger["sigmaIntent"],
            "effects": action_tree,
            "representationStatus": coverage,
            "requiresCurrentOpenApiValidation": True,
        },
        "parityTarget": {
            "enabled": enabled,
            "sourceTriggerType": trigger["sourceType"],
            "sourceNodeTypes": [node["sourceType"] for node in nodes],
        },
    }


def _translate_trigger(trigger: dict, *, frequency: str) -> dict:
    source_type = _node_type(trigger) or "UNKNOWN_TRIGGER"
    upper_frequency = frequency.upper()
    type_tokens = set(filter(None, re.split(r"[^A-Z0-9]+", source_type)))
    sanitized_trigger = _destination_config(trigger)
    user_handoffs: list[str] = []
    if "WEBHOOK" in source_type:
        route = "webhook"
        coverage = "EFFECT_INTENT_WITH_HANDOFF"
        sigma = {
            "kind": "webhook",
            "sourceConfig": sanitized_trigger,
            "authentication": "${SIGMA_WEBHOOK_AUTH_UI}",
        }
        user_handoffs.extend(
            [
                "Configure webhook authentication/HMAC in Sigma.",
                "Publish and copy the live Sigma webhook URL.",
            ]
        )
    elif any(
        token in type_tokens
        for token in ("ROW", "CELL", "COLUMN", "CREATED", "CHANGED", "UPDATED")
    ):
        route = "external-event-bridge"
        coverage = "EFFECT_INTENT_WITH_HANDOFF"
        sigma = {
            "kind": "webhook",
            "sourceEvent": source_type,
            "sourceConfig": sanitized_trigger,
            "batchingFrequency": frequency,
            "bridge": "${SMARTSHEET_EVENT_TO_SIGMA_WEBHOOK}",
        }
        user_handoffs.extend(
            [
                "Create an event bridge from the Smartsheet trigger to a Sigma webhook.",
                "Configure webhook authentication/HMAC in Sigma.",
            ]
        )
    elif (
        upper_frequency not in {"", "RIGHT_AWAY", "IMMEDIATE"}
        or bool(
            type_tokens
            & {"SCHEDULE", "DATE", "TIME", "DAILY", "WEEKLY", "MONTHLY"}
        )
    ):
        route = "schedule"
        coverage = "EFFECT_INTENT_WITH_HANDOFF"
        sigma = {
            "kind": "schedule",
            "frequency": frequency,
            "sourceConfig": sanitized_trigger,
        }
        user_handoffs.append(
            "Confirm the Sigma schedule/cron and source timezone."
        )
    else:
        route = "review"
        coverage = "REVIEW_REQUIRED"
        sigma = {
            "kind": "unmapped-trigger",
            "sourceType": source_type,
            "sourceConfig": sanitized_trigger,
        }
        user_handoffs.append(
            f"Choose a Sigma trigger for unsupported Smartsheet trigger {source_type}."
        )
    return {
        "sourceType": source_type,
        "sourceConfig": sanitized_trigger,
        "route": route,
        "coverage": coverage,
        "sigmaIntent": sigma,
        "userHandoffs": user_handoffs,
    }


def _translate_node(node: dict, *, path: str) -> dict:
    source_type = _node_type(node) or "UNKNOWN_NODE"
    category, sigma_effect, coverage, handoffs = _effect_route(source_type, node)
    children = []
    source_config = {}
    for key, value in node.items():
        if key in {"id", "type"}:
            continue
        if _looks_like_node(value):
            children.append(
                {
                    "edge": key,
                    "node": _translate_node(value, path=f"{path}.{key}"),
                }
            )
        elif isinstance(value, list) and any(_looks_like_node(item) for item in value):
            for index, item in enumerate(value):
                if _looks_like_node(item):
                    children.append(
                        {
                            "edge": f"{key}[{index}]",
                            "node": _translate_node(
                                item,
                                path=f"{path}.{key}[{index}]",
                            ),
                        }
                    )
                else:
                    source_config.setdefault(key, []).append(item)
        else:
            source_config[key] = value
    return {
        "path": path,
        "sourceType": source_type,
        "category": category,
        "coverage": coverage,
        "sigmaEffect": {
            **sigma_effect,
            "sourceConfig": _destination_config(source_config),
        },
        "userHandoffs": handoffs,
        "children": children,
    }


def _effect_route(
    source_type: str,
    node: dict,
) -> tuple[str, dict, str, list[str]]:
    if source_type in CONDITION_TYPES or any(
        token in source_type for token in ("CONDITION", "IF_ELSE")
    ):
        return (
            "condition",
            {"kind": "conditional", "operator": source_type},
            "EFFECT_INTENT_READY",
            [],
        )
    if source_type in {"EMAIL_RECIPIENT", "NOTIFICATION", "ALERT"} or any(
        token in source_type for token in ("EMAIL", "NOTIFY", "NOTIFICATION", "ALERT")
    ):
        return (
            "notification",
            {"kind": "send-notification"},
            "EFFECT_INTENT_READY",
            [],
        )
    if "EXPORT" in source_type:
        return (
            "export",
            {"kind": "export"},
            "EFFECT_INTENT_READY",
            [],
        )
    if "API" in source_type or "HTTP" in source_type:
        return (
            "api-call",
            {"kind": "call-api", "connectorId": "${SIGMA_API_CONNECTOR_ID}"},
            "EFFECT_INTENT_WITH_HANDOFF",
            ["Create and authorize the referenced Sigma API connector."],
        )
    if source_type == "COPY_ROW" or (
        "COPY" in source_type and "ROW" in source_type
    ):
        return (
            "row-copy",
            {"kind": "input-table-insert"},
            "EFFECT_INTENT_WITH_HANDOFF",
            ["Bind the copy destination to a Sigma input table."],
        )
    if source_type == "MOVE_ROW" or (
        "MOVE" in source_type and "ROW" in source_type
    ):
        return (
            "row-move",
            {
                "kind": "input-table-insert-delete",
                "transactionalParityRequired": True,
            },
            "SCAFFOLD_REQUIRED",
            [
                "Bind source/destination input tables and test insert-before-delete behavior."
            ],
        )
    if source_type in {"UPDATE_REQUEST", "APPROVAL"}:
        return (
            "human-workflow",
            {
                "kind": (
                    "approval-state-machine"
                    if source_type == "APPROVAL"
                    else "request-update"
                )
            },
            "SCAFFOLD_REQUIRED",
            [
                f"Rebuild {source_type} with Sigma input state, notifications, and user actions."
            ],
        )
    if any(
        token in source_type
        for token in (
            "CHANGE_CELL",
            "SET_CELL",
            "CLEAR_CELL",
            "RECORD_DATE",
            "ASSIGN",
        )
    ):
        return (
            "row-update",
            {"kind": "input-table-update"},
            "EFFECT_INTENT_WITH_HANDOFF",
            ["Bind referenced columns to editable Sigma input-table columns."],
        )
    if "LOCK" in source_type:
        return (
            "protection",
            {"kind": "input-protection-policy"},
            "REVIEW_REQUIRED",
            ["Recreate row/column protection with Sigma permissions or UI policy."],
        )
    if source_type in {"ELSE", "ELSE_NODE", "SEQUENCE", "ACTION_SEQUENCE"}:
        return (
            "container",
            {"kind": "sequence"},
            "EFFECT_INTENT_READY",
            [],
        )
    return (
        "unknown",
        {"kind": "unmapped-effect", "sourceType": source_type},
        "REVIEW_REQUIRED",
        [f"Choose a Sigma implementation for unsupported action {source_type}."],
    )


def _flatten_translated_nodes(node: dict):
    if node:
        yield node
    for child in node.get("children") or []:
        yield from _flatten_translated_nodes(child["node"])


def _node_counts(nodes: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node["sourceType"]] = counts.get(node["sourceType"], 0) + 1
    return dict(sorted(counts.items()))


def _worst_coverage(values) -> str:
    values = list(values)
    if not values:
        return "REVIEW_REQUIRED"
    return max(values, key=lambda value: COVERAGE_RANK.get(value, 99))


def _node_type(node: dict) -> str:
    return str(node.get("type") or "").strip().upper()


def _looks_like_node(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("type"), str)


def _destination_config(value, *, key: str = ""):
    """Replace recipient/credential values with destination bindings."""
    lowered = re.sub(r"[^a-z0-9]+", "", key.lower())
    sensitive_tokens = (
        "recipient",
        "email",
        "token",
        "secret",
        "password",
        "authorization",
        "credential",
        "hmac",
        "apikey",
    )
    if any(token in lowered for token in sensitive_tokens):
        count = len(value) if isinstance(value, list) else (0 if value is None else 1)
        name = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_") or "VALUE"
        return {
            "binding": f"${{SIGMA_{name}}}",
            "sourceCount": count,
        }
    if isinstance(value, dict):
        return {
            child_key: _destination_config(child_value, key=child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_destination_config(item, key=key) for item in value]
    return value


def _stable_id(sheet_id: str, workflow: dict, index: int) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "sheetId": str(sheet_id),
                "index": index,
                "workflow": workflow,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"captured-{digest}"


def _identifier(value) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value))
