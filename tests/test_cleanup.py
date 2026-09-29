import json
import subprocess
import unittest

from adaptive_os.docker_runtime import CleanupError, DockerError, DockerRuntime, OwnershipError

RUN = "12345678-1234-4234-8234-123456789abc"
CID = "a" * 64
OTHER = "b" * 64


def metadata(shares=1024, *, run_id=RUN, project="adaptive-os-mvp", running=True):
    return json.dumps({"labels": {"project": project, "run_id": run_id},
                       "shares": shares, "running": running, "cpuset": "2"})


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


if __name__ == "__main__":
    unittest.main()
