import copy
import json
import sys
from pathlib import Path
import tempfile
import unittest


LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(LIB))

from source_snapshot import (
    SnapshotError,
    _sanitize_source_data,
    capture_snapshot,
    finalize_snapshot,
    load_snapshot,
    parse_source,
    snapshot_sha256,
    validate_snapshot,
    write_json,
)


class FakeClient:
    def get_workspace(self, workspace_id):
        return {
            "id": int(workspace_id),
            "name": "Workspace",
        }

    def list_workspace_tree(self, workspace_id, workspace_name):
        return [
            {"id": 10, "name": "Sheet", "assetType": "sheet", "path": "Workspace/Inputs"},
            {"id": 20, "name": "Report", "assetType": "report", "path": "Workspace/Inputs"},
            {"id": 30, "name": "Dashboard", "assetType": "dashboard", "path": "Workspace"},
        ]

    def get_sheet(self, sheet_id):
        return {
            "id": int(sheet_id),
            "name": "Sheet",
            "columns": [],
            "rows": [],
            "attachments": [
                {
                    "id": 99,
                    "mimeType": "text/plain",
                    "url": "https://temporary.example/download",
                    "urlExpiresInMillis": 1000,
                }
            ],
        }

    def get_report(self, report_id):
        return {
            "id": int(report_id),
            "name": "Report",
            "columns": [],
            "rows": [],
            "totalRowCount": 0,
            "_captureComplete": True,
            "_scope": [{"assetType": "sheet", "assetId": 10}],
        }

    def get_dashboard(self, dashboard_id):
        return {
            "id": int(dashboard_id),
            "name": "Dashboard",
            "widgets": [
                {
                    "id": 1,
                    "type": "METRIC",
                    "contents": {"type": "METRIC", "reportId": 20},
                }
            ],
        }

    def optional_list(self, path, **kwargs):
        return [], None

    def optional_token_list(self, path, **kwargs):
        return [], None


class SourceSnapshotTests(unittest.TestCase):
    def test_fixture_gets_stable_canonical_identity(self):
        first = load_snapshot(FIXTURES / "asset-snapshot.json")
        second = load_snapshot(FIXTURES / "asset-snapshot.json")
        self.assertEqual(first["sourceSha256"], second["sourceSha256"])
        self.assertEqual(first["sourceSha256"], snapshot_sha256(first))

    def test_capture_time_does_not_change_identity(self):
        snapshot = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        first = finalize_snapshot(snapshot)
        snapshot["capturedAt"] = "2030-01-01T00:00:00+00:00"
        second = finalize_snapshot(snapshot)
        self.assertEqual(first["sourceSha256"], second["sourceSha256"])

    def test_source_locator_form_does_not_change_identity(self):
        snapshot = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        first = finalize_snapshot(snapshot)
        snapshot["source"]["url"] = None
        second = finalize_snapshot(snapshot)
        self.assertEqual(first["sourceSha256"], second["sourceSha256"])

    def test_changed_source_invalidates_hash(self):
        snapshot = load_snapshot(FIXTURES / "asset-snapshot.json")
        changed = copy.deepcopy(snapshot)
        changed["assets"][0]["raw"]["rows"][0]["cells"][1]["value"] = 31
        with self.assertRaises(SnapshotError):
            validate_snapshot(changed)
        changed.pop("sourceSha256")
        refreshed = finalize_snapshot(changed)
        self.assertNotEqual(snapshot["sourceSha256"], refreshed["sourceSha256"])

    def test_round_trip_validates_recorded_hash(self):
        snapshot = load_snapshot(FIXTURES / "workspace-snapshot.json")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            path.write_text(json.dumps(snapshot))
            self.assertEqual(load_snapshot(path)["sourceSha256"], snapshot["sourceSha256"])

    def test_generated_json_is_owner_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "artifact.json"
            write_json(path, {"value": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_durable_link_attachment_url_is_preserved(self):
        value = {
            "attachments": [
                {
                    "id": 1,
                    "attachmentType": "LINK",
                    "url": "https://example.com/policy",
                }
            ]
        }
        cleaned = _sanitize_source_data(value)
        self.assertEqual(
            cleaned["attachments"][0]["url"],
            "https://example.com/policy",
        )

    def test_parses_asset_and_workspace_urls(self):
        self.assertEqual(
            parse_source(
                "https://app.smartsheet.com/sheets/12345",
                scope="asset",
            )["type"],
            "sheet",
        )
        self.assertEqual(
            parse_source(
                "https://app.smartsheet.com/workspaces/67890",
                scope="workspace",
            )["id"],
            "67890",
        )

    def test_bare_asset_id_requires_type(self):
        with self.assertRaises(SnapshotError):
            parse_source("12345", scope="asset")

    def test_asset_source_must_match_captured_root(self):
        snapshot = json.loads((FIXTURES / "asset-snapshot.json").read_text())
        snapshot["source"]["id"] = "different"
        with self.assertRaises(SnapshotError):
            finalize_snapshot(snapshot)

    def test_workspace_capture_deduplicates_and_builds_dependencies(self):
        snapshot = capture_snapshot(
            FakeClient(),
            {"id": "5000", "type": "workspace", "url": None},
            scope="workspace",
        )
        self.assertEqual(
            [asset["type"] for asset in snapshot["assets"]],
            ["sheet", "report", "dashboard"],
        )
        self.assertEqual(
            {(item["from"], item["to"]) for item in snapshot["dependencies"]},
            {("20", "10"), ("30", "20")},
        )
        attachment = snapshot["assets"][0]["raw"]["attachments"][0]
        self.assertNotIn("url", attachment)
        self.assertNotIn("urlExpiresInMillis", attachment)

    def test_dashboard_asset_capture_recurses_to_report_and_sheet(self):
        snapshot = capture_snapshot(
            FakeClient(),
            {"id": "30", "type": "dashboard", "url": None},
            scope="asset",
        )
        self.assertEqual(
            {(asset["type"], asset["id"]) for asset in snapshot["assets"]},
            {("dashboard", "30"), ("report", "20"), ("sheet", "10")},
        )


if __name__ == "__main__":
    unittest.main()
