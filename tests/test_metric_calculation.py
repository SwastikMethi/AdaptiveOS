import json
from pathlib import Path
import subprocess
import sys
import unittest

from adaptive_os.workloads import WorkloadProfile, parse_workload_line

ROOT = Path(__file__).resolve().parents[1]


def tick(**changes):
    event = {"schema_version": 1, "type": "foreground_tick", "sequence": 0,
             "start_ns": 100, "monotonic_ns": 120, "process_cpu_ns": 5,
             "scheduled_ns": 100, "wakeup_ns": 101, "completion_ns": 120,
             "deadline_ns": 130, "work_units": 10, "deadline_met": True,
             "skipped": False, "checksum": "ab" * 32}
    return {**event, **changes}


class WorkloadTest(unittest.TestCase):
    def test_scripts_emit_recomputable_ticks_and_throughput(self):
        for workload in ("foreground", "background"):
            result = subprocess.run([sys.executable, str(ROOT / "workloads" / f"{workload}.py"),
                                     "--duration-sec", "1.2"], capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [parse_workload_line(line) for line in result.stdout.splitlines()]
            self.assertTrue(events)
            summaries = [event for event in events if event["type"] == f"{workload}_summary"]
            self.assertGreaterEqual(len(summaries), 2)
            self.assertGreater(sum(event["work_units"] for event in summaries), 0)
            if workload == "foreground":
                ticks = [event for event in events if event["type"] == "foreground_tick"]
                self.assertEqual(len(ticks), 60)
                self.assertEqual([event["sequence"] for event in ticks], list(range(60)))
                self.assertEqual(ticks[-1]["scheduled_ns"] - ticks[0]["scheduled_ns"], 1_180_000_000)

    def test_schema_rejects_invalid_types_and_impossible_times(self):
        self.assertEqual(parse_workload_line(json.dumps(tick()))["work_units"], 10)
        invalid = [[], tick(schema_version=True), tick(type="unknown"), tick(sequence=-1),
                   tick(work_units=True), tick(wakeup_ns=99), tick(completion_ns=90),
                   tick(deadline_met=False), tick(process_cpu_ns=-1), tick(checksum="bad")]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ValueError):
                parse_workload_line(json.dumps(event))

    def test_workload_arguments_reject_nonfinite_and_unusable_budgets(self):
        for budget in (float("nan"), 0, -1, 20):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                WorkloadProfile("invalid", 20, budget)
        result = subprocess.run([sys.executable, str(ROOT / "workloads/foreground.py"),
                                 "--duration-sec", "nan"], capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
