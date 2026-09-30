# Desktop Sandbox Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans, inline as already selected. Steps use checkbox syntax.

**Goal:** A native macOS playground for competing processes in one Linux sandbox.

**Architecture:** A stdlib Linux supervisor communicates over JSONL with one
host worker. The worker owns Docker and SQLite; Qt Widgets consumes bounded
updates and never waits on Docker. Existing CLI experiments remain separate.

**Tech Stack:** Python 3.11, Docker Desktop, SQLite, PySide6, pyside6-deploy.

**Spec:** `docs/superpowers/specs/2026-09-30-desktop-sandbox-design.md`

## Global Constraints

- Exactly one active container per desktop session, no extra probe container.
- Non-root, drop ALL capabilities, no network/host mounts/socket, 1 GiB, pids 64.
- Up to 8 workloads, first allowed CPU shared, 512 MiB memory allocation total.
- Nice 0/10/19 only; effective readback; bounded lease and owned cleanup.
- 1 s telemetry, 5 s warmup/window, 3 s freshness, 300-point live series.
- One DB owner; null unknown metrics; old CLI/data preserved.

## Review Focus

- Lost Docker connection during a control or shutdown must preserve evidence and scope cleanup.
- Database failure must stop the sandbox instead of displaying unrecorded success.
- Delayed/buffered telemetry must not look fresh, including after workload restart.
- Repeated start/stop/add/remove must not grow queues/charts/processes without bound.
- Packaged Finder launch must locate Docker and bundled build sources without shell PATH/venv.

### Task 1: Single-container engine

**Files:** Create `src/adaptive_os/sandbox/{__init__,protocol,agent,presets}.py`,
`src/adaptive_os/sandbox/Dockerfile`, `src/adaptive_os/sandbox_runtime.py`,
`tests/test_sandbox.py`, `scripts/verify_desktop.py`.

**Interfaces:** Produces `validate_command(dict)->dict`,
`validate_config(preset, config)->dict`, `Supervisor.handle(command)`,
`SandboxRuntime.prepare(progress)`, `start(session_id)->Popen`,
`cleanup_session(session_id)`, `leftovers()->list[dict]`.
JSONL v2 commands carry request_id and op; start also carries workload_id,
preset, config; stop/priority address only workload_id. Replies include
sequence, mono_ns, kind; ack includes request_id/ok/error/effective values.

- [ ] Write table-driven validation and /proc identity tests; test that foreign
  labels refuse cleanup and create flags contain resource/isolation limits.
  Example: `self.assertRaises(ValueError, validate_config, 'memory', {'mib':129})`.
- [ ] Run `.venv/bin/python -m unittest tests.test_sandbox -v`.
  Expected: FAIL because new engine is unavailable.
- [ ] Implement strict protocol, selectors-based supervisor, scoped children,
  affinity and priority probe, bounded queues, heartbeat expiry, Docker adapter.
  Core ownership rule: `labels == expected` for every expected label before rm.
- [ ] Run full `.venv/bin/python -m unittest discover -v`.
  Expected: all tests pass, including legacy tests.
- [ ] Build image and run `scripts/verify_desktop.py --seconds 15` (expanded in Task 2).
  Expected: one sandbox, same CPU, priority readback and scoped stop verified.
- [ ] Commit `feat: add single-container sandbox engine`.

### Task 2: Telemetry and recordings

**Files:** Extend sandbox presets/agent, create `src/adaptive_os/session_store.py`,
`tests/test_session_store.py`; extend `scripts/verify_desktop.py`.

**Interfaces:** Consumes Task 1 sample/raw/event envelopes. Produces
`SessionStore(path)`, `begin(id, metadata)`, `record(id, message, receipt_ns)`,
`finish(id,status)`, `sessions()`, `load(id)->dict`, `export(id,path,format)`.
Sample values: cpu_pct, rss_mib, app_rate, app_unit, read_bytes, write_bytes,
nice, warmup, miss_rate, p95_lateness_ms, p95_wakeup_ms; unsupported = None.

- [ ] Write persistence, interrupted-recovery and JSON/CSV parity tests plus
  counter delta/PID-reuse/missing /proc tests. Example: two CPU seconds over
  two wall seconds yields 100%, changed start identity yields None.
- [ ] Run `.venv/bin/python -m unittest tests.test_session_store -v`.
  Expected: FAIL on missing store behavior.
- [ ] Add bounded memory/file presets, 5-second latency windows/raw ticks,
  per-process counters, batched SQLite schema v2, streaming exports and paging.
  Transactions commit on `flush()` and finish, not per raw tick.
- [ ] Run full unittest suite. Expected: pass.
- [ ] Run 8-workload Docker verification and compare persisted rows to export.
  Expected: <=512 MiB configured buffers, bounded file sizes, no orphan resources.
- [ ] Commit `feat: record multi-workload telemetry and session exports`.

### Task 3: Native desktop UI

**Files:** Create `src/adaptive_os/{desktop,desktop_worker,charts}.py`,
`tests/test_desktop.py`; modify `cli.py`, `pyproject.toml`, `README.md`.

**Interfaces:** Worker owns SessionStore/SandboxRuntime on a thread, accepts
bounded commands, emits bounded UI updates. UI pump drains without blocking.
Produces `desktop.main(argv=None)->int`, CLI `adaptive-os desktop`.
Charts accept workload keyed series with timestamp/value or None (gap).

- [ ] Write headless Qt tests for starter rows, workload validation, pending
  priority acknowledgements, stale gaps and bounded series; fake only Docker I/O.
  Example: after 301 points, assert chart series contains 300 points.
- [ ] Run `.venv/bin/python -m unittest tests.test_desktop -v`.
  Expected: FAIL because desktop module is unavailable.
- [ ] Implement native controls, off-thread work, charts, status/errors, timeline,
  offline history, JSON/CSV dialogs and exact-session recovery confirmation.
  Disable priority when capabilities say monitoring-only; don't optimistically
  mutate applied state before ack. Confirm close before requesting shutdown.
- [ ] Run full unittest suite with Qt offscreen. Expected: pass.
- [ ] Launch app and exercise start/add/change priority/stop/history/export with
  real Docker. Expected: responsive window and persisted control events.
- [ ] Commit `feat: add native desktop sandbox playground`.

### Task 4: Packaging and resilience

**Files:** Create `desktop_app.py`, `pysidedeploy.spec`, `scripts/build_desktop.py`,
extend integration verification and README, update implementation status.

**Interfaces:** `python scripts/build_desktop.py` invokes pyside6-deploy and
bundles allowlisted Docker source files. Output `dist/Adaptive OS Controller.app`.

- [ ] Add regression tests for Docker resolution without PATH, source-resource
  lookup, failed DB writes and invalid/stale acknowledgements.
- [ ] Run these tests before implementation. Expected: failures expose gaps.
- [ ] Implement packaging/resources and smallest resilience fixes. Build via
  `.venv/bin/python scripts/build_desktop.py`. Expected: macOS .app produced.
- [ ] Run full tests, 10-minute 8-workload soak, disconnect heartbeat lease test,
  forced-app-kill recovery test, and normal owned cleanup. Expected: preserved
  evidence, bounded memory, exited workload processes, no unrelated deletion.
- [ ] Launch bundled app with sanitized PATH and no activated venv; verify UI,
  Docker readiness, source preparation and history. Expected: works from Finder.
- [ ] Commit `build: package and verify macOS desktop app`.

## Whole-plan acceptance

Run the fresh-context review against this feature's base `c356510`, fix Important
findings with regression tests, rerun suite, retain artifacts/evidence paths in
implementation status. Do not claim unrun platform or long-duration checks.
