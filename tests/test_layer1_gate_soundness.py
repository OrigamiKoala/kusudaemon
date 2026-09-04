"""PLAN.md §7 / TEST-PLAN.md §1.3 — Gate false-accept corpus.

Pure, hermetic soundness benchmarks for evaluate_gates across all 10 gate kinds:
exists, nonempty, len, max_tokens, contains, headers_std, problems_min,
terms_defined, latex_balanced, refs_resolve.

For each gate kind: 5 must-pass artifacts and 5 adversarial must-fail artifacts.
Asserts FA == 0 on the adversarial set and FR == 0 on the valid set.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kusudaemon.v1.gates import evaluate_gates


class Layer1GateSoundnessTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def test_all_ten_gate_kinds_soundness(self) -> None:
        # Create temp files for exists and terms_defined fixtures
        existing_file = self.tmp_path / "existing.txt"
        existing_file.write_text("content", encoding="utf-8")
        existing_file_2 = self.tmp_path / "existing2.md"
        existing_file_2.write_text("more content", encoding="utf-8")

        glossary_file = self.tmp_path / "glossary.json"
        glossary_data = {
            "entropy": "A measure of disorder.",
            "enthalpy": "Total heat content.",
            "gibbs free energy": "Thermodynamic potential.",
        }
        glossary_file.write_text(json.dumps(glossary_data), encoding="utf-8")

        corpus: dict[str, dict[str, list[tuple[str, str]]]] = {
            "exists": {
                "must_pass": [
                    ("exists", "Valid content"),
                    ("exists", "# Header\nSome content."),
                    ("exists", "1"),
                    (f"exists:{existing_file}", "anything"),
                    (f"exists:{existing_file_2}", ""),
                ],
                "must_fail": [
                    (f"exists:{self.tmp_path / 'nonexistent_xyz_123.txt'}", "something"),
                    (f"exists:{self.tmp_path / 'deep/nested/missing.md'}", "something"),
                    (f"exists:{self.tmp_path / 'missing.json'}", "something"),
                    (f"exists:{self.tmp_path / 'also_missing.md'}", "something"),
                    ("exists", None),  # type: ignore[arg-type]
                ],
            },
            "nonempty": {
                "must_pass": [
                    ("nonempty", "hello world"),
                    ("nonempty", "# Section\nContent here."),
                    ("nonempty", "."),
                    ("nonempty", "\n\nword\n\n"),
                    ("nonempty", "   leading and trailing spaces   "),
                ],
                "must_fail": [
                    ("nonempty", ""),
                    ("nonempty", "   "),
                    ("nonempty", "\n\n\t\n"),
                    ("nonempty", "\r\n\t  \r\n"),
                    ("nonempty", "    \t   \n"),
                ],
            },
            "len": {
                "must_pass": [
                    ("len:5-10", "one two three four five"),
                    ("len:5-10", "one two three four five six"),
                    ("len:5-10", "one two three four five six seven"),
                    ("len:5-10", "one two three four five six seven eight nine"),
                    ("len:5-10", "one two three four five six seven eight nine ten"),
                ],
                "must_fail": [
                    ("len:5-10", "one two three four"),  # 4 words (under minimum)
                    ("len:5-10", "one two three four five six seven eight nine ten eleven"),  # 11 words (over maximum)
                    ("len:5-10", ""),  # 0 words
                    ("len:malformed", "one two three four five six"),  # malformed range
                    ("len:10-5", "one two three four five six seven"),  # inverted range
                ],
            },
            "max_tokens": {
                "must_pass": [
                    ("max_tokens:10", ""),
                    ("max_tokens:10", "one two"),
                    ("max_tokens:10", "hello world test"),
                    ("max_tokens:10", "one two three four"),
                    ("max_tokens:10", "one two three four five six"),
                ],
                "must_fail": [
                    ("max_tokens:10", "one two three four five six seven eight nine ten eleven twelve"),
                    ("max_tokens:10", "word " * 50),
                    ("max_tokens:10", "a b c d e f g h i j k l m n o p q r s t u v w x y z"),
                    ("max_tokens:bad_int", "hello"),
                    ("max_tokens:10", "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt"),
                ],
            },
            "contains": {
                "must_pass": [
                    ("contains:TARGET", "This has TARGET inside."),
                    ("contains:TARGET", "TARGET"),
                    ("contains:TARGET", "Prefix TARGET Suffix"),
                    ("contains:TARGET", "# Section\n\nTARGET\nMore details."),
                    ("contains:TARGET", "TARGET is at start"),
                ],
                "must_fail": [
                    ("contains:TARGET", "This has ТАRGET inside."),  # Cyrillic lookalike U+0422 U+0410
                    ("contains:TARGET", "This has target inside."),  # Wrong case
                    ("contains:TARGET", "This has T\u200bA\u200bR\u200bG\u200bE\u200bT inside."),  # Zero-width spaces
                    ("contains:TARGET", "This has T ARGET inside."),  # Interrupted with space
                    ("contains:TARGET", "This has unrelated content entirely."),
                ],
            },
            "headers_std": {
                "must_pass": [
                    ("headers:std", "# Heading 1\n## Heading 2\nProse"),
                    ("headers:std", "# Title\n## Section A\n### Subsection 1\n## Section B"),
                    ("headers:std", "## Direct H2\n### Child H3\nProse"),
                    ("headers:std", "# A\n## B\n# C\n## D"),
                    ("headers:std", "# Only Top Level"),
                ],
                "must_fail": [
                    ("headers:std", "No markdown headings at all in this whole text."),
                    ("headers:std", "# Level 1\n### Level 3 skips level 2"),
                    ("headers:std", "# Level 1\n#### Level 4 skips levels 2 and 3"),
                    ("headers:std", "## Level 2\n#### Level 4 skips level 3"),
                    ("headers:std", "# A\n## B\n#### D skips level 3 after valid start"),
                ],
            },
            "problems_min": {
                "must_pass": [
                    ("problems>=2", "## Problem 1\nSolve for x.\n## Problem 2\nSolve for y."),
                    ("problems>=2", "## Exercise 1\nDo this.\n## Exercise 2\nDo that."),
                    ("problems>=2", "## Worked Example 1\nStep 1.\n## Example 2\nStep 2."),
                    ("problems>=2", "### 1. Problem A\nDetails\n### 2. Exercise B\nDetails\n### 3. Example C\nDetails"),
                    ("problems>=2", "## Problem 1.1\nText\n## Problem 1.2\nText"),
                ],
                "must_fail": [
                    ("problems>=2", "## Problem 1\nOnly one problem heading."),
                    ("problems>=2", "In prose, Problem 1 is simple and Problem 2 is hard."),  # No headings
                    ("problems>=2", "## Types of Waves\nContent\n## Frequency Analysis\nContent"),  # Non-problem headings
                    ("problems>=2", ""),  # Empty
                    ("problems>=malformed", "## Problem 1\n## Problem 2"),  # Malformed arg
                ],
            },
            "terms_defined": {
                "must_pass": [
                    (f"terms_defined:{glossary_file}", "No defined terms at all in this artifact."),
                    (f"terms_defined:{glossary_file}", "Here is **entropy** and [[enthalpy]]."),
                    (f"terms_defined:{glossary_file}", "**gibbs free energy** is defined."),
                    (f"terms_defined:{glossary_file}", "Case insensitive: **ENTROPY**."),
                    (f"terms_defined:{glossary_file}", "Both [[entropy]] and **enthalpy** in prose."),
                ],
                "must_fail": [
                    (f"terms_defined:{glossary_file}", "Here is an unregistered term: **flux_capacitor**."),
                    (f"terms_defined:{glossary_file}", "Here is a bracket term: [[quantum_foam]]."),
                    (f"terms_defined:{glossary_file}", "Partial match: **gibbs** alone."),
                    (f"terms_defined:{self.tmp_path / 'missing_glossary.json'}", "Here is **entropy**."),
                    (f"terms_defined:{glossary_file}", "Multiple terms: **entropy** is fine but **bogus_term** fails."),
                ],
            },
            "latex_balanced": {
                "must_pass": [
                    ("latex_balanced", "Simple inline $x + y = z$ math."),
                    ("latex_balanced", "Block math: $$E = mc^2$$ and display \\[a = b\\]."),
                    ("latex_balanced", "\\begin{equation}x = 1\\end{equation}"),
                    ("latex_balanced", "\\begin{theorem}\\begin{proof}x\\end{proof}\\end{theorem}"),
                    ("latex_balanced", "Inline paren math \\(x > 0\\) and multiple $a$ and $b$."),
                ],
                "must_fail": [
                    ("latex_balanced", "Unclosed single dollar: $x + y = z"),
                    ("latex_balanced", "Unclosed double dollar: $$x + y = z"),
                    ("latex_balanced", "Unclosed parens: \\(x + y"),
                    ("latex_balanced", "Unbalanced brackets: \\[x + y"),
                    ("latex_balanced", "\\begin{a}\\begin{b}\\end{a}\\end{b}"),  # Adversarial: balanced counts, misnested!
                ],
            },
            "refs_resolve": {
                "must_pass": [
                    ("refs_resolve", "No references in this document."),
                    ("refs_resolve", "See [ref:methods] for details.\n\n## Methods\nHere are methods."),
                    ("refs_resolve", "See [ref:1] for info.\n\n[#1] Definition of 1."),
                    ("refs_resolve", "See [ref:analysis].\n\n# Analysis\nResults here."),
                    ("refs_resolve", "Citation [ref:sec-intro] resolves to [sec-intro] anchor."),
                ],
                "must_fail": [
                    ("refs_resolve", "See [ref:missing] for details."),
                    ("refs_resolve", "As mentioned in [ref:methods], the methods are in prose with no heading."),  # Adversarial
                    ("refs_resolve", "Invalid citation [ref:chapter99].\n## Chapter 1\nDetails"),
                    ("refs_resolve", "See [ref:appendix-z].\n# Appendix A\n# Appendix B"),
                    ("refs_resolve", "References [ref:part1] and [ref:part2], but only ## Part 1 exists."),
                ],
            },
        }

        total_pass_tested = 0
        total_fail_tested = 0
        false_accepts = 0
        false_rejects = 0

        for gate_kind, sets in corpus.items():
            must_pass = sets["must_pass"]
            must_fail = sets["must_fail"]
            self.assertEqual(len(must_pass), 5, f"{gate_kind} must have 5 must-pass artifacts")
            self.assertEqual(len(must_fail), 5, f"{gate_kind} must have 5 must-fail artifacts")

            for i, (gate_str, artifact) in enumerate(must_pass):
                with self.subTest(gate=gate_kind, category="must_pass", index=i):
                    results = evaluate_gates([gate_str], artifact)
                    self.assertEqual(len(results), 1)
                    passed = results[0].passed
                    total_pass_tested += 1
                    if not passed:
                        false_rejects += 1
                    self.assertTrue(passed, f"{gate_kind} must-pass #{i} rejected: {results[0].detail}")

            for i, (gate_str, artifact) in enumerate(must_fail):
                with self.subTest(gate=gate_kind, category="must_fail", index=i):
                    results = evaluate_gates([gate_str], artifact)
                    self.assertEqual(len(results), 1)
                    passed = results[0].passed
                    total_fail_tested += 1
                    if passed:
                        false_accepts += 1
                    self.assertFalse(passed, f"{gate_kind} adversarial #{i} falsely accepted")

        fa_rate = false_accepts / total_fail_tested if total_fail_tested else 0.0
        fr_rate = false_rejects / total_pass_tested if total_pass_tested else 0.0

        # Headline assertion from TEST-PLAN.md §1.3:
        # "Report false-accept and false-reject rates as numbers; assert FA == 0 on the adversarial set."
        self.assertEqual(
            false_accepts,
            0,
            f"False accepts detected: {false_accepts}/{total_fail_tested} (FA rate={fa_rate:.2%})",
        )
        self.assertEqual(
            false_rejects,
            0,
            f"False rejects detected: {false_rejects}/{total_pass_tested} (FR rate={fr_rate:.2%})",
        )


if __name__ == "__main__":
    unittest.main()
