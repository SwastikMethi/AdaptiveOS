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
```

Implementation begins with M0 (environment and CPU-weight control). Later
milestones are gated on evidence from the preceding experiment.
See [the specification](Adaptive_OS_Controller_MVP.md) and
[the implementation plan](docs/superpowers/plans/2026-09-29-adaptive-os-controller-mvp.md).
