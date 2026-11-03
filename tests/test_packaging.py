"""Check plugin metadata and standalone skill completeness."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "smartsheet-to-sigma"


class PackagingTests(unittest.TestCase):
    def test_marketplace_points_to_plugin(self):
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(marketplace["name"], "smartsheet-to-sigma")
        self.assertEqual(len(marketplace["plugins"]), 1)
        self.assertEqual(marketplace["plugins"][0]["name"], manifest["name"])
        self.assertEqual(marketplace["plugins"][0]["source"], "./")
        self.assertNotIn("skills", manifest)

    def test_skill_is_complete_when_copied_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            standalone = Path(temp) / SKILL.name
            shutil.copytree(
                SKILL,
                standalone,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            content = (standalone / "SKILL.md").read_text()
            self.assertTrue(content.startswith("---\nname: smartsheet-to-sigma\n"))
            for path in (
                "requirements.txt",
                "QUICKSTART.md",
                "scripts/migrate.py",
                "scripts/audit-smartsheet.py",
                "scripts/build-migration.py",
                "scripts/automation-workflows.py",
            ):
                self.assertTrue((standalone / path).is_file(), path)
            for reference in re.findall(r"`(refs/[^` ]+\.md)`", content):
                self.assertTrue((standalone / reference).is_file(), reference)
            self.assertNotIn("../excel-to-sigma", content)
            self.assertNotIn("~/sigma-skills/", content)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-q",
                ],
                cwd=standalone,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_root_requirements_uses_skill_requirements(self):
        self.assertEqual(
            (ROOT / "requirements.txt").read_text().strip(),
            "-r skills/smartsheet-to-sigma/requirements.txt",
        )


if __name__ == "__main__":
    unittest.main()
