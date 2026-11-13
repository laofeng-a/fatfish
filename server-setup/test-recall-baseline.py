"""Fixture shape and exact-query sanity for the offline recall baseline."""
import json
import unittest
from pathlib import Path
import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "recall_baseline", Path(__file__).with_name("tests") / "recall_baseline.py"
)
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)


class RecallBaselineTests(unittest.TestCase):
    def test_fixture_files_and_exact_hits(self):
        report = baseline.run(reps=1)
        self.assertGreaterEqual(len(report["cases"]), 20)
        self.assertLessEqual(len(report["cases"]), 40)
        tools = {row["tool"] for row in report["cases"]}
        self.assertEqual(tools, set(baseline.TOOLS))
        for tool in baseline.TOOLS:
            summary = report["tools"][tool]
            self.assertEqual(summary["exact_hit_at_1"], 1)
            self.assertEqual(summary["exact_hit_at_5"], 1)
            self.assertIn("paraphrase_hit_at_5", summary)
            self.assertIn("distractor_false_positive_rate", summary)
            self.assertGreaterEqual(summary["zero_result_count"], 0)
            self.assertIsNotNone(summary["latency_ms"]["p50"])
        committed = baseline.FIXTURES / "baseline_results.json"
        if committed.exists():
            previous = json.loads(committed.read_text(encoding="utf-8"))
            self.assertEqual(baseline.comparable(previous), baseline.comparable(report))


if __name__ == "__main__":
    unittest.main()
