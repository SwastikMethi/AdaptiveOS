# Desktop sandbox implementation verification

The desktop app is separate from the legacy multi-container calibration CLI.
Approved design: `superpowers/specs/2026-09-30-desktop-sandbox-design.md`.

## Evidence collected 2026-09-30

- Full suite: 52 tests passed, including all 34 original tests. Qt offscreen
  prints a harmless font-alias warning on this machine.
- Exactly one container held eight periodic/CPU/memory/I/O processes plus the
  collector. Workloads shared CPU 0, collector CPU 1. Non-root UID 65534,
  read-only root and 1 GiB memory limit verified. Reversible nice 0→10→19→0
  verified without extra capabilities. Ninth workload and fifth 128 MiB memory
  allocation rejected. Scratch files remained ≤64 MiB each.
- `build/verification/1fe21510-3570-4253-80d7-c5fa13fdc0e6`: eight-workload check,
  scoped stops, priority readback, combined memory cap, heartbeat expiry, JSON/CSV
  export parity and exact container/volume cleanup passed.
- `build/ui-verification/a4ebf977-7cf8-4067-9e4c-8e54c83dd198`: 600-second Qt
  application soak passed with 8 workloads, 4,680 samples and 4,539 raw envelopes.
  UI plots plateaued at 2,400 points (300 × 8); measured process max RSS remained
  86,261,760 bytes. Queue usually empty. History reopening/export and normal
  shutdown passed. Later supervisor-only backpressure/latency hardening received
  the eight-workload integration check above, not a second 600-second soak.
- `build/crash-verification/cfb084e4-3b7f-4351-9e74-b22a90fb3e22`: force-killing
  the real session-host process stopped its sandbox within the bounded lease.
  SQLite recovered the session as interrupted with samples retained. Exact
  leftover cleanup passed; recordings retained.
- Local Apple Silicon bundle built with PySide6 6.11.2 and Nuitka 4.1.1,
  approximately 78 MiB. `codesign --verify --deep --strict` passed. Bundled app
  launched with PATH restricted to system binaries and no VIRTUAL_ENV, found
  Docker Desktop, used bundled sources and started its sandbox. No web service.
- Native development-window start/all/telemetry visually checked. The initial
  layout clipped secondary plots; they now occupy tabs below the shared CPU
  chart. Further packaged-window automation hit macOS ScreenCaptureKit errors;
  automated offscreen control/history/export checks passed. Full final visual
  interaction coverage is not claimed.

Two long-idle manual app sessions disconnected and cleaned their resources.
The latter recorded a normal Docker attach exit with no stderr; its exact cause
was not established. Automated ten-minute active-use soak and crash checks
passed. Failure recording now retains transport exit and diagnostic evidence.

## Checks to rerun

```sh
.venv/bin/python -m unittest discover -q
.venv/bin/python scripts/verify_desktop.py --seconds 45 --workloads 8 --lease
.venv/bin/python scripts/verify_desktop_ui.py --seconds 600
.venv/bin/python scripts/verify_desktop_crash.py
.venv/bin/python scripts/build_desktop.py
codesign --verify --deep --strict 'dist/Adaptive OS Controller.app'
```

Public signing/notarization, other-platform bundles and automatic scheduling
policies remain explicitly out of scope. No existing legacy run data was removed.
