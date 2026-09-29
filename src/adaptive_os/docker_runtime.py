"""Docker CLI boundary: mutations require both ownership labels and a full ID."""

from dataclasses import dataclass
import json
import re
import subprocess
import time
import uuid

PROJECT = "adaptive-os-mvp"
BASE_IMAGE = "python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b"
INSPECT_FORMAT = ('{"labels":{{json .Config.Labels}},"shares":{{json .HostConfig.CpuShares}},'
                  '"running":{{json .State.Running}},"cpuset":{{json .HostConfig.CpusetCpus}}}')


class DockerError(RuntimeError):
    """A failed command or unverifiable Docker response."""


class OwnershipError(DockerError):
    """The target does not belong to the expected experiment run."""


class CleanupError(DockerError):
    def __init__(self, removed: list[str], errors: list[str]):
        self.removed = removed
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class ShareUpdate:
    original_shares: int
    requested_shares: int
    effective_weight: int | None
    stdout: str


def validate_run_id(run_id: str) -> str:
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("run ID must be a canonical UUID") from exc
    return run_id


def validate_container_id(container_id: str) -> str:
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("expected a full Docker container ID")
    return container_id


class DockerRuntime:
    def __init__(self, executor=subprocess.run):
        self.executor = executor
        self.commands: list[dict] = []

    def _run(self, args: list[str], *, timeout: float = 15) -> str:
        command = ["docker", *args]
        record = {"argv": command}
        started = time.monotonic()
        try:
            result = self.executor(command, check=False, capture_output=True, text=True, timeout=timeout)
            record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
            if result.returncode:
                raise DockerError(f"docker {args[0]} failed ({result.returncode}): {result.stderr.strip()}")
            return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            record["error"] = str(exc)
            raise DockerError(f"docker {args[0]}: {exc}") from exc
        finally:
            record["duration_sec"] = time.monotonic() - started
            self.commands.append(record)

    def inspect_owned(self, container_id: str, expected_run_id: str) -> dict:
        validate_container_id(container_id)
        validate_run_id(expected_run_id)
        raw = self._run(["inspect", "--format", INSPECT_FORMAT, container_id])
        try:
            data = json.loads(raw)
            labels = data["labels"]
            if labels.get("project") != PROJECT or labels.get("run_id") != expected_run_id:
                raise OwnershipError(f"refusing container {container_id}: ownership labels do not match")
            if type(data["shares"]) is not int or type(data["running"]) is not bool:
                raise ValueError("invalid shares/running types")
            return data
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DockerError(f"invalid inspect response for {container_id}") from exc

    def run_owned(self, run_id: str, cpu: int | None, command: list[str], *, image: str = BASE_IMAGE) -> str:
        validate_run_id(run_id)
        if cpu is not None and (type(cpu) is not int or cpu < 0):
            raise ValueError("CPU must be a nonnegative integer")
        if not command or any(not isinstance(arg, str) for arg in command):
            raise ValueError("command must be a nonempty argument array")
        if not image or image.startswith("-"):
            raise ValueError("invalid image reference")
        args = ["create", "--label", f"project={PROJECT}", "--label", f"run_id={run_id}",
                "--cpu-shares", "1024", "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if cpu is not None:
            args += ["--cpuset-cpus", str(cpu)]
        container_id = self._run([*args, image, *command], timeout=180)
        try:
            validate_container_id(container_id)
        except ValueError as exc:
            raise DockerError("docker create returned an invalid container ID; recover using the run ID") from exc
        # Create separately so a failed start remains discoverable by exact labels.
        self.inspect_owned(container_id, run_id)
        self._run(["start", container_id])
        return container_id

    def inspect_shares(self, container_id: str, *, expected_run_id: str) -> int:
        return self.inspect_owned(container_id, expected_run_id)["shares"]

    def exec_owned(self, container_id: str, command: list[str], *, expected_run_id: str) -> str:
        self.inspect_owned(container_id, expected_run_id)
        return self._run(["exec", container_id, *command])

    def _read_weight(self, container_id: str) -> int:
        raw = self._run(["exec", container_id, "cat", "/sys/fs/cgroup/cpu.weight"])
        try:
            weight = int(raw)
            if not 1 <= weight <= 10000:
                raise ValueError
            return weight
        except ValueError as exc:
            raise DockerError(f"invalid cpu.weight for {container_id}: {raw!r}") from exc

    def read_cpu_weight(self, container_id: str, *, expected_run_id: str) -> int:
        self.inspect_owned(container_id, expected_run_id)
        return self._read_weight(container_id)

    def update_shares(self, container_id: str, shares: int, *, expected_run_id: str) -> ShareUpdate:
        if type(shares) is not int or shares not in (128, 1024):
            raise ValueError("allowed CPU shares are 128 and 1024")
        before = self.inspect_owned(container_id, expected_run_id)
        prior_weight = self._read_weight(container_id) if before["running"] else None
        if before["shares"] == shares:
            return ShareUpdate(before["shares"], shares, prior_weight, "already configured")
        stdout = self._run(["update", "--cpu-shares", str(shares), container_id])
        after = self.inspect_owned(container_id, expected_run_id)
        if after["shares"] != shares:
            raise DockerError(f"configured CPU shares did not change for {container_id}")
        weight = self._read_weight(container_id) if after["running"] else None
        if prior_weight is not None and (weight is None or weight == prior_weight):
            raise DockerError(f"effective cpu.weight did not change for {container_id}")
        return ShareUpdate(before["shares"], shares, weight, stdout)

    def cleanup_run(self, run_id: str) -> list[str]:
        validate_run_id(run_id)
        ids = self._run(["ps", "-aq", "--no-trunc", "--filter", f"label=project={PROJECT}",
                         "--filter", f"label=run_id={run_id}"]).splitlines()
        removed, errors = [], []
        for container_id in ids:
            try:
                before = self.inspect_owned(container_id, run_id)
            except (DockerError, ValueError) as exc:
                errors.append(f"{container_id}: {exc}")
                continue
            if before["shares"] != 1024:
                try:
                    self.update_shares(container_id, 1024, expected_run_id=run_id)
                except DockerError as exc:
                    errors.append(f"{container_id}: restore failed: {exc}")
            try:
                self.inspect_owned(container_id, run_id)
                self._run(["rm", "-f", container_id])
                removed.append(container_id)
            except DockerError as exc:
                errors.append(f"{container_id}: remove failed: {exc}")
        if errors:
            raise CleanupError(removed, errors)
        return removed
