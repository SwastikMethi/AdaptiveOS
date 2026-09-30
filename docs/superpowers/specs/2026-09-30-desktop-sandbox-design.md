# Desktop sandbox app — approved design

This records the plan approved in conversation. macOS/Apple Silicon first,
Python 3.11+, PySide6 Widgets, native window, no browser or HTTP service.

## Experience

Open the app, check Docker Desktop, prepare its bundled pinned Debian image,
start one sandbox, and run 2–8 built-in competing processes. Start with a 20 ms
periodic task (4 ms CPU budget) and a continuous hashing task. Add bounded
memory-touch (64 MiB default, 128 MiB each/512 MiB combined) and sequential
file I/O (64 MiB scratch file) presets. Change configurations while stopped;
change nice priorities live: Normal 0, Reduced 10, Minimal 19. No arbitrary
commands, automatic policy, calibration gate, or host process control.

One container contains all workloads AND supervisor/telemetry. Pin workloads
to the first allowed CPU; collector uses another when available. Container is
non-root, network none, read-only root, capabilities dropped, no-new-privileges,
1 GiB memory including swap, pids limit 64. I/O uses an exact session-owned
Docker volume, never host mounts or the Docker socket. Labels identify the
desktop architecture and session UUID, separate from legacy experiments.

Use `--ulimit nice=20:20`, verify 0→10→19→0 on an owned child inside this
same sandbox, and disable controls with an explanation if unsupported. Never
escalate capabilities. Retry without the ulimit only after cleaning a failed
start. Commands are allowlisted, versioned JSONL over Docker stdin/stdout,
without a TTY; acknowledgements report read-back effective state. Track PID
start identity and scoped child process groups. Heartbeat every second, lease
10 seconds; expiry stops children and exits after a force-killed GUI.

## Telemetry and recordings

Sample every second using Linux monotonic time: per-process CPU (100%=one CPU),
RSS, kernel I/O counters, effective nice and application throughput. Missing
values stay null. Periodic tasks persist raw ticks, deadline misses, wakeup
delay and nearest-rank p95 lateness over five-second windows, with the first
five seconds warm-up. Show stale state after three seconds of no fresh receipt;
charts must have gaps, not zeros or interpolated stale intervals.

Separate version-2 SQLite database in macOS Application Support, one worker
owns its connection, batched writes, bounded queues and 300-point live series.
Persist sessions, configurations, raw records, samples, commands, acknowledgements,
and lifecycle events. Record CPU/environment/image identity. Offline history
reopens charts and events; full-session JSON/CSV export. Legacy CLI and `runs/`
remain unchanged and labeled legacy. Results are observational, not causal
benchmarks. Distinguish application I/O from kernel storage I/O and cache effects.

## Native UI

Toolbar: prepare image, start/stop sandbox, add workload, start/stop all.
Workload table: name, preset, state, CPU, RSS, throughput, priority. Live CPU
and memory charts plus selected-workload throughput/latency. Details/config,
start/stop and priority control; event timeline; recording history and export.
Qt-painted charts, native keyboard-accessible controls, numeric units and
legends. All Docker, database and build operations off the UI thread. Pending
actions stay pending until acknowledged. Closing an active session confirms
stop/quit or cancel. Docker loss, build errors, stale samples, crashed workers,
disk failure, and interrupted-session recovery are explicit. Cleanup verifies
exact labels before removal; never silently remove another session.

Visual direction: laboratory process monitor, quiet native controls and a large
shared-CPU plot as the focal point. System sans-serif, left-aligned numbers and
labels, white #ffffff, slate #243247, blue #2463a8, teal #177e89, amber #a76618,
muted gray #64748b; distinct line colors and textual legends, no decorative cards.

## Delivery

1. Single-container engine and reversible priority.
2. Four presets, attributed telemetry, persistence and exports.
3. Responsive native app, history and live controls.
4. pyside6-deploy macOS .app, failure checks and 10-minute mixed-workload soak.

Docker Desktop is installed separately. Public signing/notarization and other
platform packaging are excluded. Locally built app must launch without a venv.
