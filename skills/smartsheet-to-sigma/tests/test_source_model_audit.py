import json
import sys
from pathlib import Path
import unittest


LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(LIB))

from source_audit import audit_snapshot, review_template
from planner import build_plan
from source_model import build_discovery
from source_snapshot import finalize_snapshot, load_snapshot


class SourceModelAuditTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = load_snapshot(FIXTURES / "asset-snapshot.json")
        self.discovery = build_discovery(self.snapshot)

    def test_discovers_types_formulas_and_hierarchy(self):
        asset = self.discovery["assets"][0]
        columns = {column["title"]: column for column in asset["columns"]}
        self.assertEqual(asset["rowCount"], 3)
        self.assertEqual(columns["Amount"]["sigmaType"], "number")
        self.assertEqual(columns["Double Amount"]["behavior"], "computed")
        self.assertTrue(columns["Double Amount"]["supportedFormula"])
        self.assertEqual(columns["Rollup"]["behavior"], "mixed")
        self.assertIn("HIERARCHY_FUNCTION", columns["Rollup"]["unsupportedFormulaReasons"])
        self.assertTrue(asset["hierarchy"]["hasHierarchy"])
        self.assertEqual(asset["hierarchy"]["maxDepth"], 1)
        self.assertEqual(
            [row["siblingOrder"] for row in asset["hierarchy"]["rows"][1:]],
            [0, 1],
        )
        self.assertEqual(columns["Double Amount"]["sigmaType"], "number")

    def test_audit_is_fail_loud(self):
        audit = audit_snapshot(self.snapshot, self.discovery)
        codes = {issue["code"] for issue in audit["issues"]}
        self.assertEqual(audit["status"], "REVIEW_REQUIRED")
        self.assertIn("UNSUPPORTED_FORMULA", codes)
        self.assertIn("MIXED_INPUT_FORMULA_COLUMN", codes)
        self.assertIn("AUTOMATION_REVIEW_REQUIRED", codes)
        self.assertIn("SECURITY_MAPPING_REQUIRED", codes)
        self.assertIn("COLLABORATION_CONTENT_REVIEW_REQUIRED", codes)

    def test_review_template_is_bound_to_source(self):
        audit = audit_snapshot(self.snapshot, self.discovery)
        template = review_template(audit)
        self.assertEqual(template["sourceSha256"], self.snapshot["sourceSha256"])
        self.assertTrue(template["issues"])
        self.assertTrue(all(item["verified"] is False for item in template["issues"]))

    def test_workspace_orders_inventory_and_dashboard_widget_support(self):
        snapshot = load_snapshot(FIXTURES / "workspace-snapshot.json")
        discovery = build_discovery(snapshot)
        self.assertEqual([asset["type"] for asset in discovery["assets"]], [
            "sheet",
            "report",
            "dashboard",
        ])
        dashboard = discovery["assets"][2]
        report = discovery["assets"][1]
        self.assertEqual(
            [column["id"] for column in report["columns"]],
            ["2101", "2201"],
        )
        self.assertEqual(dashboard["widgetCount"], 2)
        self.assertEqual(dashboard["unsupportedWidgetCount"], 2)
        audit = audit_snapshot(snapshot, discovery)
        self.assertIn(
            "UNSUPPORTED_DASHBOARD_WIDGET",
            {issue["code"] for issue in audit["issues"]},
        )

    def test_partial_formula_coverage_requires_review(self):
        raw = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        target = next(
            cell
            for cell in raw["assets"][0]["raw"]["rows"][2]["cells"]
            if cell["columnId"] == 14
        )
        target.pop("formula")
        target.pop("value")
        snapshot = finalize_snapshot(raw)
        discovery = build_discovery(snapshot)
        column = next(
            column
            for column in discovery["assets"][0]["columns"]
            if column["title"] == "Double Amount"
        )
        self.assertFalse(column["supportedFormula"])
        audit = audit_snapshot(snapshot, discovery)
        self.assertIn(
            "PARTIAL_FORMULA_COVERAGE",
            {issue["code"] for issue in audit["issues"]},
        )

    def test_system_managed_column_is_not_editable(self):
        raw = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        raw["assets"][0]["raw"]["columns"][1]["systemColumnType"] = "AUTO_NUMBER"
        snapshot = finalize_snapshot(raw)
        discovery = build_discovery(snapshot)
        column = next(
            column
            for column in discovery["assets"][0]["columns"]
            if column["title"] == "Amount"
        )
        self.assertEqual(column["behavior"], "read-only")
        audit = audit_snapshot(snapshot, discovery)
        self.assertIn(
            "SOURCE_LOCKS_REVIEW_REQUIRED",
            {issue["code"] for issue in audit["issues"]},
        )
        plan = build_plan(snapshot, discovery, audit)
        route = plan["assets"][0]
        self.assertNotIn(
            "Amount",
            {item["sourceName"] for item in route["editableColumns"]},
        )
        self.assertIn(
            "Amount",
            {item["sourceName"] for item in route["readOnlyColumns"]},
        )

    def test_locked_mixed_column_is_not_editable(self):
        raw = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        rollup = next(
            column
            for column in raw["assets"][0]["raw"]["columns"]
            if column["title"] == "Rollup"
        )
        rollup["locked"] = True
        snapshot = finalize_snapshot(raw)
        discovery = build_discovery(snapshot)
        column = next(
            column
            for column in discovery["assets"][0]["columns"]
            if column["title"] == "Rollup"
        )
        self.assertEqual(column["behavior"], "read-only")
        plan = build_plan(snapshot, discovery, audit_snapshot(snapshot, discovery))
        route = plan["assets"][0]
        self.assertNotIn(
            "Rollup",
            {item["sourceName"] for item in route["editableColumns"]},
        )
        self.assertEqual(route["mixedColumnPolicies"], [])


if __name__ == "__main__":
    unittest.main()
