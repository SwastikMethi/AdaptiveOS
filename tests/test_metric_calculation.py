import json
from pathlib import Path
import subprocess
import sys
import unittest

from adaptive_os.workloads import WorkloadProfile, parse_workload_line
from adaptive_os.telemetry import (WindowAccumulator, completion_lateness_ms,
                                   deadline_miss_rate, nearest_rank_percentile, parse_psi_total)

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


class MetricTest(unittest.TestCase):
    def test_hand_calculated_percentiles_lateness_and_miss_rate(self):
        self.assertEqual(nearest_rank_percentile([0, 1, 2, 100], 0.95), 100)
        self.assertEqual(nearest_rank_percentile([8], 0.95), 8)
        self.assertEqual(completion_lateness_ms({"completion_ns": 25, "deadline_ns": 20}), 0.000005)
        self.assertEqual(completion_lateness_ms({"completion_ns": 19, "deadline_ns": 20}), 0)
        self.assertEqual(deadline_miss_rate([True, False, False, True]), 0.5)
        self.assertEqual(parse_psi_total("some avg10=1.2 avg60=0 avg300=0 total=123\n"), 123)
        with self.assertRaises(ValueError):
            nearest_rank_percentile([], 0.95)

    def populated_accumulator(self):
        accumulator = WindowAccumulator(period_ns=20_000_000)
        for sequence in range(100):
            scheduled = sequence * 20_000_000
            completion = scheduled + (21_000_000 if sequence == 99 else 10_000_000)
            event = tick(sequence=sequence, start_ns=0, scheduled_ns=scheduled, wakeup_ns=scheduled,
                         completion_ns=completion, monotonic_ns=completion, deadline_ns=scheduled+20_000_000,
                         deadline_met=sequence != 99, process_cpu_ns=(sequence+1)*10_000_000)
            accumulator.add(event, received_ns=5_000_000_000)
        for kind in ("foreground_summary", "background_summary"):
            accumulator.add({"schema_version": 1, "type": kind, "sequence": 0, "start_ns": 0,
                             "monotonic_ns": 2_010_000_000, "process_cpu_ns": 1_005_000_000,
                             "interval_start_ns": 0, "interval_ns": 2_010_000_000,
                             "work_units": 20100, "checksum": "ab"*32}, received_ns=5_000_000_000)
        return accumulator

    def test_p95_zero_does_not_hide_misses_and_uses_host_receipt_clock(self):
        state = self.populated_accumulator().build_window(0, 2_000_000_000, now_ns=5_100_000_000)
        self.assertTrue(state.valid, state.invalid_reasons)
        self.assertEqual(state.foreground_p95_completion_lateness_ms, 0)
        self.assertEqual(state.foreground_deadline_miss_rate, 0.01)
        self.assertAlmostEqual(state.foreground_cpu_percent, 50)
        self.assertAlmostEqual(state.background_work_units_per_second, 10000)
        self.assertAlmostEqual(state.data_age_sec, 0.1)

    def test_empty_missing_stale_and_duplicate_samples_cannot_look_healthy(self):
        empty = WindowAccumulator(period_ns=20_000_000)
        self.assertFalse(empty.build_window(0, 1_000_000_000, now_ns=0).valid)
        accumulator = self.populated_accumulator()
        self.assertFalse(accumulator.add(tick(), received_ns=100_000_000_000))
        stale = accumulator.build_window(0, 2_000_000_000, now_ns=8_000_000_000)
        self.assertFalse(stale.valid)
        self.assertIn("stale telemetry", stale.invalid_reasons)
        missing = accumulator.build_window(2_000_000_000, 3_000_000_000, now_ns=5_100_000_000)
        self.assertFalse(missing.valid)
        self.assertIn("incomplete background coverage", missing.invalid_reasons)


if __name__ == "__main__":
    unittest.main()
