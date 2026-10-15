# Adaptive OS Controller

The optional [Guest Linux backend](docs/guest-linux.md) implements CPU weights,
separate memory protection and headroom controls, shared upload/download shaping,
Manual/Rule policies, and gated Jev control on UTM/Plasma. Restoration leaves
applications open. The guest guide records verification results, calibration and
comparison commands, model qualification, and remaining limits; implementation
alone does not qualify a performance or AI-usefulness claim.
Run `./start.sh --backend guest-linux --jev-env-file .env` **inside the configured
Ubuntu 24.04 ARM64 / KDE Plasma (X11) guest in UTM**; Docker remains the default on the Mac.

## Native desktop app (macOS)

One Debian Linux Docker sandbox contains **all** competing workloads and its
telemetry collector. This is a native Qt app, not a website. Docker Desktop must
be installed and running; no browser, ports, or Docker socket inside the sandbox.

Choose **CPU**, **Memory**, or **Network** before starting. The new resource modes
provide real resizable caches and bandwidth-limited loopback traffic, with manual,
Rule and separately gated model control. See [resource modes and verification](docs/resource-modes.md)
for budgets, controls, calibration, and current provider qualification limits. The CPU
workflow below remains available.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop]'
.venv/bin/adaptive-os desktop
```

For repeat launches from this checkout:

```sh
./start.sh                                      # latest source, normal readiness gates
./stop.sh                                       # save and clean up, then exit
./start.sh --allow-unqualified-jev-demo          # explicit experimental Memory/Network Jev demo
```

The scripts work from any directory and use the existing `.venv`; they do not
install dependencies or rebuild the Mac app. Close previously opened app instances
normally before switching. `start.sh` opens the UI in the background; start the
sandbox and workloads inside the app. Docker Desktop must be running for workloads.
Recordings, launcher state and the appended `app.log` live in `output/desktop/`.
Existing desktop options are forwarded, so `./start.sh --data-dir PATH` selects
another recording folder while launcher state and logs stay in `output/desktop/`.
To reuse your Jev key after restarting, copy `.env.example` to `.env` in the
project folder and fill in `JEV_API_KEY=your-key` privately. Then launch with:

```sh
./start.sh --jev-env-file .env
# For the experimental demo, combine both flags:
./start.sh --jev-env-file .env --allow-unqualified-jev-demo
```

The flag prefills the masked **Providers** field; connection/readiness checks
and policy selection still work as usual. `start.sh` resolves relative file paths
from the project folder. Direct desktop commands resolve them from the current
directory. Without the flag, enter the key in the UI each time.

The file accepts a single `JEV_API_KEY` assignment, optionally quoted or prefixed
with `export`, plus comments. Other entries are ignored; shell commands and
variable substitutions are never executed. Missing, empty or malformed keys
stop startup with a redacted error in `app.log`. The app reads the file without
modifying it or exporting its contents to child-process environments. It never
writes the key to logs, recordings or launcher state. `.env` is Git-ignored but
contains your plaintext key; keep it private (for example, `chmod 600 .env`).

`stop.sh` checks the recorded PID, process start time, owner and launch command.
It stops only the script-managed instance and waits up to 60 seconds for the
existing cleanup sequence. Repeated starts/stops are safe. A timeout retains the
launcher state and reports the log path; it does not force-kill the app. Recordings
are kept. The scripts do not change working-set sizes or qualification results.

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

While a sandbox session is active, the macOS app holds a scoped user-initiated
activity to prevent App Nap from delaying its heartbeat. The activity ends on
stop, failure, or quit and does not prevent idle system/display sleep or change
macOS settings. Keep the Mac awake for experiments; interruption still requires
an explicit restart. Activity acquisition/release is included in full recordings.

The **Recordings** tab opens saved charts/events without Docker and exports full
JSON or CSV records. Stop the live sandbox before browsing/exporting. The live
plots retain 300 points; history shows up to 300 observed points per workload;
exports retain every recorded sample and raw tick. Desktop data is separate from
legacy `runs/`, in Qt's macOS Application Support folder for Adaptive OS Controller
(the exact path is shown in the status bar). Override it with `desktop --data-dir PATH`.

Build a locally runnable app (Xcode Command Line Tools required):

```sh
python3.11 -m venv build/laya-venv
build/laya-venv/bin/python -m pip install -r requirements-laya.lock
build/laya-venv/bin/python -m pip install --no-deps -e .
.venv/bin/python scripts/prepare_laya.py
build/laya-venv/bin/python scripts/build_model_helper.py
.venv/bin/python scripts/build_desktop.py
open 'dist/Adaptive OS Controller.app'
```

The bundle includes Python, Qt, the sandbox sources, a separate inference helper,
and the pinned English Laya checkpoint; no activated venv is
needed to launch it. It still needs Docker Desktop. The build installs Nuitka
4.1.1 and uses pyside6-deploy. Ad-hoc local signing is included; public distribution
signing/notarization is not. Previous local bundles are retained in `dist/previous-*.app`.
Model dependencies and their licences are inside `Contents/Helpers/model_helper`.
PySide6/Qt and Nuitka retain their respective third-party licenses.

Verification (Docker checks create and clean only their own session resources):

```sh
.venv/bin/python -m unittest discover -q
.venv/bin/python scripts/verify_desktop.py --seconds 45 --workloads 8 --lease
.venv/bin/python scripts/verify_desktop_ui.py --seconds 600
```

If Docker is missing or stopped, launch it and click Start sandbox again. Build
errors appear in **Raw events / build log**, which opens during builds. A disk/recording error stops the sandbox rather
than silently losing measurements. Keep `sessions-v2.sqlite3` plus its WAL files
together for recovery; do not edit an active database. Custom shell workloads,
native macOS process control are not included.

## Decision providers

The Live playground offers Manual, Auto: Rule, Shadow: Jev/Laya and guarded
Auto: Jev/Laya. Choose roles and importance before enabling a policy. Load Laya
or supply a Jev key in **Providers** (or with `--jev-env-file .env`) before starting
workloads. Keys are masked, kept in memory by the app and excluded from recordings;
an optional env file remains user-managed. Jev receives anonymous synthetic
metrics; the network-disabled workload sandbox is unchanged.

**Decision activity** sits below the workload table and CPU chart. It shows the
requested policy, who currently controls priorities, the next expected model
call or Rule check, and the separate priority-change cooldown. A failed call's
detection time is not presented as model response latency.

The newest-first timeline separates each proposal from its outcome: held,
blocked, shadow-only, awaiting confirmation, or confirmed. Expand a row to see
the measurements, probabilities, timing, guard result and request identifiers;
confidence is not an accuracy score. Select a confirmed change to highlight its
CPU-chart marker. These marks indicate acknowledged priority changes, not proof
that the change caused an improvement. Expanding or selecting keeps your place;
**Jump to latest** resumes following new decisions. **Older/Newer** pages through
100 decisions at a time, including saved recordings. Full exports retain all
events. Drag the divider above the panel to give its timeline more room.

For a visible CPU demonstration, see the [native AI verification and exact
configuration](docs/ai-mode-verification.md). It includes the measured Jev
priority change, CPU tradeoff, restoration, and the current Laya readiness limit.

Models choose hold/lower/restore every five seconds. A separate helper keeps
inference out of Qt and the heartbeat worker. Replies must arrive within one
second; unusable replies use Rule, and two consecutive failures latch to Rule
until explicitly re-enabled. Code selects eligible backgrounds, enforces the
observation period and cooldown, and applies identity-bound acknowledged
priority changes. Shadow experiment arms retain nice 0.

Live model modes remain locked until the exact model/prompt/feature/guard/device
configuration passes at least 100 frozen contexts with >=95% valid replies
within one second. CPU is an explicit Laya alternative with separate readiness.
Laya uses verified local weights, no runtime downloads, and a working copy in
Application Support so the signed bundle remains immutable. Readiness measures
safe operation and latency; it is not evidence that a model improves scheduling.

Experiments include held-out 6 ms calibration and a five-arm, 50-trial paired
comparison. Reports retain fallback trials, show Manual and each background,
and evaluate model usefulness against Rule on contention miss rate. Full JSON
and CSV recordings include versioned contexts, proposals, rejections, fallback
and acknowledgement links. Throughput uses unique raw cumulative counters;
historical exports can be recomputed to a new file with:

```sh
.venv/bin/python scripts/recompute_desktop_report.py recording.json report-v2.json
.venv/bin/python scripts/evaluate_model.py --provider laya --device mps
```

Implementation and acceptance evidence are tracked in
[the integration ledger](docs/jev-laya-integration.md).

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
