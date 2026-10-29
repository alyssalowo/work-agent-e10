import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import task_usage


def task(**overrides):
    result = {
        "task_id": "run-001",
        "task_type": "matching",
        "model": "gpt-6.1-terra",
        "reasoning_effort": "high",
        "actual_quota": {"value": 18.0, "unit": "percent-used", "basis_id": "weekly-window-v1", "evidence_ref": "usage-snapshot.json#/run-001"},
        "subagent_calls": 1,
        "mcp_reads": 4,
        "mcp_writes": 1,
        "rework_count": 0,
        "qualified": True,
        "gates": {"scope_complete": True, "receipt_confirmed": True},
    }
    result.update(overrides)
    return result


def baseline(**overrides):
    result = {
        "task_type": "matching",
        "actual_quota": {"value": 25.0, "unit": "percent-used", "basis_id": "weekly-window-v1", "evidence_ref": "usage-snapshot.json#/baseline-matching"},
        "qualified": True,
        "gates": {"scope_complete": True},
    }
    result.update(overrides)
    return result


class TaskUsageTests(unittest.TestCase):
    def test_computes_delta_only_for_qualified_same_basis_actual_values(self):
        result = task_usage.summarize({"baseline": [baseline()], "tasks": [task()]})
        item = result["tasks"][0]["comparison"]
        self.assertEqual(item["status"], "comparable")
        self.assertEqual(item["delta"], 7.0)
        self.assertEqual(item["unit"], "percent-used")
        self.assertEqual(result["comparable_count"], 1)

    def test_missing_baseline_is_indeterminate(self):
        item = task_usage.summarize({"baseline": [], "tasks": [task()]})["tasks"][0]
        self.assertEqual(item["comparison"]["status"], "baseline_missing")
        self.assertIsNone(item["comparison"]["delta"])

    def test_null_actual_quota_is_not_replaced_by_a_proxy(self):
        quota = {"value": None, "unit": "percent-used", "basis_id": "weekly-window-v1", "evidence_ref": None}
        result = task_usage.summarize({"baseline": [baseline()], "tasks": [task(actual_quota=quota)]})
        item = result["tasks"][0]["comparison"]
        self.assertEqual(item["status"], "actual_quota_unavailable")
        self.assertIsNone(item["delta"])

    def test_different_basis_or_unit_is_incomparable(self):
        other = baseline(actual_quota={"value": 25, "unit": "percent-used", "basis_id": "five-hour-window", "evidence_ref": "usage.json#/baseline"})
        result = task_usage.summarize({"baseline": [other], "tasks": [task()]})
        self.assertEqual(result["tasks"][0]["comparison"]["status"], "incomparable_quota_basis")

    def test_unqualified_task_never_gets_numeric_comparison(self):
        result = task_usage.summarize({"baseline": [baseline()], "tasks": [task(qualified=False)]})
        item = result["tasks"][0]["comparison"]
        self.assertEqual(item["status"], "task_not_qualified")
        self.assertIsNone(item["delta"])

    def test_qualified_true_with_failed_gate_is_rejected(self):
        with self.assertRaisesRegex(task_usage.UsageError, "gate failed"):
            task_usage.summarize({"baseline": [baseline()], "tasks": [task(gates={"scope_complete": False})]})

    def test_numeric_quota_requires_evidence_reference(self):
        quota = {"value": 18, "unit": "percent-used", "basis_id": "weekly-window-v1", "evidence_ref": None}
        with self.assertRaisesRegex(task_usage.UsageError, "evidence_ref"):
            task_usage.summarize({"baseline": [baseline()], "tasks": [task(actual_quota=quota)]})

    def test_cli_reads_manual_json_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            output = root / "out" / "summary.json"
            source.write_text(json.dumps({"baseline": [baseline()], "tasks": [task()]}), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "task_usage.py"), "summarize", "--input", str(source), "--out", str(output)],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["comparable_count"], 1)
            self.assertIn("no token or prompt-length proxy", saved["comparison_policy"])


if __name__ == "__main__":
    unittest.main()
