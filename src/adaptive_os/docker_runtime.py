"""Docker CLI boundary: mutations require both ownership labels and a full ID."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import time
import uuid

PROJECT = "adaptive-os-mvp"
BASE_IMAGE = "python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b"
INSPECT_FORMAT = ('{"labels":{{json .Config.Labels}},"shares":{{json .HostConfig.CpuShares}},'
                  '"running":{{json .State.Running}},"cpuset":{{json .HostConfig.CpusetCpus}},'
                  '"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}}}')


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

    def wait_owned(self, container_id: str, *, expected_run_id: str) -> dict:
        self.inspect_owned(container_id, expected_run_id)
        exit_text = self._run(["wait", container_id], timeout=5)
        state = self.inspect_owned(container_id, expected_run_id)
        if (state["running"] or type(state.get("exit_code")) is not int
                or type(state.get("oom_killed")) is not bool or exit_text != str(state["exit_code"])):
            raise DockerError(f"unverifiable final workload state for {container_id}")
        return state

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


class UnsupportedEnvironment(ValueError):
    """A prerequisite for the cgroup v2 experiment is unavailable."""


@dataclass(frozen=True)
class PreflightResult:
    path: Path
    exit_code: int
    supported: bool
    reasons: tuple[str, ...]


def parse_docker_info(raw: str) -> tuple[str, str]:
    values = raw.split()
    if len(values) != 2 or values[0] != "linux" or values[1] not in ("1", "2"):
        raise ValueError("Docker must report Linux containers and a recognized cgroup version")
    return values[0], values[1]


def parse_affinity(raw: str) -> list[int]:
    values = json.loads(raw)
    if (not isinstance(values, list) or not values
            or any(type(cpu) is not int or cpu < 0 for cpu in values)
            or values != sorted(set(values))):
        raise ValueError("affinity must be a sorted, nonempty list of distinct CPU IDs")
    return values


def run_preflight(runtime: DockerRuntime, output_dir: Path, *, run_id: str | None = None,
                  stop_event: threading.Event | None = None) -> PreflightResult:
    run_id = validate_run_id(run_id or str(uuid.uuid4()))
    stop_event = stop_event or threading.Event()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    path = output_dir / f"preflight-{started:%Y%m%dT%H%M%S%fZ}-{run_id}.json"
    data = {"schema_version": 1, "run_id": run_id, "started_at_utc": started.isoformat(),
            "supported": False, "degraded": False, "reasons": [], "status": "running",
            "image": BASE_IMAGE, "selected_cpu": None,
            "host": {"os": platform.system(), "release": platform.release(),
                     "architecture": platform.machine(), "python": platform.python_version()},
            "environment_notes": "Power saving, temperature and other host load are not controlled; record them before trials."}
    path.write_text(json.dumps(data, indent=2) + "\n")
    exit_code = 1
    containers_attempted = False

    def check_stop():
        if stop_event.is_set():
            raise InterruptedError("preflight interrupted; restoring and cleaning up owned containers")

    try:
        check_stop()
        data["docker_version"] = json.loads(runtime._run(["version", "--format", "{{json .}}"] ))
        if not isinstance(data["docker_version"], dict) or not data["docker_version"].get("Server"):
            raise UnsupportedEnvironment("Docker daemon is unavailable")
        info = runtime._run(["info", "--format", "{{.OSType}} {{.CgroupVersion}}"])
        try:
            os_type, cgroup_version = parse_docker_info(info)
        except ValueError as exc:
            raise UnsupportedEnvironment(str(exc)) from exc
        data.update(os_type=os_type, cgroup_version=cgroup_version)
        if cgroup_version != "2":
            raise UnsupportedEnvironment("cgroup v2 is required; cgroup v1 detected")
        # Docker gives DOCKER_CONTEXT precedence over DOCKER_HOST.
        context = os.environ.get("DOCKER_CONTEXT")
        endpoint = None if context else os.environ.get("DOCKER_HOST")
        if not endpoint:
            args = ["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"]
            endpoint = json.loads(runtime._run(args + ([context] if context else [])))
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise UnsupportedEnvironment("preflight requires a local Docker Unix socket; remote contexts are outside scope")
        data["docker_host"] = json.loads(runtime._run(["info", "--format",
            '{"kernel":{{json .KernelVersion}},"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}},'
            '"operating_system":{{json .OperatingSystem}},"architecture":{{json .Architecture}}}']))
        check_stop()
        containers_attempted = True
        probe = runtime.run_owned(run_id, None, ["python", "-c", "import time; time.sleep(600)"])
        data["probe_container"] = probe
        data["resolved_image"] = json.loads(runtime._run(["image", "inspect", "--format",
            '{"id":{{json .Id}},"architecture":{{json .Architecture}},"os":{{json .Os}}}', BASE_IMAGE]))
        try:
            data["allowed_cpus"] = parse_affinity(runtime.exec_owned(probe,
                ["python", "-c", "import os; print(sorted(os.sched_getaffinity(0)))"], expected_run_id=run_id))
        except ValueError as exc:
            raise UnsupportedEnvironment(str(exc)) from exc
        cpu = data["selected_cpu"] = data["allowed_cpus"][0]
        try:
            data["cpu_pressure"] = runtime.exec_owned(probe, ["cat", "/proc/pressure/cpu"], expected_run_id=run_id)
            if not re.search(r"^some .*\btotal=\d+\s*$", data["cpu_pressure"], re.MULTILINE):
                raise DockerError("CPU PSI 'some total' is unavailable or malformed")
            data["probe_weight"] = runtime.read_cpu_weight(probe, expected_run_id=run_id)
        except DockerError as exc:
            data["degraded"] = True
            raise UnsupportedEnvironment(f"cgroup v2 telemetry unavailable: {exc}") from exc
        check_stop()
        smoke = runtime.run_owned(run_id, cpu, ["python", "-c", "while True: pass"])
        data["smoke_container"] = smoke
        before = runtime.inspect_owned(smoke, run_id)
        pinned = parse_affinity(runtime.exec_owned(smoke,
            ["python", "-c", "import os; print(sorted(os.sched_getaffinity(0)))"], expected_run_id=run_id))
        if pinned != [cpu] or before["cpuset"] != str(cpu):
            raise UnsupportedEnvironment("test container could not be pinned to the selected CPU")
        if before["shares"] != 1024:
            raise DockerError("owned container did not start with baseline shares")
        initial_weight = runtime.read_cpu_weight(smoke, expected_run_id=run_id)
        data["smoke"] = {"initial_shares": before["shares"], "initial_weight": initial_weight}
        check_stop()
        changed = runtime.update_shares(smoke, 128, expected_run_id=run_id)
        data["smoke"]["change"] = asdict(changed)
        if changed.effective_weight is None or changed.effective_weight >= initial_weight:
            raise DockerError("lower shares did not lower effective cpu.weight")
        check_stop()
        restored = runtime.update_shares(smoke, before["shares"], expected_run_id=run_id)
        data["smoke"].update(restore=asdict(restored), restored_weight=restored.effective_weight)
        if restored.effective_weight != initial_weight:
            raise DockerError("effective cpu.weight was not restored to its initial value")
        exit_code = 0
    except UnsupportedEnvironment as exc:
        data["reasons"].append(str(exc))
        exit_code = 2
    except (DockerError, ValueError, OSError) as exc:
        data["reasons"].append(str(exc))
        exit_code = 1
    finally:
        if containers_attempted:
            try:
                data["removed_containers"] = runtime.cleanup_run(run_id)
                remaining = runtime._run(["ps", "-aq", "--no-trunc", "--filter", f"label=project={PROJECT}",
                                          "--filter", f"label=run_id={run_id}"])
                if remaining:
                    raise DockerError(f"owned containers remain after cleanup: {remaining}")
            except DockerError as exc:
                if isinstance(exc, CleanupError):
                    data["removed_containers"] = exc.removed
                data["reasons"].append(f"cleanup failed: {exc}")
                exit_code = 1
        data.update(supported=exit_code == 0, status="complete", exit_code=exit_code,
                    commands=runtime.commands, finished_at_utc=datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(data, indent=2) + "\n")
    return PreflightResult(path, exit_code, data["supported"], tuple(data["reasons"]))
