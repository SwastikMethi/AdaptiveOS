# Desktop sandbox implementation verification

The desktop app is separate from the legacy multi-container calibration CLI.
Approved design: `superpowers/specs/2026-09-30-desktop-sandbox-design.md`.

## Evidence collected 2026-09-30

- Full suite: 55 tests passed, including all 34 original tests. Qt offscreen
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
- Final review reproduced two important defects: disconnected shutdown could
  discard queued evidence, and workload replacement/churn could create unbounded
  historical chart series. Failing regressions preceded both fixes. Shutdown now
  drains valid queued messages before terminal transport errors, retains failure
  diagnostics, and never reports a missing close acknowledgement as clean.
  Reopened recordings page through eight workloads at a time, with at most 300
  observed samples each. Exports still include every workload and raw record.
- `build/ui-verification/c705c4bb-74d2-4ff7-ac27-815d0fe2c8b6`: post-review
  45-second real-Docker check passed with eight workloads, priority controls,
  bounded plots, normal shutdown, reopening and export.
- A packaged native accessibility scan crashed in Qt Cocoa's selected-table
  traversal. The app now selects one cell to identify a workload, rather than
  seven cells in a full row. This avoids the sibling-interface invalidation path
  in [Qt's Cocoa accessibility implementation](https://github.com/qt/qtbase/blob/v6.11.2/src/plugins/platforms/cocoa/qcocoaaccessibilityelement.mm).
  A selection regression and subsequent native interaction checks passed. No
  accessibility features were disabled and no framework binary was patched.
- The rebuilt, signature-verified bundle passed a minimal-PATH/no-venv native
  walkthrough: start sandbox, start both workloads, observe measurements,
  change priority 0→10→19→0, stop one workload, stop sandbox, reopen recording,
  and export JSON through the macOS save dialog. Recording
  `b538a7ba-bcc4-4c19-a425-ba1a4a26d1a2` completed with 140 persisted/exported
  messages. The test app exited normally. Container and volume queries were
  empty afterward; all recordings were retained.

Two long-idle manual app sessions disconnected and cleaned their resources.
The latter recorded a normal Docker attach exit with no stderr; its exact cause
was not established. Automated ten-minute active-use soak and crash checks
passed. These earlier disconnects remain unexplained; the later accessibility
crash does not establish their cause. Do not treat the active-use soak as proof
of indefinite background or sleep/wake reliability. Failure recording now
retains queued closing messages, transport exit and diagnostic evidence.

## Deferred minor

The recording list shows only the newest 200 sessions. Older sessions remain in
SQLite but cannot currently be selected/opened/exported through the UI. Add
recording-list paging before using this for a larger session archive. Workload
paging *within* an opened session is implemented and tested independently.

## Execution decisions

1. Recorded the already-approved conversational design without another approval
   gate, since implementation was explicitly requested. Cost if wrong: revise
   the written design without expanding scope.
2. History browsing/export requires stopping the sandbox, keeping the single
   database/heartbeat worker responsive. Cost if wrong: add a separate read-only
   history worker for concurrent use.
3. Initially accepted automated Qt checks plus bundle startup when native screen
   capture was unavailable. Cost if wrong: visual-only defects need follow-up.
   This limitation was subsequently reduced by the native walkthrough above.
4. Kept unexplained earlier idle disconnects as a disclosed local-build
   limitation instead of guessing at an App Nap fix. Fail-safe shutdown and
   saved evidence remain in place. Cost if wrong: an interrupted background run
   may need repeating and diagnosis from the improved failure recording.
5. Left priority popup logic unchanged after native 0→10→19→0 acknowledgement
   succeeded; the initial interaction suspicion was not reproduced. Cost if
   wrong: repeat the native interaction check on the affected machine.
6. Accepted fresh local bundle/signature/minimal-PATH and native walkthrough
   evidence despite the read-only reviewer not launching the bundle. Cost if
   wrong: platform-specific packaging defects need a second-machine check.
7. Used single-cell workload selection to avoid the observed Qt Cocoa crash,
   retaining keyboard selection and native accessibility. Cost if wrong: other
   Qt accessibility paths could require an upstream fix; this is not a claim
   that all Qt accessibility defects are resolved.

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
