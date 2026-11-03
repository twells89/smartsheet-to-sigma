import json
import py_compile
import sys
from pathlib import Path
import tempfile
import unittest


LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(LIB))

from migration_gate import MigrationGateError, gate_plan
from automation_workflows import merge_workflow_capture
from planner import (
    _rewrite_formula_columns,
    apply_mixed_policy,
    build_plan,
    plan_sha256,
    validate_plan,
)
from sigma_builder import build_bundle
from source_audit import audit_snapshot
from source_model import build_discovery
from source_snapshot import load_snapshot
from source_snapshot import finalize_snapshot


def make_plan(fixture="asset-snapshot.json"):
    snapshot = load_snapshot(FIXTURES / fixture)
    discovery = build_discovery(snapshot)
    audit = audit_snapshot(snapshot, discovery)
    return snapshot, build_plan(snapshot, discovery, audit)


class PlannerBuilderTests(unittest.TestCase):
    def test_sheet_plan_preserves_rows_and_splits_mixed_override(self):
        _, plan = make_plan()
        validate_plan(plan)
        route = plan["assets"][0]
        editable = {column["name"] for column in route["editableColumns"]}
        computed = {column["name"] for column in route["computedColumns"]}
        self.assertIn("Rollup_OVERRIDE", editable)
        self.assertIn("Rollup", computed)
        self.assertEqual(route["naturalKeys"], ["ROW_ID"])
        self.assertEqual(route["rows"][1]["PARENT_ROW_ID"], "101")

    def test_workspace_plan_orders_sheet_report_dashboard(self):
        _, plan = make_plan("workspace-snapshot.json")
        self.assertEqual(
            [asset["sourceType"] for asset in plan["assets"]],
            ["sheet", "report", "dashboard"],
        )
        self.assertEqual(plan["assets"][1]["sourceSheetIds"], ["2001"])

    def test_source_column_cannot_overwrite_migration_row_id(self):
        raw = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        raw["assets"][0]["raw"]["columns"][0]["title"] = "ROW_ID"
        for row in raw["assets"][0]["raw"]["rows"]:
            cell = next(cell for cell in row["cells"] if cell["columnId"] == 14)
            cell["formula"] = "=[ROW_ID]@row"
            cell["value"] = next(
                item["value"] for item in row["cells"] if item["columnId"] == 11
            )
        snapshot = finalize_snapshot(raw)
        discovery = build_discovery(snapshot)
        plan = build_plan(snapshot, discovery, audit_snapshot(snapshot, discovery))
        route = plan["assets"][0]
        renamed = next(
            column for column in route["editableColumns"]
            if column["sourceColumnId"] == "11"
        )
        self.assertEqual(renamed["name"], "ROW_ID_SOURCE")
        calculated = next(
            column for column in route["computedColumns"]
            if column["sourceColumnId"] == "14"
        )
        self.assertEqual(calculated["sigmaFormula"], "[ROW_ID_SOURCE]")
        with tempfile.TemporaryDirectory() as temp:
            build_bundle(plan, temp, gate=gate_plan(plan))
            tables = json.loads((Path(temp) / "input-tables.json").read_text())
            self.assertEqual(tables["tables"][0]["seedRows"][0]["ROW_ID"], "101")

    def test_formula_reference_rewrite_skips_string_literals(self):
        self.assertEqual(
            _rewrite_formula_columns(
                'If([ROW_ID] = "[ROW_ID]", 1, 0)',
                {"row_id": "ROW_ID_SOURCE"},
            ),
            'If([ROW_ID_SOURCE] = "[ROW_ID]", 1, 0)',
        )

    def test_read_only_values_get_snapshot_join_route(self):
        raw = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        raw["assets"][0]["raw"]["columns"][1]["systemColumnType"] = "AUTO_NUMBER"
        snapshot = finalize_snapshot(raw)
        discovery = build_discovery(snapshot)
        plan = build_plan(snapshot, discovery, audit_snapshot(snapshot, discovery))
        with tempfile.TemporaryDirectory() as temp:
            build_bundle(plan, temp, gate=gate_plan(plan))
            models = json.loads((Path(temp) / "data-models.json").read_text())
            source = models["models"][0]["source"]
            self.assertEqual(source["kind"], "input-table-with-read-only-snapshot")
            self.assertEqual(source["join"]["editableKey"], "ROW_ID")
            data = json.loads((Path(temp) / "source-data.json").read_text())
            self.assertIn("immutable-snapshot-load", data["assets"][0]["loadRoute"])

    def test_bundle_emits_all_reviewable_artifacts(self):
        _, plan = make_plan()
        gate = gate_plan(plan)
        with tempfile.TemporaryDirectory() as temp:
            manifest = build_bundle(plan, temp, gate=gate)
            expected = {
                "input-tables.json",
                "data-models.json",
                "workbooks.json",
                "source-data.json",
                "dashboard-review.json",
                "automation-intents.json",
                "mcp-seed-program.py",
                "migration-manifest.json",
                "parity.json",
            }
            self.assertEqual({path.name for path in Path(temp).iterdir()}, expected)
            py_compile.compile(
                str(Path(temp) / "mcp-seed-program.py"),
                doraise=True,
            )
            tables = json.loads((Path(temp) / "input-tables.json").read_text())
            self.assertEqual(len(tables["tables"]), 1)
            rows = tables["tables"][0]["seedRows"]
            self.assertNotIn("Rollup_OVERRIDE", rows[0])
            self.assertEqual(rows[1]["Rollup_OVERRIDE"], 10)
            self.assertEqual(manifest["reviewStatus"], "DRAFT_PENDING")

    def test_production_requires_complete_source_bound_dispositions(self):
        snapshot, plan = make_plan()
        with self.assertRaises(MigrationGateError):
            gate_plan(plan, production=True)
        capture = json.loads(
            (FIXTURES / "automation-workflows.json").read_text()
        )
        capture["baseSourceSha256"] = snapshot["sourceSha256"]
        snapshot, _ = merge_workflow_capture(snapshot, capture)
        discovery = build_discovery(snapshot)
        plan = build_plan(
            snapshot,
            discovery,
            audit_snapshot(snapshot, discovery),
        )
        plan = apply_mixed_policy(plan, "override-wins")
        dispositions = {
            "sourceSha256": snapshot["sourceSha256"],
            "planSha256": plan["planSha256"],
            "issues": [
                {
                    "issueId": issue_id,
                    "code": issue_id.split(":", 1)[0],
                    "owner": "Migration owner",
                    "disposition": "Implemented and checked",
                    "verified": True,
                    "evidence": {
                        "status": "passed",
                        "evidenceId": f"check-{issue_id}",
                        "targetId": "target-element",
                    },
                }
                for issue_id in plan["pendingReview"]
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            path.write_text(json.dumps(dispositions))
            snapshot_path = Path(temp) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot))
            result = gate_plan(
                plan,
                snapshot_path=str(snapshot_path),
                dispositions_path=str(path),
                production=True,
            )
            self.assertEqual(result["status"], "DISPOSITIONED")

    def test_changed_snapshot_is_rejected(self):
        snapshot, plan = make_plan()
        changed = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        changed["assets"][0]["raw"]["rows"][0]["cells"][1]["value"] = 999
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "changed.json"
            path.write_text(json.dumps(changed))
            with self.assertRaises(MigrationGateError):
                gate_plan(plan, snapshot_path=str(path))

    def test_production_rejects_self_hashed_plan_tampering(self):
        _, plan = make_plan("workspace-snapshot.json")
        plan["assets"] = []
        plan["pendingReview"] = []
        plan["auditStatus"] = "AUTO"
        plan["planSha256"] = plan_sha256(plan)
        with self.assertRaises(MigrationGateError):
            gate_plan(
                plan,
                snapshot_path=str(FIXTURES / "workspace-snapshot.json"),
                production=True,
            )


if __name__ == "__main__":
    unittest.main()
