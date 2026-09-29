"""Workload profile and JSONL contract shared by telemetry and orchestration."""

from dataclasses import asdict, dataclass, field
import json
import math
import re
import queue
import subprocess
import threading
import time
import uuid

from adaptive_os.docker_runtime import DockerError, validate_run_id
from adaptive_os.telemetry import (WindowAccumulator, completion_lateness_ms, deadline_miss_rate,
                                   nearest_rank_percentile, parse_psi_total)

WORKLOAD_IMAGE = "adaptive-os-workloads:dev"


@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    period_ms: float
    work_budget_ms: float

    def __post_init__(self):
        if not self.name or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                                for value in (self.period_ms, self.work_budget_ms)):
            raise ValueError("profile requires a name and finite positive period/budget")
        if self.work_budget_ms >= self.period_ms:
            raise ValueError("work budget must be below the period for a valid solo baseline")


def parse_workload_line(line: str) -> dict:
    event = json.loads(line)
    if not isinstance(event, dict) or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise ValueError("expected a schema_version=1 workload object")
    kind = event.get("type")
    if kind not in ("foreground_tick", "foreground_summary", "background_summary"):
        raise ValueError("unknown workload event type")
    fields = ["sequence", "start_ns", "monotonic_ns", "process_cpu_ns", "work_units"]
    fields += (["scheduled_ns", "wakeup_ns", "completion_ns", "deadline_ns"] if kind == "foreground_tick"
               else ["interval_start_ns", "interval_ns"])
    for field in fields:
        if type(event.get(field)) is not int or event[field] < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
    if not isinstance(event.get("checksum"), str) or not re.fullmatch("[0-9a-f]{64}", event["checksum"]):
        raise ValueError("missing or invalid checksum")
    if event["monotonic_ns"] < event["start_ns"]:
        raise ValueError("event precedes workload start")
    if kind == "foreground_tick":
        if not (event["start_ns"] <= event["scheduled_ns"] <= event["wakeup_ns"] <= event["completion_ns"]
                == event["monotonic_ns"] and event["deadline_ns"] > event["scheduled_ns"]):
            raise ValueError("causally impossible foreground timestamps")
        if (type(event.get("deadline_met")) is not bool or type(event.get("skipped")) is not bool
                or event["deadline_met"] != (event["completion_ns"] <= event["deadline_ns"])):
            raise ValueError("deadline flag does not match timestamps")
    elif (event["interval_ns"] <= 0 or event["interval_start_ns"] < event["start_ns"]
          or event["monotonic_ns"] - event["interval_start_ns"] != event["interval_ns"]):
        raise ValueError("invalid summary interval")
    return event


@dataclass(frozen=True)
class TrialConfig:
    profile: WorkloadProfile
    cpu: int
    policy: str = "default"
    measurement_sec: int = 60
    warmup_sec: int = 5
    background: bool = True
    background_cpu: int | None = None
    image: str = WORKLOAD_IMAGE
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    seed: int = 20260929
    block_index: int = 0
    smoke: bool = False

    def __post_init__(self):
        validate_run_id(self.run_id)
        if type(self.cpu) is not int or self.cpu < 0:
            raise ValueError("CPU must be a nonnegative integer")
        if type(self.measurement_sec) is not int or self.measurement_sec < 5 or self.measurement_sec % 5:
            raise ValueError("measurement duration must be a positive multiple of five seconds")
        if type(self.warmup_sec) is not int or self.warmup_sec < 0 or self.warmup_sec % 5:
            raise ValueError("warm-up must be a nonnegative multiple of five seconds")
        if 1_000_000_000 % round(self.profile.period_ms * 1_000_000):
            raise ValueError("period must divide one second exactly")


@dataclass(frozen=True)
class TrialResult:
    run_id: str
    valid: bool
    reason: str | None
    metrics: dict


def select_profiles(rows):
    chosen = {}
    for row in sorted(rows, key=lambda row: row["budget_ms"]):
        solo, paired = row["solo"], row["paired"]
        if not solo["valid"] or not paired["valid"] or solo["miss_rate"] > 0.005:
            continue
        name = "light" if paired["miss_rate"] <= 0.02 and paired["p95_lateness_ms"] <= 1 else "contention"
        chosen.setdefault(name, asdict(WorkloadProfile(name, 20, row["budget_ms"])))
    missing = {"light", "contention"} - chosen.keys()
    if missing:
        raise ValueError(f"calibration found no valid {', '.join(sorted(missing))} profile; inspect retained trials")
    return chosen


def run_trial(config: TrialConfig, runtime, store, *, metadata=None, stop_event=None) -> TrialResult:
    if config.policy != "default":
        raise ValueError("only the default policy is available before the M1 gate")
    stop_event = stop_event or threading.Event()
    metadata = dict(metadata or {})
    store.start_run(config.run_id, config.policy, config.profile.name, config.block_index,
                    config.seed, metadata, asdict(config))
    host_origin = time.monotonic_ns()
    cpu_origin = time.process_time_ns()
    command_origin = len(runtime.commands)
    total_sec = config.warmup_sec + config.measurement_sec
    accumulator = WindowAccumulator(round(config.profile.period_ms * 1_000_000), background_required=config.background)
    messages = queue.Queue(maxsize=10000)
    readers, followers, windows, errors = [], [], [], []
    reader_stop = threading.Event()
    overflow = threading.Event()
    vm_start = 0

    def event(kind, payload):
        store.event(config.run_id, time.monotonic_ns() - host_origin, kind, payload)

    def read_logs(process, kind):
        try:
            for line in process.stdout:
                if reader_stop.is_set():
                    break
                try:
                    messages.put((kind, time.monotonic_ns(), line), timeout=0.5)
                except queue.Full:
                    overflow.set()
                    break
        finally:
            process.stdout.close()

    def drain():
        while True:
            try:
                kind, received, line = messages.get_nowait()
            except queue.Empty:
                break
            record = parse_workload_line(line)
            if not record["type"].startswith(kind) or record["start_ns"] != vm_start:
                raise ValueError("workload stream identity/start time mismatch")
            if not accumulator.add(record, received_ns=received):
                raise ValueError("duplicate or out-of-order workload record")
            store.sample(config.run_id, record["monotonic_ns"] - vm_start, record["type"], record)

    try:
        if stop_event.is_set():
            raise InterruptedError("trial interrupted")
        benchmark_cpus = {config.cpu, config.background_cpu}
        probe_cpu = next((cpu for cpu in metadata.get("allowed_cpus", []) if cpu not in benchmark_cpus), None)
        metadata["instrumentation_cpu"] = probe_cpu
        metadata["instrumentation_isolated"] = probe_cpu is not None
        probe = runtime.run_owned(config.run_id, probe_cpu, ["python", "-c", "import time; time.sleep(600)"], image=config.image)
        before_clock = time.monotonic_ns()
        vm_now = int(runtime.exec_owned(probe, ["python", "-c", "import time; print(time.monotonic_ns())"],
                                       expected_run_id=config.run_id))
        after_clock = time.monotonic_ns()
        vm_start = vm_now + 3_000_000_000
        host_start = (before_clock + after_clock) // 2 + 3_000_000_000
        metadata.update(vm_start_ns=vm_start, host_start_ns=host_start, host_origin_ns=host_origin,
                        event_offset_clock="host_since_runner_start", sample_offset_clock="vm_since_workload_start",
                        clock_uncertainty_ns=(after_clock - before_clock) // 2)
        containers = {}
        for kind in (["foreground", "background"] if config.background else ["foreground"]):
            command = ["python", f"workloads/{kind}.py", "--duration-sec", str(total_sec), "--start-ns", str(vm_start)]
            if kind == "foreground":
                command += ["--period-ms", str(config.profile.period_ms), "--work-budget-ms", str(config.profile.work_budget_ms)]
            cpu = config.background_cpu if kind == "background" and config.background_cpu is not None else config.cpu
            cid = runtime.run_owned(config.run_id, cpu, command, image=config.image)
            inspected = runtime.inspect_owned(cid, config.run_id)
            if inspected["cpuset"] != str(cpu) or inspected["shares"] != 1024 or not inspected["running"]:
                raise DockerError(f"{kind} did not start with the requested CPU/shares")
            containers[kind] = cid
            event("workload_start", {"kind": kind, "container_id": cid, "cpu": cpu, "shares": 1024})
            process = subprocess.Popen(["docker", "logs", "--follow", cid], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            followers.append(process)
            reader = threading.Thread(target=read_logs, args=(process, kind), daemon=True)
            reader.start()
            readers.append(reader)
        previous_psi = parse_psi_total(runtime.exec_owned(probe, ["cat", "/proc/pressure/cpu"], expected_run_id=config.run_id))
        window_psi, next_second = 0, 1
        while next_second <= total_sec:
            if stop_event.is_set():
                raise InterruptedError("trial interrupted")
            if overflow.is_set():
                raise ValueError("telemetry queue overflow")
            drain()
            now = time.monotonic_ns()
            # Half a second permits summary emission and Docker log transport.
            due = host_start + next_second * 1_000_000_000 + 500_000_000
            if now < due:
                stop_event.wait(min(0.05, (due - now) / 1_000_000_000))
                continue
            for process in followers:
                if process.poll() is not None and (process.returncode != 0 or next_second < total_sec):
                    raise DockerError("workload log stream exited before the trial ended")
            psi = parse_psi_total(runtime.exec_owned(probe, ["cat", "/proc/pressure/cpu"], expected_run_id=config.run_id))
            psi_delta, previous_psi = psi - previous_psi, psi
            if psi_delta < 0:
                raise ValueError("PSI counter moved backwards")
            window_psi += psi_delta
            drain()
            end = vm_start + next_second * 1_000_000_000
            state = accumulator.build_window(end - 1_000_000_000, end, now_ns=time.monotonic_ns(),
                                             elapsed_since_last_change_sec=next_second, cpu_pressure_some_delta=psi_delta,
                                             window_end_host_ns=host_start + next_second * 1_000_000_000)
            store.sample(config.run_id, next_second * 1_000_000_000, "sample",
                         {"warmup": next_second <= config.warmup_sec, "state": asdict(state)})
            if next_second == config.warmup_sec:
                event("warmup_end", {"seconds": config.warmup_sec})
            if next_second > config.warmup_sec and (next_second - config.warmup_sec) % 5 == 0:
                state = accumulator.build_window(end - 5_000_000_000, end, now_ns=time.monotonic_ns(),
                                                 elapsed_since_last_change_sec=next_second, cpu_pressure_some_delta=window_psi,
                                                 window_end_host_ns=host_start + next_second * 1_000_000_000)
                windows.append((end - 5_000_000_000, end, state))
                store.sample(config.run_id, next_second * 1_000_000_000, "window", asdict(state))
                store.decision(config.run_id, len(windows) - 1, asdict(state), proposed_action="balanced",
                               applied_action="balanced", reason="default baseline; no share updates")
            if next_second % 5 == 0:
                window_psi = 0
            next_second += 1
        drain()
        for kind, cid in containers.items():
            final = runtime.wait_owned(cid, expected_run_id=config.run_id)
            event("workload_stop", {"kind": kind, "container_id": cid, **final})
            if final["exit_code"] != 0 or final["oom_killed"] or final["running"]:
                raise DockerError(f"{kind} terminated abnormally: exit={final['exit_code']}, OOM={final['oom_killed']}")
    except (DockerError, ValueError, OSError) as exc:
        errors.append(str(exc))
        event("failure", {"reason": str(exc)})
    finally:
        try:
            reader_stop.set()
            for process in followers:
                try:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"log follower shutdown failed: {exc}")
                    event("failure", {"reason": errors[-1]})
            for reader in readers:
                reader.join(timeout=2)
                if reader.is_alive():
                    errors.append("log reader did not stop")
        finally:
            try:
                removed = runtime.cleanup_run(config.run_id)
                event("cleanup", {"removed": removed, "baseline_shares": 1024})
            except DockerError as exc:
                errors.append(f"cleanup failed: {exc}")
                event("cleanup_failure", {"reason": str(exc)})
    valid_windows = [(start, end, state) for start, end, state in windows if state.valid]
    if len(valid_windows) != config.measurement_sec // 5:
        errors.append("incomplete or invalid measurement windows")
    raw_ticks = [tick for start, end, _ in valid_windows for tick in accumulator.ticks(start, end)]
    metrics = {}
    if raw_ticks:
        metrics = {"p95_lateness_ms": nearest_rank_percentile([completion_lateness_ms(tick) for tick in raw_ticks]),
                   "miss_rate": deadline_miss_rate([tick["deadline_met"] for tick in raw_ticks]),
                   "p95_wakeup_ms": nearest_rank_percentile([(tick["wakeup_ns"] - tick["scheduled_ns"]) / 1_000_000 for tick in raw_ticks]),
                   "background_work_per_sec": sum(state.background_work_units_per_second for _, _, state in valid_windows) / len(valid_windows),
                   "foreground_cpu_percent": sum(state.foreground_cpu_percent for _, _, state in valid_windows) / len(valid_windows),
                   "background_cpu_percent": sum(state.background_cpu_percent for _, _, state in valid_windows) / len(valid_windows),
                   "ticks": len(raw_ticks), "valid_windows": len(valid_windows), "policy_changes": 0}
    metrics["controller_cpu_sec"] = (time.process_time_ns() - cpu_origin) / 1_000_000_000
    metadata.update(metrics=metrics, docker_commands=runtime.commands[command_origin:],
                    invalid_windows=[asdict(state) for _, _, state in windows if not state.valid])
    reason = "; ".join(errors) or None
    store.finish_run(config.run_id, not errors, reason, metadata)
    return TrialResult(config.run_id, not errors, reason, metrics)
