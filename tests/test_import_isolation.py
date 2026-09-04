from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


class OptionalDepIsolationTest(unittest.TestCase):
    def test_driver_imports_without_optional_deps(self) -> None:
        optional_deps = ["tomli", "tomllib", "gptme"]
        for dep in optional_deps:
            with self.subTest(dep=dep):
                with mock.patch.dict(sys.modules, {dep: None}):
                    for mod in [m for m in list(sys.modules) if m.startswith("kusudaemon")]:
                        del sys.modules[mod]
                    from kusudaemon.pipeline.driver import RecursiveDriver  # noqa: F401
                    self.assertIsNotNone(RecursiveDriver)


if __name__ == "__main__":
    unittest.main()
