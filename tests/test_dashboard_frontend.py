"""Guards for the dashboard's frontend: the committed production build is
present and wired to the server, and the pure frontend modules pass their
node unit tests (skipped when node is not installed)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_STATIC = _REPO_ROOT / "src" / "kusudaemon" / "dashboard" / "static"
_FRONTEND = _REPO_ROOT / "frontend"


class FrontendBuildTest(unittest.TestCase):
    def test_committed_build_is_complete(self) -> None:
        for name in ("index.html", "app.js", "app.css"):
            self.assertTrue((_STATIC / name).is_file(), f"missing build output: {name}")
        index = (_STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Kusudaemon</title>", index)
        self.assertIn('src="/static/app.js"', index)
        self.assertIn('href="/static/app.css"', index)

    def test_every_asset_the_css_references_exists(self) -> None:
        css = (_STATIC / "app.css").read_text(encoding="utf-8")
        refs = re.findall(r"url\(([^)]+)\)", css)
        fonts = [r.strip("'\"") for r in refs if "data:" not in r]
        self.assertTrue(fonts, "expected bundled fonts")
        for ref in fonts:
            self.assertTrue(ref.startswith("/static/"), ref)
            self.assertTrue((_STATIC / ref[len("/static/") :]).is_file(), f"missing asset {ref}")

    def test_bundle_has_no_external_hosts(self) -> None:
        js = (_STATIC / "app.js").read_text(encoding="utf-8")
        css = (_STATIC / "app.css").read_text(encoding="utf-8")
        self.assertNotIn("fonts.googleapis.com", js + css)
        self.assertNotIn("cdn.", css)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FrontendUnitTest(unittest.TestCase):
    def test_pure_modules(self) -> None:
        proc = subprocess.run(
            ["node", str(_FRONTEND / "scripts" / "unit.mjs")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(_FRONTEND),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"\d+ passed")


if __name__ == "__main__":
    unittest.main()
