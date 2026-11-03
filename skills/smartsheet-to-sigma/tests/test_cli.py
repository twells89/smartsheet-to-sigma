import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CliTests(unittest.TestCase):
    def test_offline_plan_and_build(self):
        with tempfile.TemporaryDirectory() as temp:
            plan_dir = Path(temp) / "plan"
            build_dir = Path(temp) / "build"
            planned = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate.py",
                    "plan",
                    "fixture",
                    "--scope",
                    "asset",
                    "--asset-type",
                    "sheet",
                    "--snapshot",
                    str(FIXTURES / "asset-snapshot.json"),
                    "--out-dir",
                    str(plan_dir),
                ],
                cwd=SKILL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            summary = json.loads(planned.stdout)
            self.assertEqual(summary["auditStatus"], "REVIEW_REQUIRED")
            self.assertTrue((plan_dir / "source-snapshot.json").is_file())
            self.assertTrue((plan_dir / "workflow-capture-plan.json").is_file())
            capture = json.loads(
                (FIXTURES / "automation-workflows.json").read_text()
            )
            capture["baseSourceSha256"] = summary["sourceSha256"]
            capture_path = Path(temp) / "automation-workflows.json"
            capture_path.write_text(json.dumps(capture))
            enriched_dir = plan_dir
            enriched = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate.py",
                    "plan",
                    "fixture",
                    "--scope",
                    "asset",
                    "--asset-type",
                    "sheet",
                    "--snapshot",
                    str(plan_dir / "source-snapshot.json"),
                    "--automation-workflows",
                    str(capture_path),
                    "--out-dir",
                    str(enriched_dir),
                ],
                cwd=SKILL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(enriched.returncode, 0, enriched.stderr)
            enriched_plan = json.loads(
                (enriched_dir / "plan.json").read_text()
            )
            self.assertEqual(
                len(enriched_plan["assets"][0]["automation"]["workflows"]),
                4,
            )
            self.assertFalse(
                (enriched_dir / "workflow-capture-plan.json").exists()
            )
            self.assertTrue(
                (enriched_dir / "automation-workflows.normalized.json").exists()
            )
            base_snapshot = json.loads(
                (enriched_dir / "base-source-snapshot.json").read_text()
            )
            normalized_capture = json.loads(
                (
                    enriched_dir / "automation-workflows.normalized.json"
                ).read_text()
            )
            self.assertEqual(
                normalized_capture["baseSourceSha256"],
                base_snapshot["sourceSha256"],
            )
            self.assertNotEqual(
                enriched_plan["sourceSha256"],
                base_snapshot["sourceSha256"],
            )
            built = subprocess.run(
                [
                    sys.executable,
                    "scripts/build-migration.py",
                    str(enriched_dir / "plan.json"),
                    "--out-dir",
                    str(build_dir),
                ],
                cwd=SKILL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(json.loads(built.stdout)["status"], "PLANNED")
            reset = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate.py",
                    "plan",
                    "fixture",
                    "--scope",
                    "asset",
                    "--asset-type",
                    "sheet",
                    "--snapshot",
                    str(enriched_dir / "base-source-snapshot.json"),
                    "--out-dir",
                    str(enriched_dir),
                ],
                cwd=SKILL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertTrue(
                (enriched_dir / "workflow-capture-plan.json").exists()
            )
            self.assertFalse(
                (enriched_dir / "automation-workflows.normalized.json").exists()
            )
            self.assertFalse(
                (enriched_dir / "base-source-snapshot.json").exists()
            )

    def test_blocked_snapshot_returns_two_and_still_writes_audit(self):
        blocked = {
            "schemaVersion": 1,
            "captureKind": "smartsheet",
            "scope": "asset",
            "source": {"id": "1", "type": "sheet"},
            "assets": [
                {
                    "id": "1",
                    "type": "sheet",
                    "name": "Incomplete",
                    "path": "",
                    "featureErrors": [],
                    "raw": {"id": 1, "name": "Incomplete", "columns": [], "rows": []},
                }
            ],
            "dependencies": [],
            "errors": [
                {
                    "code": "ASSET_CAPTURE_FAILED",
                    "assetType": "sheet",
                    "assetId": "2",
                    "status": 403,
                    "message": "dependency inaccessible",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "blocked.json"
            out = Path(temp) / "out"
            source.write_text(json.dumps(blocked))
            done = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate.py",
                    "plan",
                    "fixture",
                    "--scope",
                    "asset",
                    "--asset-type",
                    "sheet",
                    "--snapshot",
                    str(source),
                    "--out-dir",
                    str(out),
                ],
                cwd=SKILL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 2)
            audit = json.loads((out / "migration-audit.json").read_text())
            self.assertEqual(audit["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
