# Native acceptance handoff — 2026-10-06

## Latest update: native Jev CPU demonstration and guard priority-v5

This section supersedes the older handoff below.

**Newest state:** user re-entered the key; guard-v5 Jev readiness passed
107/108 (99.07%), no timeouts, one invalid proposal, complete audit. Report:
`output/native-readiness-observations/6cf3782fead14492af4776d4f1e36d41/`.
Then native session `ece22bfe-82d9-438b-8171-791e05dcce16` completed with
44 accepted proposals, two model lowerings and one model restoration, zero
Rule changes/fallbacks. Final manual disable restored the last lowering and
stop completed. JSON/CSV/audit/CPU phase report:
`output/native-experiments/b20b491f44f9470685cb3c5976759591/`.
New phase CPU averages are 49.55/49.59 → 80.43/18.80 → 49.42/49.42%.
The observer's per-trial totals are zero because this is a playground session;
use `audit.json` and `cpu-effect-report.json` for live action counts.

The app still runs as PID 30934 with the key intact, no active sandbox/helper
observer. It is on Experiments with Backgrounds 3, Foreground budget 16,
importance Low, but the dropdown still reads Responsiveness comparison.
An async request is pending for the user to select **Shadow: Jev (fixed nice 0)**
without pressing Start. Then run the 180-second 3-background shadow experiment,
followed by the 7-background one; retain and audit full JSON/CSV with
`observe_native_experiment.py --trials 1` and `audit_model_recording.py --shadow`.
Finally restore the user-facing two-workload 16/20 ms demo configuration.

CUA workaround: AX click on the Live policy combo failed, but a coordinate
click plus three Down keys/Return selected Auto: Jev successfully. The
Experiment combo is absent from AX and resisted coordinate/keyboard navigation.
Screenshots can remain stale even while AX and DB advance. Performing the
window's observed **zoom the window** secondary action forced a fresh capture
(window now larger). Do not infer current state from stale screenshots or use
non-CUA input. Native start/stop, tabs, buttons and numeric keyboard editing
work. The broader Laya gates and performance acceptance remain outstanding.

- The user's current request is to verify the full app in AI mode and provide
  a configuration where model decisions visibly affect CPU use.
- Completed a real CUA-operated native Jev run with a 16/20 ms foreground and
  one CPU hashing background, Normal importance, both initially nice 0.
  Session `5955a6c5-9eb9-4d79-b2d3-38abb68b8f30` completed cleanly. One real
  Jev lower (907.92 ms) was linked to Linux nice 0→10 acknowledgement; zero
  Rule interventions. Disabling policy restored nice 0 before sandbox stop.
  Thirty-second foreground/background CPU means were 49.60/49.63 before,
  80.23/19.08 after lowering, and 49.56/49.60 after restoration. Full JSON/CSV,
  audit and raw phase report are in `output/native-ai-demo/<session>/`.
  See `docs/ai-mode-verification.md` for exact steps and limitations.
- The demo exposed a transient-error recovery bug: returned provider
  `unavailable` killed a healthy helper, making the next window an artificial
  second failure. Added that returned error to the reusable-helper allowlist
  in `PolicyController.fail`, and advanced `GUARD_VERSION` to `priority-v5`.
  Two new regressions failed before and passed after; all 172 tests passed.
- New bundle is installed and running at `dist/Adaptive OS Controller.app`,
  PID 30934. Previous bundle is `dist/previous-0cf5633b.app`. Build succeeded
  after approved compiler-cache access; deep/strict signing and checkpoint,
  helper, dependency-lock and licence checks passed. Build evidence:
  `output/native-ai-demo/guard-v5-build.json`.
- CUA launched the new app and opened Providers. The masked memory-only key
  field is focused. An asynchronous request asks the user to re-enter the key
  after restart; no answer has arrived yet. Never read/extract the key. Fresh
  guard-v5 Jev readiness (108 cases at 5-second cadence) is required before
  the final new-build live check. The old guard-v4 readiness cannot unlock it.
- No sandbox, diagnostic helper or our temporary caffeinate process remains
  running. The new native app stays open for key entry. GUI startup works;
  ScreenCaptureKit -3812 remains intermittent. Fresh AX reads recover and
  button/stepper input works. Policy dropdown may still need user selection.
- Laya remains unavailable for live qualification: paced MPS runs and the
  small CPU diagnostic exceeded the one-second gate. Do not rerun unchanged
  validations indefinitely or relax thresholds. Remaining acceptance includes
  native 3/7-background coverage, Laya readiness/visible/minimized 30-minute
  soaks, native held-out calibration and the 50-trial comparison.
- The 16/20 ms demo is diagnostic only: background throughput dropped ~61%,
  exceeding the 40% loss gate. No superiority over Rule is established.

## Earlier handoff (historical)

Implementation remains on the existing dirty `feat/adaptive-os-mvp` checkout.
Preserve all existing changes and prior app bundles. No completion or AI-quality
claim is warranted until the remaining acceptance runs finish.

## Current app

- Running rebuilt bundle: `dist/Adaptive OS Controller.app`, PID 19851.
- Rebuild includes priority-v4 timeout recovery and readiness corpus v2 with
  five-second dispatch cadence. All prior provider readiness is invalidated.
- User re-entered the Jev key in the masked field. Never read or extract it.
  Native Jev corpus-v2 readiness passed 106/108 (98.15%), with one timeout and
  one invalid response retained. Full report is
  `output/native-readiness/readiness-1172d19ddc55-d8b351a1.json`.
- Native Laya MPS corpus-v2 readiness failed 88/108 (81.48%), with 20 retained
  timeouts. Report: `output/native-readiness/readiness-1731274857f5-ae1ce736.json`.
  Laya live and five-arm comparison remain gated. User closed unused apps and
  manually started a repeat native MPS validation; it failed 71/108 (65.74%),
  with 37 timeouts. Observer session 87513 completed; audited evidence is in
  `output/native-readiness-observations/9aa07c1ea0634694add5aaca978753b4/`.
  The live policy dropdown remains Auto: Jev; no sandbox is running.
- App is on Providers; no sandbox is running. Dropdowns have
  resisted automation; ordinary buttons and stepper keyboard entry work.
- Relocated offline readiness finished unsuccessfully: 97/108 (89.81%) within
  one second. Full report preserved in `output/offline-paced-readiness/`.
  Resumed packaging-only checks passed against the same relocated artifact;
  combined report keeps `passed: false` for failed readiness, under
  `output/bundle-acceptance/a097f4a067374e289e6ded21cc8bf097.json`.
  Full 108-case cadence validation takes ~9 min.
- The user restarted the Codex/MCP session. CUA now starts, reads the window,
  captures screenshots and switches tabs. The previous startup failure was
  traced to stale launcher paths in the older REPL services
  (PIDs 12774/12932, started 07:04): their inherited `CODEX_CLI_PATH` is
  `/Applications/ChatGPT.app/Contents/Resources/codex`, which no longer exists.
  Newer ChatGPT services (94354/94443, started 10:57) and the current on-disk
  config use the existing `codex-cli/CodexCLI.app/Contents/MacOS/codex` path.
  The configured Node executable itself exists and runs (v24.21.0). The REPL
  launcher uses CODEX_CLI_PATH to start its kernel; JS resets had retained the
  stale service environment. No signed bundle or sandbox settings were changed.
  Native capture remains intermittent: ScreenCaptureKit -3812/-3811,
  `cgWindowNotFound` and `noWindowsAvailable` occurred during input attempts.
  Rebinding by display name or resetting CUA restored reads. Qt dropdown
  selection is still unreliable, including after keyboard and focus attempts.
  The app is left on Providers in full-screen mode with its masked key intact;
  no new sandbox or experiment was started. The experiment selector last read
  Seven-background control coverage. A connection-test click returned a capture
  error, so its outcome is unverified. Do not bypass CUA with other UI tools.
- A reporting-only rebuild is staged at
  `build/staged-desktop/Adaptive OS Controller.app`; it passed deep/strict
  signing, checkpoint/helper/lock integrity and licence checks. The current
  `dist` app was not moved, replaced or restarted; it retains the memory-only
  Jev key. Staged app native UI validation remains pending.
- User successfully clicked Test Jev connection after the failed repeat. The
  idle Laya model was unloaded. Two isolated source-runtime diagnostics then
  completed and closed their helpers: MPS 8/12 within one second, CPU 9/12.
  Forward inference dominates the delay. Diagnostic evidence is under
  `output/laya-stage-profiles/745cc6e915b544dc8d59c3ce072a8992/` (MPS) and
  `output/laya-stage-profiles/83d58cb504ea4bafad3c214f6996bf3f/` (CPU).
  These are not readiness runs. Do not enable either Laya device from them.

## Latest completed work

- Full current suite passed all 170 tests, including reporting fixes.
  Rebuilt app deep/strict signing passed; previous app bundle preserved.
- Two additional reporting regressions now pass in source: preserve initial and
  every subsequent helper cold start, and separate deadline detection latency
  from helper cleanup. They are in the staged bundle, not the running bundle.
  They do not alter the model/runtime fingerprint or the one-second gate.
- Native playback → New playground correctly refreshed the workload editor
  (Protected foreground → Periodic task, editable defaults). Experiment
  replacement remains to be checked.
- Native Laya shadow with three backgrounds completed safely but timed out on
  the first request and latched Rule: 35 fallback windows, zero interventions.
  `output/native-experiments/78735cb1299144a69f80ade28615fee6/` contains native
  result JSON, full JSON/CSV, audit and recomputation. Do not label this Laya
  availability success. Its measurement/safety checks passed.
- Identical-input replay at five-second spacing reproduced a 1.397 s reply:
  `output/laya-live-diagnostics/f810561504ac416f81c39ff6be463675/`. Recovery fix
  discards late replies without manufacturing a second helper-unavailable
  failure. A still-busy helper at the next window latches Rule and is killed.
- Native Jev timeout: `ce61149f-01a9-49cc-b397-7a3796a9c594`; evidence at
  `output/native-jev-failures/a726a44d528e421f8a71578881574d35/`.
- Native Jev helper-pipe disconnection: `5dd239d6-a2b6-451a-b479-bf3eeedd07e9`;
  evidence at `output/native-jev-failures/47b2d7274a27485fa6abd1dbf8e4f7be/`.
  Both visibly latched to Rule after two failures, kept telemetry and heartbeat
  healthy, and stopped cleanly. Full exports/audits passed. This is helper
  failure evidence, not a real network outage.
- Native one-background Jev shadow previously passed; exports and report in
  `output/native-shadow-jev-1/`.
- Offscreen real-sandbox held-out calibration previously passed nine trials at
  `output/heldout-calibration/db2847756590447eaea30905bb8bd319/`. It does not
  transfer the calibration gate into the native app.

## Next sequence

1. Resolve the remaining native input/capture reliability and address Laya
   latency before repeating readiness; repeated unchanged runs are not a remedy. Do not relax the gate
   or select only passing results. Current native Jev isolated-delay recovery,
   hung-helper latch and pipe-disconnection checks passed; all captures audited
   in `output/native-jev-v4/`. Delay/hang session is
   `88489bcf-1d94-401b-8f66-8d8a5118ef22`; disconnect session is
   `1a883d08-c50f-48ec-8890-1fa10902f5de`. Both completed and restored backgrounds.
   Then select Laya shadow and rerun the 3-background experiment.
   Use `scripts/observe_native_experiment.py` with the new session ID and
   `--trials 1`; it keeps the host awake and saves full recordings plus raw
   recomputation from a read-only database snapshot. Validated against the
   completed Jev shadow at `output/native-experiments/7e3e1521ea9f408dac869f16ff144f44/`.
2. Native 7-background Laya UI coverage and two 1,800-second awake visible and
   minimized runs; use `scripts/verify_native_soak.py` after enabling Auto: Laya.
3. Native held-out 6/20 ms calibration with 3 backgrounds and unchanged gates;
   then the native five-arm 50-trial comparison (180 measurement seconds each).
   Both providers ready and passing native calibration are enforced in the app.
4. Preserve all fallback trials; export native result JSON, audit full JSON/CSV
   and compare raw recomputation. Separate actual model interventions from the
   already-passing deterministic seven-lowering/seven-restoration test.
5. Verify the rebuilt editor refresh through recording playback → New playground
   and experiment replacement. Update evidence docs and full final tests.

The native database is
`~/Library/Application Support/Adaptive OS/Adaptive OS Controller/sessions-v2.sqlite3`.
Only open it with SQLite `mode=ro` while the app runs. **Do not construct
SessionStore on that database**: its constructor marks active sessions interrupted.
The observer safely uses the production exporter on a disposable backup instead.

Native Export results saves relative basenames into `dist`; absolute paths in
the macOS Save As name become colon-containing filenames. Full recording export
requires opening the selected recording first. JSON/CSV parity was already
checked through that native flow for Jev shadow.
