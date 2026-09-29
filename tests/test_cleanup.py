import json
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from adaptive_os.docker_runtime import CleanupError, DockerError, DockerRuntime, OwnershipError
from adaptive_os.docker_runtime import parse_affinity, parse_docker_info, run_preflight, ShareUpdate

RUN = "12345678-1234-4234-8234-123456789abc"
CID = "a" * 64
OTHER = "b" * 64


def metadata(shares=1024, *, run_id=RUN, project="adaptive-os-mvp", running=True, exit_code=0, oom_killed=False):
    return json.dumps({"labels": {"project": project, "run_id": run_id},
                       "shares": shares, "running": running, "cpuset": "2",
                       "exit_code": exit_code, "oom_killed": oom_killed})


class FakeExecutor:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        assert isinstance(command, list)
        assert kwargs == {"check": False, "capture_output": True, "text": True,
                          "timeout": kwargs["timeout"]}
        assert 0 < kwargs["timeout"] <= 180
        result = next(self.outputs)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            return subprocess.CompletedProcess(command, *result)
        return subprocess.CompletedProcess(command, 0, result, "")


class CleanupTest(unittest.TestCase):
    def test_wait_checks_actual_container_exit_and_oom_state(self):
        fake = FakeExecutor([metadata(), "137", metadata(running=False, exit_code=137, oom_killed=True)])
        state = DockerRuntime(fake).wait_owned(CID, expected_run_id=RUN)
        self.assertEqual(state["exit_code"], 137)
        self.assertTrue(state["oom_killed"])
        self.assertEqual(fake.commands[1], ["docker", "wait", CID])

    def test_launch_sets_labels_and_relative_shares_without_quota(self):
        fake = FakeExecutor([CID, metadata(), CID])
        self.assertEqual(DockerRuntime(fake).run_owned(RUN, 2, ["python", "-c", "while True: pass"]), CID)
        command = fake.commands[0]
        for flag, value in (("--cpuset-cpus", "2"), ("--cpu-shares", "1024")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("project=adaptive-os-mvp", command)
        self.assertIn(f"run_id={RUN}", command)
        self.assertNotIn("--cpus", command)
        self.assertNotIn("--privileged", command)

    def test_update_refuses_either_mismatched_label(self):
        for data in (metadata(project="other"), metadata(run_id="other"), '{"labels":null}', "[]"):
            with self.subTest(data=data):
                fake = FakeExecutor([data])
                with self.assertRaises(DockerError):
                    DockerRuntime(fake).update_shares(CID, 128, expected_run_id=RUN)
                self.assertEqual(len(fake.commands), 1)

    def test_update_verifies_effective_weight_and_records_original(self):
        fake = FakeExecutor([metadata(), "39", CID, metadata(128), "5"])
        update = DockerRuntime(fake).update_shares(CID, 128, expected_run_id=RUN)
        self.assertEqual((update.original_shares, update.requested_shares, update.effective_weight),
                         (1024, 128, 5))

    def test_unchanged_effective_weight_is_not_success(self):
        fake = FakeExecutor([metadata(), "39", CID, metadata(128), "39"])
        with self.assertRaisesRegex(DockerError, "weight"):
            DockerRuntime(fake).update_shares(CID, 128, expected_run_id=RUN)

    def test_cleanup_restores_then_removes_only_exact_owned_container(self):
        fake = FakeExecutor([CID, metadata(128), metadata(128), "5", CID,
                             metadata(), "39", metadata(), CID])
        self.assertEqual(DockerRuntime(fake).cleanup_run(RUN), [CID])
        self.assertEqual(fake.commands[0], ["docker", "ps", "-aq", "--no-trunc", "--filter",
                         "label=project=adaptive-os-mvp", "--filter", f"label=run_id={RUN}"])
        mutations = [command for command in fake.commands if command[1] in ("update", "rm")]
        self.assertEqual(mutations, [["docker", "update", "--cpu-shares", "1024", CID],
                                    ["docker", "rm", "-f", CID]])

    def test_cleanup_does_not_update_already_balanced_container(self):
        fake = FakeExecutor([CID, metadata(), metadata(), CID])
        self.assertEqual(DockerRuntime(fake).cleanup_run(RUN), [CID])
        self.assertFalse(any(command[1] == "update" for command in fake.commands))

    def test_cleanup_rechecks_filters_and_refuses_foreign_container(self):
        fake = FakeExecutor([CID, metadata(project="another-project")])
        with self.assertRaises(CleanupError) as caught:
            DockerRuntime(fake).cleanup_run(RUN)
        self.assertEqual(caught.exception.removed, [])
        self.assertFalse(any(command[1] in ("update", "rm") for command in fake.commands))

    def test_cleanup_continues_after_failed_restore_and_reports_it(self):
        fake = FakeExecutor([f"{CID}\n{OTHER}", metadata(128), metadata(128), "5",
                             (1, "", "daemon refused update"), metadata(128), CID,
                             metadata(), metadata(), OTHER])
        with self.assertRaises(CleanupError) as caught:
            DockerRuntime(fake).cleanup_run(RUN)
        self.assertEqual(caught.exception.removed, [CID, OTHER])
        self.assertIn(CID, str(caught.exception))
        self.assertIn("daemon refused update", str(caught.exception))

    def test_command_errors_are_bounded_and_preserve_stderr(self):
        cases = [(1, "", "daemon unavailable"), subprocess.TimeoutExpired(["docker"], 1),
                 FileNotFoundError("docker missing")]
        for result in cases:
            with self.subTest(result=result):
                runtime = DockerRuntime(FakeExecutor([result]))
                with self.assertRaises(DockerError):
                    runtime._run(["version"])
                self.assertEqual(len(runtime.commands), 1)

    def test_bad_run_id_container_id_and_action_fail_before_docker(self):
        fake = FakeExecutor([])
        runtime = DockerRuntime(fake)
        for run_id, cid, shares in (("", CID, 128), (RUN, "--all", 128), (RUN, CID, 0)):
            with self.subTest(run_id=run_id, cid=cid, shares=shares):
                with self.assertRaises(ValueError):
                    runtime.update_shares(cid, shares, expected_run_id=run_id)
        self.assertEqual(fake.commands, [])


class PreflightRuntime:
    """Local state double for preflight; real command safety is covered above."""
    def __init__(self, *, info="linux 2", fail_update=False, fail_cleanup=False, psi=True, stop=None):
        self.info = info
        self.fail_update = fail_update
        self.fail_cleanup = fail_cleanup
        self.psi = psi
        self.stop = stop
        self.commands = []
        self.containers = {}
        self.updates = []
        self.cleaned = False

    def _run(self, args, **kwargs):
        if args[0] == "version":
            return '{"Client":{"Version":"29","Context":"desktop-linux"},"Server":{"Version":"29"}}'
        if args[0] == "context":
            return '"unix:///test/docker.sock"'
        if args[0] == "info":
            return self.info if args[2] == "{{.OSType}} {{.CgroupVersion}}" else '{"cpus":4,"memory_bytes":8000000000,"kernel":"linux"}'
        if args[:2] == ["image", "inspect"]:
            return '{"id":"sha256:test","architecture":"arm64","os":"linux"}'
        if args[0] == "ps":
            return ""
        raise AssertionError(args)

    def run_owned(self, run_id, cpu, command, **kwargs):
        cid = CID if cpu is None else OTHER
        self.containers[cid] = {"shares": 1024, "cpu": cpu}
        return cid

    def inspect_owned(self, cid, run_id):
        cpu = self.containers[cid]["cpu"]
        return {"shares": self.containers[cid]["shares"], "running": True, "cpuset": str(cpu)}

    def exec_owned(self, cid, command, **kwargs):
        if "sched_getaffinity" in command[-1]:
            return "[2, 4, 6]" if cid == CID else "[2]"
        if not self.psi:
            raise DockerError("/proc/pressure/cpu: No such file")
        return "some avg10=0.00 avg60=0.00 avg300=0.00 total=12345\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0"

    def read_cpu_weight(self, cid, **kwargs):
        return 39 if self.containers[cid]["shares"] == 1024 else 5

    def update_shares(self, cid, shares, **kwargs):
        original = self.containers[cid]["shares"]
        self.containers[cid]["shares"] = shares
        self.updates.append(shares)
        if shares == 128 and self.stop:
            self.stop.set()
        if shares == 128 and self.fail_update:
            raise DockerError("effective cpu.weight did not change")
        return ShareUpdate(original, shares, 39 if shares == 1024 else 5, cid)

    def cleanup_run(self, run_id):
        self.cleaned = True
        for cid, state in self.containers.items():
            if state["shares"] != 1024:
                self.update_shares(cid, 1024)
        removed = list(self.containers)
        self.containers.clear()
        if self.fail_cleanup:
            raise CleanupError([], ["remove failed"])
        return removed


class PreflightTest(unittest.TestCase):
    def test_remote_context_override_cannot_be_masked_by_local_docker_host(self):
        runtime = PreflightRuntime()
        original_run = runtime._run

        def output(args, **kwargs):
            return '"ssh://remote.example"' if args[0] == "context" else original_run(args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"DOCKER_HOST": "unix:///local.sock", "DOCKER_CONTEXT": "remote"}), \
                patch.object(runtime, "_run", side_effect=output):
            result = run_preflight(runtime, Path(directory), run_id=RUN)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(runtime.containers, {})

    def test_capability_parsers_reject_malformed_data(self):
        self.assertEqual(parse_docker_info("linux 2\n"), ("linux", "2"))
        self.assertEqual(parse_affinity("[2, 4, 6]\n"), [2, 4, 6])
        for data in ("[]", "[true]", "[-1]", "[2,2]", "null", "[1.5]", "[4,2]"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_affinity(data)
        with self.assertRaises(ValueError):
            parse_docker_info("windows 2")

    def test_preflight_selects_allowed_cpu_proves_restore_and_persists(self):
        runtime = PreflightRuntime()
        with tempfile.TemporaryDirectory() as directory:
            result = run_preflight(runtime, Path(directory), run_id=RUN)
            data = json.loads(result.path.read_text())
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(data["supported"])
        self.assertEqual(data["selected_cpu"], 2)
        self.assertEqual(data["smoke"]["initial_weight"], data["smoke"]["restored_weight"])
        self.assertEqual(runtime.updates, [128, 1024])
        self.assertTrue(runtime.cleaned)

    def test_cgroup_v1_and_missing_psi_are_explicitly_unsupported(self):
        for runtime in (PreflightRuntime(info="linux 1"), PreflightRuntime(psi=False)):
            with tempfile.TemporaryDirectory() as directory:
                result = run_preflight(runtime, Path(directory), run_id=RUN)
                data = json.loads(result.path.read_text())
            self.assertEqual(result.exit_code, 2)
            self.assertFalse(data["supported"])
            self.assertTrue(data["reasons"])

    def test_failed_update_and_interruption_restore_before_cleanup(self):
        stop = threading.Event()
        for runtime in (PreflightRuntime(fail_update=True), PreflightRuntime(stop=stop)):
            with tempfile.TemporaryDirectory() as directory:
                result = run_preflight(runtime, Path(directory), run_id=RUN, stop_event=stop)
                data = json.loads(result.path.read_text())
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(data["supported"])
            self.assertEqual(runtime.updates, [128, 1024])
            self.assertTrue(runtime.cleaned)

    def test_cleanup_failure_cannot_be_reported_as_supported(self):
        runtime = PreflightRuntime(fail_cleanup=True)
        with tempfile.TemporaryDirectory() as directory:
            result = run_preflight(runtime, Path(directory), run_id=RUN)
            data = json.loads(result.path.read_text())
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(data["supported"])
        self.assertIn("remove failed", " ".join(data["reasons"]))


if __name__ == "__main__":
    unittest.main()
