"""Meta-test ensuring all test files in tests/ are discoverable, importable,
contain at least one TestCase subclass, and that the total suite meets a checked-in floor.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_TESTS_DIR))

# Floor for total tests: 1064 (original runnable) + 22 (ported pytest files) + 24 (phase 1 benchmarks) >= 1100
SUITE_TEST_COUNT_FLOOR = 1100


class TestSuiteReachabilityTest(unittest.TestCase):
    def test_all_test_files_importable_and_have_test_cases(self) -> None:
        test_files = sorted(
            p for p in _TESTS_DIR.glob("test_*.py") if p.is_file()
        )
        self.assertGreaterEqual(len(test_files), 50, "Expected at least 50 test files in tests/")

        total_tests = 0
        for test_file in test_files:
            module_name = test_file.stem
            with self.subTest(module=module_name):
                try:
                    mod = importlib.import_module(module_name)
                except Exception as exc:
                    self.fail(f"Failed to import {test_file.name}: {exc}")

                test_case_classes = [
                    cls
                    for name, cls in inspect.getmembers(mod, inspect.isclass)
                    if issubclass(cls, unittest.TestCase)
                    and cls is not unittest.TestCase
                    and cls.__module__ == mod.__name__
                ]

                self.assertGreater(
                    len(test_case_classes),
                    0,
                    f"{test_file.name} defines no unittest.TestCase subclasses",
                )

                file_tests = sum(
                    len([
                        m
                        for m in dir(cls)
                        if m.startswith("test") and callable(getattr(cls, m))
                    ])
                    for cls in test_case_classes
                )
                total_tests += file_tests

        self.assertGreaterEqual(
            total_tests,
            SUITE_TEST_COUNT_FLOOR,
            f"Total collected tests ({total_tests}) fell below checked-in floor ({SUITE_TEST_COUNT_FLOOR})",
        )


if __name__ == "__main__":
    unittest.main()
