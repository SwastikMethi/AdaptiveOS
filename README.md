# Adaptive OS Controller

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

Implementation begins with M0 (environment and CPU-weight control). Later
milestones are gated on evidence from the preceding experiment.
See [the specification](Adaptive_OS_Controller_MVP.md) and
[the implementation plan](docs/superpowers/plans/2026-09-29-adaptive-os-controller-mvp.md).
