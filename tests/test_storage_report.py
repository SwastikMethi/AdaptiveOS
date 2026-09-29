import json
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adaptive_os.storage import RunStore
from adaptive_os.workloads import TrialConfig, WorkloadProfile, run_trial, select_profiles
from adaptive_os.docker_runtime import DockerError
from adaptive_os.telemetry import WindowState

RUN = "12345678-1234-4234-8234-123456789abc"


class StorageTest(unittest.TestCase):
    def test_persists_auditable_records_and_rejects_orphans_and_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.sqlite3"
            with RunStore(path) as store:
                store.start_run(RUN, "default", "light", 0, 42, {}, {})
                store.sample(RUN, 100, "foreground_tick", {"lateness": 0})
                store.event(RUN, 0, "start", {})
                store.decision(RUN, 0, {}, proposed_action="balanced", applied_action="balanced", reason="default")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.sample("unknown", 100, "foreground_tick", {})
                with self.assertRaises(ValueError):
                    store.decision(RUN, 1, {"bad": float("nan")}, proposed_action="balanced", applied_action="balanced", reason="default")
                store.finish_run(RUN, True, None, {"metrics": {"miss_rate": 0}})
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT valid FROM runs").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT run_id FROM samples").fetchone()[0], RUN)

    def test_failed_launch_still_cleans_and_records_failure(self):
        class Runtime:
            commands = []
            cleaned = False

            def run_owned(self, *args, **kwargs):
                raise DockerError("start failed after create")

            def cleanup_run(self, run_id):
                self.cleaned = run_id == RUN
                return []

        runtime = Runtime()
        config = TrialConfig(WorkloadProfile("light", 20, 4), cpu=2, run_id=RUN)
        with tempfile.TemporaryDirectory() as directory, RunStore(Path(directory) / "runs.sqlite3") as store:
            result = run_trial(config, runtime, store)
            events = [row[0] for row in store.connection.execute("SELECT event_type FROM events")]
        self.assertFalse(result.valid)
        self.assertIn("start failed", result.reason)
        self.assertTrue(runtime.cleaned)
        self.assertIn("failure", events)
        self.assertIn("cleanup", events)

    def test_calibration_requires_valid_solo_and_paired_evidence(self):
        rows = [{"budget_ms": 4, "solo": {"valid": True, "miss_rate": 0},
                 "paired": {"valid": True, "miss_rate": 0.01, "p95_lateness_ms": 0}},
                {"budget_ms": 6, "solo": {"valid": True, "miss_rate": 0.002},
                 "paired": {"valid": True, "miss_rate": 0.08, "p95_lateness_ms": 4}}]
        profiles = select_profiles(rows)
        self.assertEqual(profiles["light"]["work_budget_ms"], 4)
        self.assertEqual(profiles["contention"]["work_budget_ms"], 6)
        rows[1]["solo"]["valid"] = False
        with self.assertRaisesRegex(ValueError, "contention"):
            select_profiles(rows)


class LifecycleTest(unittest.TestCase):
    def run_fake_trial(self, *, exit_code=0, shutdown_error=False):
        class Clock:
            now = 0

            def is_set(self):
                return False

            def wait(self, seconds):
                self.now += round(seconds * 1_000_000_000)

        clock = Clock()

        class Runtime:
            commands = []
            cleaned = False

            def __init__(self):
                self.cpus = {}

            def run_owned(self, run_id, cpu, *args, **kwargs):
                cid = f"{len(self.cpus) + 1:064x}"
                self.cpus[cid] = cpu
                return cid

            def inspect_owned(self, cid, run_id):
                return {"cpuset": str(self.cpus[cid]), "shares": 1024, "running": clock.now < 8_000_000_000,
                        "exit_code": exit_code, "oom_killed": exit_code == 137}

            def exec_owned(self, cid, command, **kwargs):
                return "0" if command[0] == "python" else "some total=0"

            def wait_owned(self, cid, **kwargs):
                return self.inspect_owned(cid, RUN)

            def cleanup_run(self, run_id):
                self.cleaned = True
                return list(self.cpus)

        class Follower:
            returncode = 0

            def __init__(self):
                self.stdout = io.StringIO("")

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout):
                if shutdown_error:
                    raise subprocess.TimeoutExpired("docker logs", timeout)
                return 0

        class CompleteTelemetry:
            def __init__(self, *args, **kwargs):
                pass

            def build_window(self, *args, **kwargs):
                return WindowState(0, 0, 0, 100, 10, 90, 0, "balanced", 0, 0, True, ())

            def ticks(self, *args):
                return [{"completion_ns": 10, "deadline_ns": 20, "deadline_met": True,
                         "wakeup_ns": 0, "scheduled_ns": 0}]

        runtime = Runtime()
        config = TrialConfig(WorkloadProfile("test", 20, 1), 0, run_id=RUN, measurement_sec=5, warmup_sec=0)
        with tempfile.TemporaryDirectory() as directory, RunStore(Path(directory) / "runs.sqlite3") as store, \
                patch("adaptive_os.workloads.time.monotonic_ns", side_effect=lambda: clock.now), \
                patch("adaptive_os.workloads.subprocess.Popen", side_effect=lambda *a, **k: Follower()), \
                patch("adaptive_os.workloads.WindowAccumulator", CompleteTelemetry):
            result = run_trial(config, runtime, store, metadata={"allowed_cpus": [0, 1]}, stop_event=clock)
            events = [(kind, json.loads(payload)) for kind, payload in store.connection.execute("SELECT event_type,payload_json FROM events")]
        return result, runtime, events

    def test_shutdown_failure_cannot_skip_container_cleanup(self):
        result, runtime, _ = self.run_fake_trial(shutdown_error=True)
        self.assertTrue(runtime.cleaned)
        self.assertFalse(result.valid)
        self.assertIn("follower", result.reason)

    def test_failed_workload_exit_invalidates_complete_telemetry(self):
        result, runtime, events = self.run_fake_trial(exit_code=137)
        self.assertFalse(result.valid)
        self.assertTrue(runtime.cleaned)
        stops = [payload for kind, payload in events if kind == "workload_stop"]
        self.assertEqual(stops[0]["exit_code"], 137)

    def test_success_requires_normal_exit_and_probe_uses_spare_cpu(self):
        result, runtime, events = self.run_fake_trial()
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(next(iter(runtime.cpus.values())), 1)
        self.assertEqual(sum(kind == "workload_stop" for kind, _ in events), 2)
