# Adaptive OS Controller MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible Docker-based experiment that measures whether changing a background container's CPU shares improves a periodic foreground workload without hiding the cost to background throughput.

**Architecture:** A Python 3.11 host CLI owns and observes two single-process containers pinned to one Docker-host CPU. Workloads emit JSON Lines; the host aggregates one-second samples into five-second policy windows, applies guarded `docker update --cpu-shares` actions, persists an audit trail in SQLite, and writes Markdown plus machine-readable reports. M0 is a hard feasibility gate; Laya stays optional until the default and deterministic arms work.

**Tech Stack:** Python 3.11 stdlib (`argparse`, `subprocess`, `sqlite3`, `json`, `csv`, `statistics`, `signal`, `threading`), Docker CLI, cgroup v2, optional `laya==0.3.21` for M4, `unittest` for tests.

**Spec:** `Adaptive_OS_Controller_MVP.md`

## Global Constraints

- Run the controller on the host; never mount the Docker socket or use privileged containers.
- Manage only containers labeled `project=adaptive-os-mvp` and `run_id=RUN_ID`, where `RUN_ID` is the exact UUID recorded for that trial.
- Pin foreground and background to the first CPU returned by `os.sched_getaffinity(0)` inside the Docker host unless the user explicitly selects another permitted CPU.
- Use cgroup v2; report cgroup v1 or missing `cpu.weight`/PSI as unsupported or degraded, never as a valid v2 experiment.
- Treat CPU shares as relative soft weights: `balanced=1024`, `favor_interactive=128`; do not combine them with CPU quotas.
- Use `time.monotonic_ns()` for intervals and `time.thread_time_ns()` for workload CPU accounting; wall-clock time is metadata only.
- Sample every 1 second, decide every 5 seconds, enforce a 10-second dwell and 30-second post-change cooldown.
- Initial rule thresholds are miss rate `>2%` for two windows, `<0.5%` for three windows, and background CPU `>=20%`; freeze and record them before comparison runs.
- A model response taking more than 1 second, returning an invalid schema/action, or failing maps to `balanced`.
- Exclude 5 seconds of warm-up and invalid/stale windows from comparisons.
- Run at least five paired trials per policy/profile, 45-60 seconds each, with randomized policy order per block.
- Restore background shares to 1024 on normal completion, failure, stale data, missing workload, SIGINT, or SIGTERM.
- Store raw foreground latency records or lossless histograms sufficient to recompute p95.
- Never claim real video-call quality or AI scheduling superiority; this is a synthetic container feasibility experiment.
- Workload base image is pinned to `python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b`; verify that digest still resolves before the first build and record the resolved platform image ID.

## Review Focus

- A Docker command that returns nonzero or malformed JSON must invalidate the trial, restore shares, and preserve the error event.
- Cleanup must not remove a container missing either the project label or the exact run ID.
- Empty or small latency windows must produce deterministic nearest-rank p95 values rather than `statistics.quantiles` errors.
- A late/out-of-order workload line must not be double-counted or allowed to make stale data look fresh.
- Laya shadow mode must persist proposals but never call `docker update`; live mode must use the same guardrails and fallback as the rule arm.

## File Map

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` | Package metadata, `adaptive-os` entry point, optional Laya extra |
| `Dockerfile.workloads` | Reproducible workload image using the pinned Python base |
| `src/adaptive_os/cli.py` | Argument parsing and orchestration for all five commands |
| `src/adaptive_os/docker_runtime.py` | Docker CLI boundary, ownership checks, share changes, cleanup |
| `src/adaptive_os/workloads.py` | Workload command construction, log readers, profile configuration |
| `src/adaptive_os/telemetry.py` | JSON validation, one-second samples, five-second windows, percentiles |
| `src/adaptive_os/policies.py` | Default/rule/model decisions, hysteresis, cooldown, guardrails |
| `src/adaptive_os/storage.py` | SQLite schema and append-only run/sample/decision/event records |
| `src/adaptive_os/report.py` | Per-trial, paired, and across-trial Markdown/CSV/JSON output |
| `workloads/foreground.py` | Periodic synthetic task and raw latency JSONL |
| `workloads/background.py` | Continuous deterministic hash work and throughput JSONL |
| `tests/test_cli.py` | Command surface and argument validation |
| `tests/test_cleanup.py` | Docker ownership and restoration behavior |
| `tests/test_metric_calculation.py` | Parsing, p95, lateness, staleness, and window validity |
| `tests/test_policy_transitions.py` | Threshold, hysteresis, cooldown, shadow, and fallback behavior |
| `tests/test_storage_report.py` | Schema, exports, pairing, and invalid-run exclusion |
| `runs/` | Ignored local databases, profiles, exports, and reports |

No Compose file, web dashboard, custom cgroup writer, plugin system, or non-model runtime dependency is needed for the MVP.

---

### Task 1: Create the minimal package and CLI shell

**Milestone:** M0 foundation

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/adaptive_os/__init__.py`
- Create: `src/adaptive_os/cli.py`
- Create: `tests/__init__.py`

**Interfaces:**
- Consumes: Python 3.11+
- Produces: `adaptive_os.cli.main(argv: list[str] | None = None) -> int` and the `adaptive-os` console command

- [ ] **Step 1: Initialize version control if this directory is still not a repository**

Run: `git init`

Expected: `.git/` exists and `git status --short` succeeds. Do not reinitialize if a repository has appeared since planning.

- [ ] **Step 2: Add a failing CLI parser test**

Create `tests/test_cli.py` with:

```python
import unittest

from adaptive_os.cli import build_parser


class CliTest(unittest.TestCase):
    def test_expected_commands_exist(self):
        parser = build_parser()
        for command in ("preflight", "calibrate", "run", "report", "cleanup"):
            with self.subTest(command=command):
                namespace = parser.parse_args([command])
                self.assertEqual(namespace.command, command)
```

- [ ] **Step 3: Run the test and confirm the package is missing**

Run: `PYTHONPATH=src python -m unittest tests.test_cli -v`

Expected: FAIL because `adaptive_os.cli` does not exist.

- [ ] **Step 4: Add the package metadata and smallest useful parser**

Use this project configuration:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "adaptive-os-mvp"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
model = ["laya==0.3.21"]

[project.scripts]
adaptive-os = "adaptive_os.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
```

Implement `build_parser()` with the five subcommands and `main()` returning the selected handler's integer exit status. At this stage each handler may call `parser.error("not implemented")`; the parser itself must be real and stable for later tasks.

- [ ] **Step 5: Document the experiment boundary immediately**

The first README section must state that Docker Desktop experiments run inside Docker's Linux VM, affect only project-owned containers, do not schedule native macOS applications, and measure a synthetic foreground workload. Add `.venv/`, `__pycache__/`, `*.pyc`, and `runs/` to `.gitignore`.

- [ ] **Step 6: Run the parser test**

Run:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest tests.test_cli -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add .gitignore pyproject.toml README.md src/adaptive_os tests
git commit -m "chore: scaffold adaptive OS experiment CLI"
```

---

### Task 2: Build the scoped Docker runtime boundary

**Milestone:** M0

**Files:**
- Create: `src/adaptive_os/docker_runtime.py`
- Create: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `subprocess.run`-compatible executor
- Produces: `DockerRuntime.run_owned(...) -> str`, `inspect_shares(container_id) -> int`, `read_cpu_weight(container_id) -> int`, `update_shares(container_id, shares) -> ShareUpdate`, `cleanup_run(run_id) -> list[str]`

- [ ] **Step 1: Write failing ownership and cleanup tests**

Use an injected fake executor and assert the exact command boundary:

```python
class CleanupTest(unittest.TestCase):
    def test_cleanup_filters_on_project_and_exact_run(self):
        fake = FakeExecutor(outputs=["abc\n", "", ""])
        runtime = DockerRuntime(fake)
        self.assertEqual(runtime.cleanup_run("run-123"), ["abc"])
        self.assertEqual(
            fake.commands[0],
            ["docker", "ps", "-aq", "--filter", "label=project=adaptive-os-mvp",
             "--filter", "label=run_id=run-123"],
        )

    def test_update_refuses_unowned_container(self):
        fake = FakeExecutor(outputs=['{"project":"other","run_id":"run-123"}'])
        with self.assertRaises(OwnershipError):
            DockerRuntime(fake).update_shares("abc", 128, expected_run_id="run-123")
```

The fake returns objects with `returncode`, `stdout`, and `stderr`; it must never invoke Docker.

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=src python -m unittest tests.test_cleanup -v`

Expected: FAIL because the runtime types do not exist.

- [ ] **Step 3: Implement one Docker command gateway**

Define:

```python
PROJECT = "adaptive-os-mvp"

@dataclass(frozen=True)
class ShareUpdate:
    original_shares: int
    requested_shares: int
    effective_weight: int
    stdout: str

class DockerError(RuntimeError): ...
class OwnershipError(DockerError): ...

class DockerRuntime:
    def __init__(self, executor=subprocess.run): ...
    def _run(self, args: list[str], *, timeout: float = 15) -> str: ...
```

`_run` must always call an argument array with `check=False`, `capture_output=True`, `text=True`, and a timeout; convert nonzero exits and timeouts into `DockerError` including the command and stderr, but no environment secrets.

Before update/stop/remove, inspect labels with:

```bash
docker inspect --format '{{json .Config.Labels}}' CONTAINER_ID
```

Require both exact labels. `run_owned` must set both labels, `--cpuset-cpus`, `--cpu-shares 1024`, and return the full container ID.

- [ ] **Step 4: Implement restore-first cleanup**

For each doubly filtered ID: re-check labels, attempt `docker update --cpu-shares 1024`, then `docker rm -f`. Continue across failures and return only successfully removed IDs; also return/raise an aggregate error containing failed IDs so callers can record it.

- [ ] **Step 5: Run runtime tests**

Run: `PYTHONPATH=src python -m unittest tests.test_cleanup -v`

Expected: PASS, including command-array assertions and refusal of mismatched labels.

- [ ] **Step 6: Commit**

```bash
git add src/adaptive_os/docker_runtime.py tests/test_cleanup.py
git commit -m "feat: add scoped Docker runtime"
```

---

### Task 3: Implement the M0 preflight and share-update proof

**Milestone:** M0

**Files:**
- Modify: `src/adaptive_os/cli.py`
- Modify: `src/adaptive_os/docker_runtime.py`
- Modify: `tests/test_cleanup.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `DockerRuntime`, pinned Python image
- Produces: `run_preflight(runtime, output_dir) -> PreflightResult`; `adaptive-os preflight --runs-dir runs`

- [ ] **Step 1: Add failing parsers for Docker metadata and CPU affinity**

Test these exact cases:

```python
self.assertEqual(parse_docker_info("linux 2\n"), ("linux", "2"))
self.assertEqual(parse_affinity("[2, 4, 6]\n"), [2, 4, 6])
with self.assertRaises(ValueError):
    parse_affinity("[]")
with self.assertRaises(ValueError):
    parse_docker_info("windows 2")
```

- [ ] **Step 2: Run the failing test**

Run: `PYTHONPATH=src python -m unittest tests.test_cleanup -v`

Expected: FAIL because the parsers and preflight result do not exist.

- [ ] **Step 3: Implement and persist the full preflight sequence**

Run and record:

```text
docker version --format {{json .}}
docker info --format {{.OSType}} {{.CgroupVersion}}
docker run --rm python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b python -c "import os; print(sorted(os.sched_getaffinity(0)))"
docker run --rm python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b sh -c "cat /proc/pressure/cpu; cat /sys/fs/cgroup/cpu.weight"
```

Choose the first affinity CPU. Create a file named like `runs/preflight-20260929T120000Z.json`, using the actual UTC timestamp, with command outputs, Docker/host metadata, selected CPU, image digest, and `supported`, `degraded`, and `reasons` fields. Unsupported includes non-Linux containers, cgroup v1, missing `cpu.weight`, fewer than one permitted CPU, or inability to pin a test container.

- [ ] **Step 4: Add the owned manual-control smoke test**

Launch a labeled container on the selected CPU with `python -c 'while True: pass'`. Capture `HostConfig.CpuShares` and `/sys/fs/cgroup/cpu.weight`, update to 128, verify both configured shares and effective weight changed, restore 1024, verify restoration, then remove. Use `try/finally`; a failed verification makes `supported=false` and cleanup still runs.

- [ ] **Step 5: Wire the command and its exit codes**

`adaptive-os preflight` prints the JSON result path and a one-line verdict. Return `0` only for supported mode, `2` for unsupported/degraded prerequisites, and `1` for an unexpected execution failure.

- [ ] **Step 6: Run the unit suite, then the real M0 gate**

Run:

```bash
PYTHONPATH=src python -m unittest -v
PYTHONPATH=src python -m adaptive_os.cli preflight --runs-dir runs
```

Expected: tests PASS; on a supported Docker Desktop/Engine setup, configured shares and effective weight change and return to baseline. If M0 is unsupported, stop implementation here and preserve the preflight JSON rather than building M1-M4.

- [ ] **Step 7: Commit**

```bash
git add README.md src/adaptive_os/cli.py src/adaptive_os/docker_runtime.py tests/test_cleanup.py
git commit -m "feat: verify Docker CPU share control"
```

---

### Task 4: Add deterministic foreground and background workloads

**Milestone:** M1

**Files:**
- Create: `Dockerfile.workloads`
- Create: `workloads/foreground.py`
- Create: `workloads/background.py`
- Create: `src/adaptive_os/workloads.py`
- Create: `tests/test_metric_calculation.py`

**Interfaces:**
- Consumes: period/work-budget/duration CLI values
- Produces: schema-versioned JSONL events; `WorkloadProfile`; `parse_workload_line(line) -> dict`

- [ ] **Step 1: Write failing schema tests**

Run each workload for about 1.2 seconds as a local subprocess and assert:

```python
self.assertEqual(event["schema_version"], 1)
self.assertIn(event["type"], {"foreground_tick", "foreground_summary"})
self.assertGreaterEqual(event["monotonic_ns"], 0)
self.assertGreaterEqual(event["process_cpu_ns"], 0)
```

For foreground ticks also require `scheduled_ns`, `deadline_ns`, `wakeup_ns`, `completion_ns`, `work_units`, and `deadline_met`; for background summaries require `interval_ns`, `work_units`, and `checksum`.

- [ ] **Step 2: Run tests and confirm failure**

Run: `PYTHONPATH=src python -m unittest tests.test_metric_calculation -v`

Expected: FAIL because workloads are absent.

- [ ] **Step 3: Implement the periodic foreground loop**

Use absolute monotonic deadlines to avoid drift:

```python
scheduled_ns = start_ns + sequence * period_ns
sleep_ns = scheduled_ns - time.monotonic_ns()
if sleep_ns > 0:
    time.sleep(sleep_ns / 1_000_000_000)
wakeup_ns = time.monotonic_ns()
cpu_start = time.thread_time_ns()
while time.thread_time_ns() - cpu_start < work_budget_ns:
    digest = hashlib.sha256(digest).digest()
    work_units += 1
completion_ns = time.monotonic_ns()
deadline_ns = scheduled_ns + period_ns
```

Emit one tick per period and a one-second aggregate. Validate positive duration, period, and work budget at the CLI boundary. Include the final digest hex value.

- [ ] **Step 4: Implement background work and the image**

Background repeatedly hashes a fixed evolving 32-byte buffer and emits one-second summaries using monotonic and thread CPU deltas. The Dockerfile must contain:

```dockerfile
FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
WORKDIR /opt/adaptive-os
COPY workloads/ /opt/adaptive-os/workloads/
USER 65534:65534
```

No package manager install is needed.

- [ ] **Step 5: Add profile and log helpers**

Define:

```python
@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    period_ms: float
    work_budget_ms: float
```

`parse_workload_line` must reject non-objects, unknown `schema_version`, unknown event types, booleans where integers are required, and negative/causally impossible timestamps.

- [ ] **Step 6: Verify locally and in Docker**

Run:

```bash
PYTHONPATH=src python -m unittest tests.test_metric_calculation -v
docker build -f Dockerfile.workloads -t adaptive-os-workloads:dev .
docker run --rm adaptive-os-workloads:dev python workloads/foreground.py --duration-sec 2 --period-ms 20 --work-budget-ms 6
docker run --rm adaptive-os-workloads:dev python workloads/background.py --duration-sec 2
```

Expected: tests PASS; both containers emit valid JSONL and terminate cleanly.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile.workloads workloads src/adaptive_os/workloads.py tests/test_metric_calculation.py
git commit -m "feat: add deterministic synthetic workloads"
```

---

### Task 5: Calculate valid one-second samples and five-second windows

**Milestone:** M1

**Files:**
- Create: `src/adaptive_os/telemetry.py`
- Modify: `tests/test_metric_calculation.py`

**Interfaces:**
- Consumes: parsed foreground/background events and PSI totals
- Produces: `nearest_rank_percentile`, `Sample`, `WindowState`, `WindowAccumulator.add(...)`

- [ ] **Step 1: Add boundary-focused failing tests**

Include:

```python
self.assertEqual(nearest_rank_percentile([0, 1, 2, 100], 0.95), 100)
self.assertEqual(completion_lateness_ms({"completion_ns": 25, "deadline_ns": 20}), 0.000005)
self.assertEqual(completion_lateness_ms({"completion_ns": 19, "deadline_ns": 20}), 0.0)
self.assertEqual(deadline_miss_rate([True, False, False, True]), 0.5)
self.assertFalse(build_window(samples=[], now_ns=10_000_000_000).valid)
```

Also cover a sample older than two sample intervals, duplicate foreground sequence IDs, missing background data, and a p95 of zero with a nonzero miss rate.

- [ ] **Step 2: Run the focused test and observe failure**

Run: `PYTHONPATH=src python -m unittest tests.test_metric_calculation -v`

Expected: FAIL because telemetry functions are missing.

- [ ] **Step 3: Implement explicit metric formulas**

Nearest-rank p95 is `sorted_values[ceil(0.95 * n) - 1]`. Per tick:

```text
wakeup_delay = max(0, wakeup_ns - scheduled_ns)
completion_elapsed = max(0, completion_ns - scheduled_ns)
completion_lateness = max(0, completion_ns - deadline_ns)
deadline_miss = completion_ns > deadline_ns
```

CPU percent is `100 * process_cpu_delta_ns / monotonic_delta_ns`; reject negative deltas and cap only presentation, not stored raw values. Parse the integer following PSI's `some ... total=` field and store the nonnegative microsecond delta; PSI absence makes only that field null.

- [ ] **Step 4: Build immutable sample/window records**

`WindowState` must expose exactly the policy contract:

```python
@dataclass(frozen=True)
class WindowState:
    foreground_p95_completion_lateness_ms: float
    foreground_deadline_miss_rate: float
    foreground_p95_wakeup_delay_ms: float
    background_work_units_per_second: float
    foreground_cpu_percent: float
    background_cpu_percent: float
    cpu_pressure_some_delta: int | None
    current_mode: str
    elapsed_since_last_change_sec: float
    data_age_sec: float
    valid: bool
    invalid_reasons: tuple[str, ...]
```

Make five seconds configurable for fast tests but default/freeze it at five for experiments.

- [ ] **Step 5: Run all metric tests**

Run: `PYTHONPATH=src python -m unittest tests.test_metric_calculation -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/adaptive_os/telemetry.py tests/test_metric_calculation.py
git commit -m "feat: aggregate workload telemetry"
```

---

### Task 6: Persist audit records and establish calibration/default baselines

**Milestone:** M1

**Files:**
- Create: `src/adaptive_os/storage.py`
- Modify: `src/adaptive_os/cli.py`
- Modify: `src/adaptive_os/workloads.py`
- Create: `tests/test_storage_report.py`

**Interfaces:**
- Consumes: run metadata, samples, decisions, events
- Produces: `RunStore`, `TrialConfig`, `TrialResult`, `run_trial(config, runtime, store)`, `adaptive-os calibrate`, default-policy trial records, `runs/profiles.json`

- [ ] **Step 1: Write failing schema/transaction tests**

Create a temporary SQLite database, start a run, append one event/sample/decision, reopen it, and assert all foreign keys point to the run. Add a rollback test where malformed JSON payload input leaves no partial decision.

- [ ] **Step 2: Run and confirm failure**

Run: `PYTHONPATH=src python -m unittest tests.test_storage_report -v`

Expected: FAIL because `RunStore` is absent.

- [ ] **Step 3: Implement the four-table schema**

Use `PRAGMA foreign_keys=ON` and this core shape:

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, policy TEXT NOT NULL, profile TEXT NOT NULL,
  block_index INTEGER NOT NULL, randomization_seed INTEGER NOT NULL,
  started_at_utc TEXT NOT NULL, ended_at_utc TEXT, valid INTEGER NOT NULL DEFAULT 0,
  invalid_reason TEXT, metadata_json TEXT NOT NULL, settings_json TEXT NOT NULL
);
CREATE TABLE samples (
  run_id TEXT NOT NULL REFERENCES runs(run_id), monotonic_offset_ns INTEGER NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, monotonic_offset_ns, kind)
);
CREATE TABLE decisions (
  run_id TEXT NOT NULL REFERENCES runs(run_id), window_index INTEGER NOT NULL,
  policy_version TEXT NOT NULL, state_json TEXT NOT NULL, proposed_action TEXT NOT NULL,
  applied_action TEXT NOT NULL, reason TEXT NOT NULL, confidence REAL,
  inference_duration_ms REAL, original_shares INTEGER NOT NULL,
  new_shares INTEGER NOT NULL, docker_result TEXT NOT NULL,
  PRIMARY KEY (run_id, window_index)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  monotonic_offset_ns INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL
);
```

Serialize JSON with `sort_keys=True` and compact separators for reproducibility.

- [ ] **Step 4: Implement bounded calibration rather than an open-ended tuner**

For each foreground work budget `[4, 6, 8, 10, 12, 14]` ms at a 20 ms period, run 5 seconds warm-up plus 15 seconds measurement:

1. foreground alone;
2. foreground plus balanced background on the same CPU.

Choose `light` as the lowest budget with solo miss rate `<=0.5%` and paired miss rate `<=2%`. Choose `contention` as the lowest budget with solo miss rate `<=0.5%` and paired miss rate `>2%` or paired p95 completion lateness `>1 ms`. If either profile is absent, fail calibration and retain every attempted run. Save the chosen budgets, actual CPU percentages, image ID, CPU, and timestamp to `runs/profiles.json`.

- [ ] **Step 5: Implement a default-policy trial lifecycle**

Define immutable `TrialConfig` and `TrialResult` records and `run_trial(config, runtime, store)`. In this task it accepts only the `default` policy: launch containers, stream both `docker logs --follow` processes, warm up, collect raw ticks/summaries, mark validity, then restore/remove in `finally`. The `default` arm records one `balanced -> balanced` decision per valid window but never invokes `docker update` after startup. Task 8 extends this same function for the rule policy rather than adding a second runner.

- [ ] **Step 6: Run tests and a short calibration smoke**

Run:

```bash
PYTHONPATH=src python -m unittest tests.test_storage_report tests.test_metric_calculation -v
PYTHONPATH=src python -m adaptive_os.cli calibrate --cpu auto --runs-dir runs --measurement-sec 15
```

Expected: tests PASS; both profiles are saved or calibration exits nonzero with retained evidence explaining which criterion was unmet.

- [ ] **Step 7: Commit**

```bash
git add src/adaptive_os/cli.py src/adaptive_os/storage.py src/adaptive_os/workloads.py tests/test_storage_report.py
git commit -m "feat: calibrate and persist baseline trials"
```

---

### Task 7: Implement deterministic policy transitions and shared guardrails

**Milestone:** M2

**Files:**
- Create: `src/adaptive_os/policies.py`
- Create: `tests/test_policy_transitions.py`

**Interfaces:**
- Consumes: `telemetry.WindowState`
- Produces: `Decision`, `DefaultPolicy.decide`, `RulePolicy.decide`, `guard_decision`

- [ ] **Step 1: Write transition-table tests before policy code**

Cover exactly-at and just-over thresholds, invalid windows, two high windows, three low windows, background CPU below 20%, dwell, cooldown, and stale data. Representative assertion:

```python
policy = RulePolicy()
self.assertEqual(policy.decide(state(miss_rate=0.021, background_cpu=50)).applied_action, "balanced")
self.assertEqual(policy.decide(state(miss_rate=0.021, background_cpu=50)).applied_action, "favor_interactive")
self.assertEqual(policy.decide(state(miss_rate=0.8, valid=False)).applied_action, "balanced")
```

Use a fake monotonic timestamp passed into `decide`; never sleep in tests.

- [ ] **Step 2: Run the failing test**

Run: `PYTHONPATH=src python -m unittest tests.test_policy_transitions -v`

Expected: FAIL because policy types are absent.

- [ ] **Step 3: Implement only the two required modes**

```python
SHARES = {"balanced": 1024, "favor_interactive": 128}

@dataclass(frozen=True)
class Decision:
    proposed_action: str
    applied_action: str
    reason: str
    confidence: float | None = None
    inference_duration_ms: float | None = None
```

Do not implement `favor_build` in this MVP. `RulePolicy` stores consecutive-high, consecutive-low, current mode, and last-change time. The initial state requires 10 seconds of valid data before its first change; after any change, the 30-second cooldown prevents another change and therefore also satisfies the minimum dwell. Any invalid/stale state resets counters and proposes/applies `balanced`.

- [ ] **Step 4: Centralize guardrails once**

`guard_decision(proposal, state, now_ns)` is the only path from any policy proposal to shares. It rejects unknown actions, stale/invalid data, expired inference, missing background work, active dwell/cooldown, and terminal state. It returns a `Decision`; callers must not duplicate these checks.

- [ ] **Step 5: Run policy and metric tests**

Run: `PYTHONPATH=src python -m unittest tests.test_policy_transitions tests.test_metric_calculation -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/adaptive_os/policies.py tests/test_policy_transitions.py
git commit -m "feat: add guarded deterministic policy"
```

---

### Task 8: Connect the controller loop, updates, signals, and failback

**Milestone:** M2

**Files:**
- Modify: `src/adaptive_os/cli.py`
- Modify: `src/adaptive_os/docker_runtime.py`
- Modify: `src/adaptive_os/workloads.py`
- Modify: `tests/test_cleanup.py`
- Modify: `tests/test_policy_transitions.py`

**Interfaces:**
- Consumes: existing `run_trial`, `RulePolicy`, `WindowAccumulator`, `RunStore`, `DockerRuntime`
- Produces: rule-policy support in `run_trial`; functional `adaptive-os run --policy rule`; resilient `adaptive-os cleanup`

- [ ] **Step 1: Write a failing restoration-on-error test**

Inject a fake runtime and a log reader that raises after one applied `favor_interactive` decision. Assert call order ends with update to 1024, stop, and remove, and that the store contains `failure` and `restore` events.

- [ ] **Step 2: Run the focused failure tests**

Run: `PYTHONPATH=src python -m unittest tests.test_cleanup tests.test_policy_transitions -v`

Expected: FAIL because `run_trial` is not wired.

- [ ] **Step 3: Implement the one-second/five-second loop**

Use one reader thread per container log stream and a bounded `queue.Queue`. The main thread owns storage and Docker updates. Every second it drains events, writes raw records, samples PSI, creates a sample, and every fifth valid sample calls the selected policy. Record the decision before and after update status without losing either outcome.

- [ ] **Step 4: Make failback non-optional**

On two stale windows, background exit, Docker error, trial end, or stop event:

1. mark the trial invalid if failure occurred before normal end;
2. call guarded restore to 1024;
3. record restore result;
4. terminate log followers;
5. stop/remove only exact-owned containers;
6. close storage.

Install SIGINT/SIGTERM handlers that set a `threading.Event`; do not perform Docker or SQLite calls directly inside the signal handler.

- [ ] **Step 5: Implement leftover cleanup**

`adaptive-os cleanup --run-id RUN_ID` cleans exactly one run using the UUID printed when that run started. Without `--run-id`, enumerate distinct run IDs from doubly project-labeled containers, then call `cleanup_run` separately for each exact ID. Print every removed ID and every failed restore/removal; return nonzero on any failure.

- [ ] **Step 6: Verify units and a short rule run**

Run:

```bash
PYTHONPATH=src python -m unittest -v
PYTHONPATH=src python -m adaptive_os.cli run --policy rule --profile contention --trials 1 --duration-sec 20 --runs-dir runs
```

Expected: unit suite PASS; smoke trial logs decisions and leaves no labeled containers. Because it is shorter than the registered protocol, mark it `smoke=true` and exclude it from reports by default.

- [ ] **Step 7: Commit**

```bash
git add src/adaptive_os tests
git commit -m "feat: run guarded CPU share controller"
```

---

### Task 9: Run randomized comparison blocks and generate M3 reports

**Milestone:** M3

**Files:**
- Create: `src/adaptive_os/report.py`
- Modify: `src/adaptive_os/cli.py`
- Modify: `src/adaptive_os/storage.py`
- Modify: `tests/test_storage_report.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: valid non-smoke run databases grouped by seed/block/profile/policy
- Produces: randomized multi-arm runs; `report.md`, `trials.csv`, `summary.json`

- [ ] **Step 1: Write failing pairing and exclusion tests**

Seed a temporary database with valid default/rule trials, one invalid trial, one warm-up sample, and one smoke run. Assert the report excludes invalid/warm-up/smoke data, keeps per-trial values, pairs by block/profile, and reports the invalid run separately.

- [ ] **Step 2: Run the focused report test**

Run: `PYTHONPATH=src python -m unittest tests.test_storage_report -v`

Expected: FAIL because reporting is absent.

- [ ] **Step 3: Implement randomized block scheduling**

Support repeated `--policy` options. For each block, use `random.Random(seed + block_index).shuffle(policies)` and persist the resulting order before starting the block. Reject comparison mode with fewer than five trials unless `--smoke` is explicit. Restart containers for every arm; never change profile parameters within a block.

- [ ] **Step 4: Compute transparent summary statistics**

For every trial report foreground p95 completion lateness, miss rate, background work/sec, controller CPU time, and policy changes. Across trials report the median plus the sorted values and Tukey Q1/Q3; with five trials this is more honest than a fragile asymptotic confidence interval. Also report paired rule-minus-default deltas and whether the pre-registered target was met:

```text
foreground improvement >= 20% on miss rate or p95 lateness
AND background throughput loss <= 40%
```

When the baseline metric is zero, write `not applicable` rather than divide by zero. Never declare success from a single metric or trial.

- [ ] **Step 5: Write all three outputs from the same summary object**

`summary.json` is the canonical aggregate, `trials.csv` has one row per trial, and `report.md` renders environment, settings, validity/exclusions, per-trial table, distributions, paired deltas, target result, control changes, and limitations. Include `no useful intervention needed` when default meets the registered deadline criteria.

- [ ] **Step 6: Run tests and the registered M3 experiment**

Run:

```bash
PYTHONPATH=src python -m unittest -v
PYTHONPATH=src python -m adaptive_os.cli run --policy default --policy rule --profile light --profile contention --trials 5 --duration-sec 60 --seed 20260929 --runs-dir runs
PYTHONPATH=src python -m adaptive_os.cli report --runs-dir runs
```

Expected: tests PASS; report includes ten trials per profile, randomized order, invalid-run appendix, and explicit foreground/background tradeoff. Preserve a negative result unchanged.

- [ ] **Step 7: Commit**

```bash
git add README.md src/adaptive_os/cli.py src/adaptive_os/report.py src/adaptive_os/storage.py tests/test_storage_report.py
git commit -m "feat: report randomized policy comparisons"
```

---

### Task 10: Add optional Laya shadow decisions

**Milestone:** M4 shadow gate

**Files:**
- Modify: `src/adaptive_os/policies.py`
- Modify: `src/adaptive_os/cli.py`
- Modify: `tests/test_policy_transitions.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: compact rounded `WindowState`, optional agent returned by `laya.load`
- Produces: `LayaAdapter.predict(state) -> ModelProposal`; `laya-shadow` policy

- [ ] **Step 1: Write failing adapter/shadow tests with a fake predictor**

Cover valid choice/answer confidence, missing keys, unknown action, exception, response over 1 second, and shadow behavior. The key assertion is:

```python
decision = ModelPolicy(FakeAdapter("favor_interactive"), shadow=True).decide(state())
self.assertEqual(decision.proposed_action, "favor_interactive")
self.assertEqual(decision.applied_action, "balanced")
```

Add a runner-level fake-runtime case that feeds this decision through `run_trial` and asserts `update_calls == []`.

- [ ] **Step 2: Run the failing tests**

Run: `PYTHONPATH=src python -m unittest tests.test_policy_transitions -v`

Expected: FAIL because model policy support is absent.

- [ ] **Step 3: Implement a callable adapter, not a plugin framework**

Import Laya only inside `LayaAdapter.__init__`; without the optional extra, raise a concise setup error. Before warm-up, load only the English checkpoint with `laya.load("convaiinnovations/laya")`; do not preload the multilingual router or a local HTTP service for numeric English state. Send rounded state JSON and one typed choice question:

```python
questions = {
    "action": {
        "type": "choice",
        "instructions": "Choose the safe CPU-share mode for the next control window.",
        "criteria": {
            "balanced": "keep background at baseline when foreground is healthy or evidence is invalid",
            "favor_interactive": "reduce background weight when valid measurements show foreground deadline harm",
        },
    }
}
```

Require `result["answers"]["action"]["choice"]` to be in `SHARES` and `result["answers"]["action"]["answer_confidence"]` to be a finite number in `[0, 1]`; missing or malformed data is invalid and falls back to balanced. Persist that field as the decision confidence, but never use it to bypass deterministic guardrails. Record model ID `convaiinnovations/laya`, package version, checkpoint revision when exposed, and the complete allowed-action mapping. Measure inference with `monotonic_ns`. Execute prediction through a single-worker `ThreadPoolExecutor`; a one-second timeout falls back to balanced. Do not cancel the whole trial if model inference fails.

- [ ] **Step 4: Enforce shadow semantics in the shared action path**

The model may propose either action, but `shadow=True` always applies the existing mode and skips Docker update. Persist proposed action, applied action, confidence, duration, raw allowed-action mapping, and fallback reason.

- [ ] **Step 5: Verify without and with the optional model**

Run:

```bash
PYTHONPATH=src python -m unittest tests.test_policy_transitions -v
.venv/bin/python -m pip install -e '.[model]'
.venv/bin/adaptive-os run --policy laya-shadow --profile contention --trials 1 --duration-sec 20 --smoke --runs-dir runs
```

Expected: tests PASS; first real model use downloads/loads its checkpoint before warm-up; shadow records proposals and performs no share updates.

- [ ] **Step 6: Run the full shadow comparison gate**

Run five randomized, 60-second `default`, `rule`, and `laya-shadow` trials on both profiles. Proceed to live mode only if every shadow response is auditable, invalid/late responses safely fall back, no shadow update occurs, and report generation succeeds.

- [ ] **Step 7: Commit**

```bash
git add README.md src/adaptive_os/cli.py src/adaptive_os/policies.py tests/test_policy_transitions.py
git commit -m "feat: record Laya shadow decisions"
```

---

### Task 11: Enable guarded Laya live trials and finish reproducibility docs

**Milestone:** M4 live and release check

**Files:**
- Modify: `src/adaptive_os/policies.py`
- Modify: `src/adaptive_os/cli.py`
- Modify: `src/adaptive_os/report.py`
- Modify: `tests/test_policy_transitions.py`
- Modify: `tests/test_storage_report.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: validated M4 shadow results
- Produces: `laya-live` policy using identical guardrails; final reproducible command sequence and report

- [ ] **Step 1: Write a failing equivalence test for live guardrails**

Feed the same invalid, stale, dwell, cooldown, and terminal states through rule and model proposals. Assert both end in `balanced`, use the same share mapping, and persist the same guard reason categories. Add a report test comparing live results only against matching profile/block trials.

- [ ] **Step 2: Run tests to establish failure**

Run: `PYTHONPATH=src python -m unittest tests.test_policy_transitions tests.test_storage_report -v`

Expected: FAIL until the live policy is exposed.

- [ ] **Step 3: Expose live mode behind an explicit flag**

Require both `--policy laya-live` and `--enable-model-updates`; otherwise refuse to start. Pass model proposals through `guard_decision`, then through the same single Docker update function used by the rule policy. No model-specific shares, cadence, thresholds, or cleanup path are allowed.

- [ ] **Step 4: Extend the report without changing success semantics**

Add Laya inference latency/confidence/fallback distributions and rule-versus-Laya paired tradeoffs. State that confidence is model output, not evidence of optimality. Laya is useful only if held-out profiles show a better foreground/background tradeoff than the fixed rule; do not fold shadow proposals into outcome comparisons.

- [ ] **Step 5: Complete README operations and safety guidance**

Document exact setup, preflight, calibration, registered run, report, and cleanup commands; Docker daemon access; thermal/VM confounders; output files; exit codes; label scope; signal behavior; image digest; optional model install/download; and the synthetic/macOS boundary. Include a recovery command using the exact run ID.

- [ ] **Step 6: Run final verification**

Run:

```bash
PYTHONPATH=src python -m unittest -v
adaptive-os preflight --runs-dir runs
adaptive-os run --policy default --policy rule --policy laya-live --enable-model-updates --profile light --profile contention --trials 5 --duration-sec 60 --seed 20260929 --runs-dir runs
adaptive-os report --runs-dir runs
adaptive-os cleanup
docker ps -aq --filter label=project=adaptive-os-mvp
```

Expected: all unit tests PASS; supported preflight; report contains all valid arms plus exclusions; cleanup reports success; final Docker query is empty. If the environment or shadow gate is not valid, skip the live command and report M4 as not run rather than failed superiority.

- [ ] **Step 7: Commit**

```bash
git add README.md src/adaptive_os tests
git commit -m "feat: complete guarded model comparison"
```

---

## Milestone Gates

| Gate | Continue only when | Stop/preserve when |
| --- | --- | --- |
| M0 | Linux containers, cgroup v2, affinity, share update/effective weight, restore, cleanup all pass | Any control capability is missing or restoration fails |
| M1 | Both JSONL streams validate, profiles are reproducible, default trials retain raw metrics | No baseline contention can be produced without invalid solo load |
| M2 | Threshold boundaries, stale data, cooldown, signals, and failure restoration pass | Controller can oscillate, touch unowned containers, or leave shares changed |
| M3 | Five randomized paired trials per arm/profile produce auditable Markdown/CSV/JSON | Report cannot expose exclusions or foreground/background tradeoff |
| M4 shadow | Proposals are valid/auditable and zero Docker updates occur | Model is unavailable, late, malformed, or shadow changes resources |
| M4 live | Explicit opt-in; same guardrails; held-out comparison against rule | Shadow gate is unmet; record live as not run |

## Plan Self-Review

- Spec coverage: M0-M4, workloads, metrics, ownership, persistence, experimental protocol, reports, and exclusions each map to tasks above.
- Scope: one vertical MVP with sequential hard gates; native host scheduling, network control, dashboards, Jev, and `sched_ext` remain separate milestones.
- Type consistency: `WindowState`, `Decision`, `ShareUpdate`, `RunStore`, and `DockerRuntime` have one owning task and stable downstream names.
- Review focus: each listed failure mode is pinned by a named test task.
- Dynamic facts: only the host-selected CPU, platform image ID, calibration result, and run IDs are discovered at runtime; all policies, thresholds, durations, schema, and image manifest digest are fixed above.
