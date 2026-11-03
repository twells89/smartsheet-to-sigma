import copy
import json
import sys
from pathlib import Path
import tempfile
import unittest


LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(LIB))

from automation_translation import translate_sheet_workflows
from automation_workflows import (
    WorkflowCaptureError,
    merge_workflow_capture,
    validate_embedded_workflow_capture,
    validate_workflow_capture,
    workflow_capture_plan,
)
from migration_gate import MigrationGateError, gate_plan
from planner import apply_mixed_policy, build_plan
from sigma_builder import build_bundle
from source_audit import audit_snapshot
from source_model import build_discovery
from source_snapshot import finalize_snapshot, load_snapshot


def source_and_capture():
    source = load_snapshot(FIXTURES / "asset-snapshot.json")
    capture = json.loads(
        (FIXTURES / "automation-workflows.json").read_text(encoding="utf-8")
    )
    capture["baseSourceSha256"] = source["sourceSha256"]
    return source, capture


class AutomationWorkflowTests(unittest.TestCase):
    def test_capture_plan_requests_every_sheet_with_pagination(self):
        source, _ = source_and_capture()
        plan = workflow_capture_plan(source)
        self.assertEqual(plan["tool"], "get_workflows")
        self.assertEqual(plan["calls"][0]["arguments"]["sheet_id"], 1001)
        self.assertEqual(
            plan["calls"][0]["pagination"]["requestCursor"],
            "last_key",
        )

    def test_capture_is_source_bound_and_merges_into_identity(self):
        source, capture = source_and_capture()
        normalized = validate_workflow_capture(capture, source)
        self.assertTrue(normalized["complete"])
        merged, _ = merge_workflow_capture(source, capture)
        self.assertNotEqual(merged["sourceSha256"], source["sourceSha256"])
        raw = merged["assets"][0]["raw"]
        self.assertEqual(raw["_workflowCapture"]["status"], "complete")
        self.assertEqual(len(raw["_workflows"]), 4)

        stale = copy.deepcopy(capture)
        stale["baseSourceSha256"] = "0" * 64
        with self.assertRaises(WorkflowCaptureError):
            validate_workflow_capture(stale, source)

    def test_capture_rejects_unfinished_cursor_and_duplicate_ids(self):
        source, capture = source_and_capture()
        capture["sheets"][0]["pages"][0]["response_last_key"] = "next-page"
        with self.assertRaises(WorkflowCaptureError):
            validate_workflow_capture(capture, source)

        _, duplicate = source_and_capture()
        duplicate["sheets"][0]["workflows"][1]["id"] = 9001
        duplicate["sheets"][0]["pages"][0]["workflow_ids"][1] = "9001"
        with self.assertRaises(WorkflowCaptureError):
            validate_workflow_capture(duplicate, source)

        _, broken_chain = source_and_capture()
        page = broken_chain["sheets"][0]["pages"][0]
        page["response_last_key"] = None
        broken_chain["sheets"][0]["pages"].append(
            {
                "request_last_key": None,
                "response_last_key": None,
                "workflow_ids": [],
            }
        )
        with self.assertRaises(WorkflowCaptureError):
            validate_workflow_capture(broken_chain, source)

    def test_capture_requires_source_enabled_and_frequency(self):
        source, capture = source_and_capture()
        capture["sheets"][0]["workflows"][0].pop("enabled")
        with self.assertRaises(WorkflowCaptureError):
            validate_workflow_capture(capture, source)

        _, capture = source_and_capture()
        capture["sheets"][0]["workflows"][0].pop("frequency")
        with self.assertRaises(WorkflowCaptureError):
            validate_workflow_capture(capture, source)

    def test_embedded_capture_evidence_is_revalidated(self):
        source, capture = source_and_capture()
        merged, _ = merge_workflow_capture(source, capture)
        validate_embedded_workflow_capture(merged)
        tampered = copy.deepcopy(merged)
        tampered["assets"][0]["raw"]["_workflowCapture"]["pages"][0][
            "workflow_ids"
        ][0] = "different"
        tampered.pop("sourceSha256")
        tampered = finalize_snapshot(tampered)
        with self.assertRaises(WorkflowCaptureError):
            validate_embedded_workflow_capture(tampered)

    def test_translation_routes_supported_and_partial_workflows(self):
        _, capture = source_and_capture()
        translated = translate_sheet_workflows(
            "1001",
            capture["sheets"][0]["workflows"],
            capture={
                "status": "complete",
                "limitations": ["REMINDER_WORKFLOWS_NOT_RETURNED"],
            },
        )
        workflows = {
            workflow["sourceWorkflowId"]: workflow
            for workflow in translated["workflows"]
        }
        self.assertEqual(
            workflows["9001"]["trigger"]["route"],
            "schedule",
        )
        self.assertEqual(
            workflows["9002"]["trigger"]["route"],
            "external-event-bridge",
        )
        self.assertEqual(
            workflows["9002"]["actionTree"]["children"][0]["node"]["sigmaEffect"][
                "kind"
            ],
            "input-table-insert",
        )
        self.assertEqual(workflows["9003"]["coverage"], "SCAFFOLD_REQUIRED")
        self.assertEqual(workflows["9004"]["coverage"], "REVIEW_REQUIRED")
        self.assertIn(
            "Manually inventory reminder workflows in Smartsheet.",
            translated["userHandoffs"],
        )

    def test_else_node_is_a_branch_container(self):
        workflow = {
            "id": 1,
            "name": "Branch",
            "enabled": True,
            "frequency": "RIGHT_AWAY",
            "root_node": {
                "type": "ROOT_NODE",
                "trigger": {"type": "ROW_UPDATED"},
                "childNode": {
                    "type": "IF_ELSE",
                    "elseNode": {
                        "type": "ELSE_NODE",
                        "childNode": {"type": "EMAIL_RECIPIENT"},
                    },
                },
            },
        }
        translated = translate_sheet_workflows(
            "1001",
            [workflow],
            capture={"status": "complete", "limitations": []},
        )
        if_else = translated["workflows"][0]["actionTree"]
        else_node = if_else["children"][0]["node"]
        self.assertEqual(else_node["category"], "container")
        self.assertEqual(
            else_node["children"][0]["node"]["category"],
            "notification",
        )

    def test_audit_and_bundle_include_automation_intents(self):
        source, capture = source_and_capture()
        merged, _ = merge_workflow_capture(source, capture)
        discovery = build_discovery(merged)
        audit = audit_snapshot(merged, discovery)
        codes = {issue["code"] for issue in audit["issues"]}
        self.assertIn("AUTOMATION_EXTERNAL_TRIGGER_BRIDGE", codes)
        self.assertIn("AUTOMATION_SCAFFOLD_REQUIRED", codes)
        self.assertIn("AUTOMATION_EFFECT_REVIEW_REQUIRED", codes)
        self.assertIn("REMINDER_AUTOMATION_MANUAL_REVIEW", codes)
        self.assertNotIn("AUTOMATION_WORKFLOW_CAPTURE_REQUIRED", codes)

        plan = build_plan(merged, discovery, audit)
        self.assertEqual(
            len(plan["assets"][0]["automation"]["workflows"]),
            4,
        )
        with tempfile.TemporaryDirectory() as temp:
            manifest = build_bundle(plan, temp, gate=gate_plan(plan))
            intents = json.loads(
                (Path(temp) / "automation-intents.json").read_text()
            )
            workbooks = json.loads((Path(temp) / "workbooks.json").read_text())
            self.assertEqual(intents["summary"]["total"], 4)
            self.assertNotIn(
                "automatedActions",
                workbooks["workbooks"][0]["contents"],
            )
            self.assertEqual(
                len(workbooks["deferredAutomationIntents"]),
                4,
            )
            self.assertNotIn(
                "owner@example.com",
                json.dumps(workbooks),
            )
            self.assertNotIn(
                "owner@example.com",
                json.dumps(intents),
            )
            self.assertNotIn("do-not-copy", json.dumps(intents))
            self.assertEqual(manifest["targets"]["automationIntents"], 4)
            self.assertEqual(len(manifest["automationWorkflows"]), 4)
            self.assertTrue(
                all(
                    item["targetActionSequenceId"] is None
                    for item in manifest["automationWorkflows"]
                )
            )

    def test_incomplete_capture_blocks_migration(self):
        source, capture = source_and_capture()
        capture["sheets"][0]["complete"] = False
        capture["sheets"][0]["pages"][0]["response_last_key"] = "next-page"
        merged, _ = merge_workflow_capture(source, capture)
        discovery = build_discovery(merged)
        audit = audit_snapshot(merged, discovery)
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertIn(
            "AUTOMATION_CAPTURE_INCOMPLETE",
            {issue["code"] for issue in audit["issues"]},
        )

    def test_production_cannot_disposition_missing_capture(self):
        source, _ = source_and_capture()
        discovery = build_discovery(source)
        plan = build_plan(source, discovery, audit_snapshot(source, discovery))
        review = {
            "sourceSha256": source["sourceSha256"],
            "planSha256": plan["planSha256"],
            "issues": [
                {
                    "issueId": issue_id,
                    "owner": "Migration owner",
                    "disposition": "Accepted",
                    "verified": True,
                    "evidence": {
                        "status": "passed",
                        "evidenceId": issue_id,
                        "targetId": "target",
                    },
                }
                for issue_id in plan["pendingReview"]
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            snapshot_path = Path(temp) / "snapshot.json"
            review_path = Path(temp) / "review.json"
            snapshot_path.write_text(json.dumps(source))
            review_path.write_text(json.dumps(review))
            with self.assertRaises(MigrationGateError):
                gate_plan(
                    plan,
                    snapshot_path=str(snapshot_path),
                    dispositions_path=str(review_path),
                    production=True,
                )

    def test_production_manifest_carries_workflow_evidence(self):
        source, capture = source_and_capture()
        merged, _ = merge_workflow_capture(source, capture)
        discovery = build_discovery(merged)
        plan = build_plan(merged, discovery, audit_snapshot(merged, discovery))
        plan = apply_mixed_policy(plan, "override-wins")
        issues = []
        for issue in plan["reviewIssues"]:
            evidence_text = " ".join(str(item) for item in issue["evidence"])
            workflow_id = next(
                (
                    workflow_id
                    for workflow_id in ("9001", "9002", "9003", "9004")
                    if f":workflow:{workflow_id}:" in evidence_text
                ),
                None,
            )
            issues.append(
                {
                    "issueId": issue["issueId"],
                    "owner": "Migration owner",
                    "disposition": "Implemented and checked",
                    "verified": True,
                    "evidence": {
                        "status": "passed",
                        "evidenceId": f"check-{issue['issueId']}",
                        "targetId": (
                            f"sequence-{workflow_id}"
                            if workflow_id
                            else "migration-target"
                        ),
                    },
                }
            )
        review = {
            "sourceSha256": merged["sourceSha256"],
            "planSha256": plan["planSha256"],
            "issues": issues,
        }
        with tempfile.TemporaryDirectory() as temp:
            snapshot_path = Path(temp) / "snapshot.json"
            review_path = Path(temp) / "review.json"
            snapshot_path.write_text(json.dumps(merged))
            review_path.write_text(json.dumps(review))
            gate = gate_plan(
                plan,
                snapshot_path=str(snapshot_path),
                dispositions_path=str(review_path),
                production=True,
            )
            manifest = build_bundle(plan, temp, gate=gate)
            workflow = next(
                item
                for item in manifest["automationWorkflows"]
                if item["sourceWorkflowId"] == "9002"
            )
            self.assertEqual(workflow["deploymentState"], "VERIFIED")
            self.assertEqual(
                workflow["targetActionSequenceId"],
                "sequence-9002",
            )


if __name__ == "__main__":
    unittest.main()
