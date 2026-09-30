# Adaptive OS Controller

## Native desktop app (macOS)

One Debian Linux Docker sandbox contains **all** competing workloads and its
telemetry collector. This is a native Qt app, not a website. Docker Desktop must
be installed and running; no browser, ports, or Docker socket inside the sandbox.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop]'
.venv/bin/adaptive-os desktop
```

Click **Start sandbox** (the pinned image builds on first use), then **Start all**.
The starter pair is a periodic task and continuous CPU hashing. Add memory-touch
and file-I/O presets up to eight workloads. All compete on the same Linux CPU;
the collector uses another allowed CPU where available. Select a row to change
its live CPU priority, or stop it to edit its settings. Stop the sandbox to save
the recording and remove its temporary container and scratch volume.

CPU 100% means one saturated logical CPU. RSS is resident MiB; memory throughput
counts bytes actually touched (one byte per page), not the whole allocated
buffer. File I/O uses a bounded 64 MiB file with periodic fsync; application
throughput includes reads served from cache. Kernel read/write counters are
shown separately. Periodic tasks have a 20 ms period; their first five seconds
are warm-up. Missing/stale metrics show gaps, never invented zeros. This is an
observational playground, not a calibrated policy comparison.

Priority options are nice 0/10/19 (Normal/Reduced/Minimal), checked with a real
0→10→19→0 probe **inside the same container**. Unsupported systems remain in
monitoring-only mode. The sandbox runs non-root, with no network, a read-only
root, dropped capabilities, a 1 GiB memory limit and 64-process limit. A ten-second
heartbeat lease stops workloads if the app dies. On reopening, **Recover…**
offers removal of only the exact labeled leftover session; recordings are kept.

The **Recordings** tab opens saved charts/events without Docker and exports full
JSON or CSV records. Stop the live sandbox before browsing/exporting. The live
plots retain 300 points; history shows up to 300 observed points per workload;
exports retain every recorded sample and raw tick. Desktop data is separate from
legacy `runs/`, in Qt's macOS Application Support folder for Adaptive OS Controller
(the exact path is shown in the status bar). Override it with `desktop --data-dir PATH`.

Build a locally runnable app (Xcode Command Line Tools required):

```sh
.venv/bin/python scripts/build_desktop.py
open 'dist/Adaptive OS Controller.app'
```

The bundle includes Python, Qt and the sandbox sources; no activated venv is
needed to launch it. It still needs Docker Desktop. The build installs Nuitka
4.1.1 and uses pyside6-deploy. Ad-hoc local signing is included; public distribution
signing/notarization is not. Previous local bundles are retained in `dist/previous-*.app`.
PySide6/Qt and Nuitka retain their respective third-party licenses.

Verification (Docker checks create and clean only their own session resources):

```sh
.venv/bin/python -m unittest discover -q
.venv/bin/python scripts/verify_desktop.py --seconds 45 --workloads 8 --lease
.venv/bin/python scripts/verify_desktop_ui.py --seconds 600
```

If Docker is missing or stopped, launch it and click Start sandbox again. Build
errors appear in the timeline. A disk/recording error stops the sandbox rather
than silently losing measurements. Keep `sessions-v2.sqlite3` plus its WAL files
together for recovery; do not edit an active database. Custom shell workloads,
automatic scheduling policies and native macOS process control are not included.

## Legacy CLI benchmark workflow

The commands below are the original multi-container experiment. The desktop app
does not call them, does not require calibration, and does not consume their data.

This experiment measures a **synthetic foreground workload** in Linux containers.
On Docker Desktop for macOS, the containers run inside Docker's Linux VM.
The controller affects only project-owned containers; it cannot schedule native
macOS applications or measure real video-call quality. CPU shares are relative
weights under contention, not a CPU cap or reservation.

Requires Python 3.11+ and access to a local Docker Engine/Desktop Linux daemon.
The host controller uses the Docker CLI. Containers never receive the Docker
socket or privileged access.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest -v
.venv/bin/adaptive-os preflight --runs-dir runs
```

Preflight records Docker/VM metadata, CPU affinity, pinned image identity, CPU
pressure, every command result, and the `1024 → 128 → 1024` share/weight proof in
`runs/preflight-*.json`. It exits `0` only when verification and cleanup succeed,
`2` for unsupported/degraded prerequisites, or `1` for execution/cleanup failure.
Only a local Docker Unix socket is supported. Docker daemon access is effectively
administrative access; review which Docker context is selected before running.

All probe containers carry `project=adaptive-os-mvp` and their exact run UUID.
SIGINT/SIGTERM request restoration and cleanup after the current bounded command.
If the controller is forcibly killed, use the printed run ID for recovery:

```sh
.venv/bin/adaptive-os cleanup                    # list only
.venv/bin/adaptive-os cleanup --run-id RUN_UUID  # restore/remove only this run
```

The pinned base is
`python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b`.
Docker may download it on the first preflight. Containers run as an unprivileged
user with networking disabled and a read-only root filesystem. Record host
load, power/thermal conditions and VM settings before comparison experiments.

M0 (environment and CPU-weight control), synthetic workloads, raw telemetry,
SQLite persistence and default-policy calibration are implemented. The rule,
comparison report and optional model follow only after calibration passes.

```sh
docker build -f Dockerfile.workloads -t adaptive-os-workloads:dev .
.venv/bin/adaptive-os calibrate --cpu auto --measurement-sec 15 --runs-dir runs
# For hardware needing lighter loads, keep the same acceptance thresholds:
.venv/bin/adaptive-os calibrate --budgets-ms 1 2 4 8 --runs-dir runs
.venv/bin/adaptive-os run --policy default --profile light --trials 1 --duration-sec 20 --smoke
```

Calibration tests foreground alone and with a background competitor on the same
CPU. It requires solo misses ≤0.5%, a light paired profile with misses ≤2%, and a
contention profile with misses >2% or p95 lateness >1 ms. Failed attempts remain in
`runs/calibration-*.json` and `runs/runs.sqlite3`; `profiles.json` is usable only
when `valid` is true. Profiles and the workload image ID are then frozen. Build
changes require recalibration. Calibration runs are marked as smoke data and
must not be presented as a repeated policy comparison.

Foreground records every scheduled 20 ms period. A period already overdue when
the process wakes is counted as skipped/missed, rather than silently discarded.
All latency calculations use Linux VM monotonic time. Host receipt time is used
only for freshness, and CPU/work summaries are prorated at window boundaries.
Raw records remain available to recompute the latency percentiles. A sleeping,
labeled probe reads the VM clock and CPU pressure, pinned to another allowed CPU
when one is available. Single-CPU environments record instrumentation as shared.

The runner excludes five seconds of warm-up and requires all measurement windows
to be complete and fresh. Controller CPU time currently measures the Python
process, including its log-reader threads; Docker daemon/CLI CPU cost and host
thermal/power effects are not isolated.

See [the specification](Adaptive_OS_Controller_MVP.md) and
[the implementation plan](docs/superpowers/plans/2026-09-29-adaptive-os-controller-mvp.md).
Current verification results and the calibration gate are recorded in
[implementation status](docs/implementation-status.md).
