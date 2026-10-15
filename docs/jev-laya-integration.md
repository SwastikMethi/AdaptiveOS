# Jev and Laya integration

Source of intent: the Jev and Laya Integration Plan supplied on 2026-10-05.

## Contract

Separate Manual, Auto: Rule, Shadow: Jev/Laya and readiness-gated Auto: Jev/Laya.
Five-second windows, one-second host-monotonic deadlines, one owned inference
helper process and at most one request. Models select hold/lower/restore; code
selects targets and uses the existing identity-bound acknowledged control path.
Ten seconds of valid post-warm-up observation; one step from the original nice
value; Low/Normal/High lowering order with CPU/ID tie-breaks; High-first reverse
restoration; 30 seconds between verified acknowledgements; background CPU >=20%.
Rule fallback on unusable output, latched after two failures; immediate latch on
authentication failure. Cancellation and stale responses never affect control.
Keep missing metrics unknown, complete seven-background inputs, and versioned
audit events. Keep the network-disabled sandbox and ten-second lease unchanged.

Jev is pinned to jev-1.13.0, with actual version recorded; direct typed-choice
HTTP, no retries, server backoff, anonymous metrics, memory-only credentials.
Laya 0.3.27 and convaiinnovations/laya are pinned and bundled with dependencies
and licences. Private pipes, lazy loading, MPS by default, bounded CPU threads,
no compilation or device fallback. Verify an Application Support working copy
without modifying the signed bundle. No runtime downloads/installations.

## Tasks

1. Repair throughput using unique raw cumulative process-instance streams;
   reject conflicts/resets/gaps, clip boundaries, preserve historical reports.
2. Extract controller coordination and prove fake-provider deadlines,
   cancellation, state-preserving fallback and guarded acknowledgements.
3. Add hosted Jev and bundled Laya; validate contracts and offline relocation.
4. Add shadow/UI/audit/readiness integration. Fixed nice 0 in shadow experiments.
5. Add gated live modes and reproducible evaluation/comparison tooling.
6. Verify native UI at one/three/seven backgrounds, deterministic seven-change
   restoration, awake 30-minute visible/minimized Laya checks, Jev failures.
7. Evaluate >=100 frozen contexts per runtime: >=95% valid within one second,
   zero unauthorized actions, complete audits; report cold/warm latency and
   process overhead. Invalidate on model/prompt/features/guards/device changes.
8. Calibrate held-out 6/20ms using unchanged thresholds, freeze configuration,
   then five randomized paired blocks x two profiles x five arms = 50 trials
   of 180 measurement seconds. Retain fallback trials. Report each background,
   Manual and Rule comparisons, and no invented gains from zero baselines.

AI usefulness requires improvement over Rule on contention miss rate and the
existing >=20% foreground improvement / <=40% background loss vs Default gate.
Safety/reproducibility do not imply superiority. Legacy CLI, fine-tuning,
cascades, host scheduling and network/memory controls are outside this change.

## Evidence ledger

- Baseline: `python -m unittest discover -q`: 90 tests passed. This repository
  uses unittest; pytest is absent from the existing environment.
- Ruling: work in the existing feature checkout because its uncommitted Rule,
  experiment and restoration implementation is required input. Preserve those
  changes and historical evidence. A clean worktree would omit that input.
- Pre-flight: reporting consumes raw app envelopes already recorded by worker;
  controller consumes Rule state and existing control acknowledgements; provider
  helpers consume anonymized contexts; UI consumes readiness and audit events;
  live experiments consume the same guard path and repaired reporting.
- External prerequisite: no Jev credential is available in the process
  environment. Hosted readiness/performance require a key entered in the app.
- Pending: all implementation and new acceptance checks above.

### Implementation progress

- Reporting: regression suite passed (duplicates/conflicts/resets/process restarts,
  gaps, clipping, export recomputation). New historical report:
  `output/performance-report-v2-2026-10-05.json`: 30/30 valid, every background
  has exactly 180 seconds of unique raw coverage. Historical files unchanged.
- Controller: fake-provider and private-process tests passed for deadlines,
  cancellation, stale identities, no shadow changes, bounded queues and Rule
  state preservation. Qt integration uses the same acknowledged priority path.
- Providers: Jev adapter has typed-choice validation, no retries, memory-only
  secret transport, immediate authentication disable and server backoff. No key
  is currently available for hosted acceptance.
- Laya: checkpoint pinned to 7b928d828b7b0e022f929d9bd2e44165aa270148;
  digests in `laya-checkpoint.json`; isolated runtime in `build/laya-venv`;
  dependencies in `requirements-laya.lock`. First real MPS warm response 211 ms,
  cold start 15.07 s. GPU unavailable under the tool sandbox; GPU checks were
  explicitly approved to run outside it. 108-context evaluation is in progress.
- Native UI: provider selection, masked credential entry, device selection,
  readiness evidence, shadow/live gates, status and audit events implemented.
- Pending: final packaging/relocation, broader guard/experiment regressions,
  actual native computer-use checks/soaks, Jev readiness, held-out calibration
  and 50-trial comparison, whole-change review.
- Laya MPS rerun after probability rounding fix: 108/108 valid within 1 s,
  p50 114.97 ms, p95 192.08 ms, p99 210.24 ms; no timeouts. Evidence:
  `output/model-readiness/readiness-074d79b879c8-cb0f08a9.json`.
  Dependency-lock hashing was subsequently added to the readiness fingerprint;
  packaged readiness will be rerun with that final fingerprint.
- Native regressions now include serialized restoration commands, preventing
  overlapping priority changes; five-arm manifests/reports and held-out 6 ms
  calibration/shadow experiment flows are implemented. Full suite: 133 tests.
- User will enter the Jev key in the app once the rebuilt app is ready.
- Packaging refinement: restrict Transformers families to auto/bert/modernbert,
  the pinned checkpoint's dependencies. The initial unrestricted compiler walk
  was stopped because it visited hundreds of unused model families.
- Readiness is latency/schema evidence only. On the 108-context MPS run, Laya
  proposed lower in every context (0 hold/restore); deterministic guards remain
  mandatory, and no scheduling-quality claim is supported by this result.
  Maximum observed input was 726 tokens; peak helper RSS 1,881,735,168 bytes;
  average helper CPU per decision 0.03854 seconds.
- Controller guard version priority-v3 adds duplicate-current-task rejection,
  full validation deadline accounting, proposal/command/ack links, and rejects
  acknowledgement times before the command window. Regression tests failed
  first and passed after these fixes. Automatic Qt acknowledgements now require
  the controller binding, without an unbound cooldown fallback.
- Calibration qualification is bound to image, allowed CPUs, workload/collector
  placement, workload presets/period, budget, importance and background count.
  Failed/cancelled calibration clears old qualification. Source suite reached
  143 passing tests; an additional readiness deadline regression also passed.
- Packaging compiler cache required approved access outside the tool sandbox.
  The narrowed standalone helper build is running; app rebuild follows it.
- Ruling: conduct the fresh whole-change review after implementation while
  packaging acceptance is underway, so findings can be fixed before the app is
  handed to the user for memory-only credential entry. Acceptance evidence is
  still pending; review does not substitute for those checks.
- Fresh review: five Important findings; no Critical/Minor findings. All fixed
  with observed RED→GREEN regressions: raw CPU envelopes now reach the live
  consumer and agree with full-export recomputation; ready captures calibration
  identity before preflight; missing background CPU produces distinct invalid
  controller windows and restoration; policy controls are locked during trials;
  helper owner/pipe monitoring stops blocked warm-up or inference after parent
  death or EOF. Full source suite: 150/150 passing.
- Final: Ruling: review deferred judgment on packaged/offline operation, hosted
  Jev, real Laya resource use, native UI/soaks, sandbox seven-change restoration,
  held-out/scored trials and upstream checkpoint provenance. These remain
  acceptance work below, not implied passes. Upstream revision/digests were
  retrieved during preparation; native/runtime checks still need completion.
- Packaging diagnosis: Nuitka attempted to sign 9,903 paths (1.41 MB of argv),
  including 9,805 Torch SDK headers. Signing the 98 runtime paths succeeded.
  Build excludes torch/include using Nuitka's documented noinclude-data-files;
  native/JIT extension compilation is already disabled by the runtime contract.
  The full rebuild will incorporate helper ownership monitoring and licences.
- Deterministic real-sandbox test passed: seven acknowledged lowerings and
  seven serialized restorations, >=30 s acknowledgement spacing, Low/Normal/High
  lowering and High-first reverse restoration, original foreground unchanged.
  This is explicitly a fake-provider control test, not real-model evidence.
  Recording/report: `output/deterministic-control/5382521f-dcc1-4c30-92e5-2f48b09cc7a1/`.
  Exact session container and volume removal verified after cleanup.
- Source suite now 152/152 passing. Per-trial inference cost summaries and each
  background's loss versus Default are included in the five-arm report.
- Compiled helper first smoke returned a sanitized startup failure. It did not
  pass readiness. A rebuild with explicit local-Laya diagnostic support is in
  progress; no credentials enter diagnostic output, and Jev diagnostics remain
  disabled. Runtime startup exits now report unavailable immediately.
- Pinned-runtime audit found Laya's internal GPU-to-CPU retry. Regression tests
  failed before the fix: loading on a different device now fails readiness, and
  fixed eager FP32 inference propagates GPU errors to guarded Rule fallback.
  Readiness fingerprint includes this runtime change. Source MPS rerun:
  108/108 in deadline, p50 119.58 ms / p95 195.56 ms / p99 208.14 ms, cold start
  13.92 s (`output/fixed-device-readiness/readiness-1c88ff98ec3a-a00f715f.json`).
  Full source suite at this point: 154 passing tests.
- Standalone tokenizer startup requires Transformers' encoder_decoder config
  in addition to auto/bert/modernbert. Rebuild includes that dependency and the
  CPython licence. Licence metadata is copied verbatim to avoid parsing long
  distribution descriptions as email headers.
- Checkpoint preparation now verifies both cached and freshly downloaded files
  against the reviewed `laya-checkpoint.json`; it cannot bless corrupt cached
  weights by replacing the source manifest. Two RED-to-GREEN regressions pass;
  all six pinned checkpoint files verified again.
- Native ModernBERT import uses a transitive `torch.compile` decorator despite
  Agent's compile=False. Its compiled startup failed in an unused Inductor
  configuration module. The supported TORCHDYNAMO_DISABLE=1 setting avoids that
  path; child environments enforce it and readiness identity includes it.
  Regression failed before the change and passes after. Native smoke passed;
  complete standalone MPS readiness: 107/108 in deadline (99.07%), 1 retained
  timeout, p50 122.59 / p95 245.33 / p99 748.40 ms, measured cold start 8.34 s.
  Evidence: `output/standalone-readiness/readiness-87c65b0bbc48-9482706e.json`.
- Native app rebuilt with the standalone helper and pinned weights; preceding
  bundles retained. Relocation/offline acceptance is running. A connection-only
  test now displays retained full readiness accurately (RED-to-GREEN regression).
- Relocated/offline acceptance passed with a clean environment, empty external
  model cache and macOS network-deny profile. 108/108 responses in deadline;
  p50 114.03 / p95 221.99 / p99 254.68 ms; cold start 21.09 s; maximum input
  726 tokens; peak helper RSS 2,903,851,008 bytes. All proposals were lower;
  this remains latency/schema evidence, not evidence of scheduling benefit.
  Corrupt/missing weights and missing Jev credentials disabled cleanly. Cache
  remained empty, checkpoint digests unchanged and deep strict codesign passed.
  Report: `output/bundle-acceptance/09382a7e1e1a425ba10fc0cb3f778c35.json`.
- Full source suite: 158 passing tests. Rebuilt app opened through computer use,
  Providers panel visually verified; user notified that masked key entry is ready.
- User entered the Jev key directly into the masked native field. Native Jev
  connection test and full readiness passed: 108/108 in deadline, zero timeouts,
  p50 400.15 / p95 500.68 / p99 698.99 ms; 85 hold, 21 lower, 2 restore proposals.
  Actual returned model jev-1.13.0, maximum input 1,221 tokens, peak helper RSS
  48,365,568 bytes, mean helper CPU 0.00847 seconds per response. Evidence copied
  to `output/native-readiness/readiness-82754fea555f-fab00995.json`.
- Three additional bundled MPS cold starts: 7.71, 9.35 and 50.74 seconds.
  Observed p50 9.35 s and p95/p99 50.74 s (only three samples; slow start retained).
  Combined readiness/resource summary: `output/provider-readiness-summary.json`.
- Native window access was lost with cgWindowNotFound before any shadow trial
  started. App/helper processes remained alive. Access recovered after user
  requested retry; Qt dropdown automation is being resolved. Native shadow/live
  workload checks, both awake soaks, Jev failures, calibration and scored trials
  remain pending and must not be reported as completed.
- Held-out 6 ms / 20 ms calibration passed all nine real-sandbox trials with
  unchanged thresholds: average miss rates solo 0.0028889, light 0.0046667,
  contention 0.9911111; contention p95 lateness 24.277211 ms. This was run
  through the offscreen desktop controller, so it does not claim native UI
  acceptance or transfer qualification into the independently running app.
  Evidence: `output/heldout-calibration/db2847756590447eaea30905bb8bd319/`.
  The native app has now started its one-background Jev shadow trial after
  that calibration session finished and released the sandbox.
- Native one-background Jev shadow completed 180 measurement seconds. All 36
  contexts returned in deadline (36 hold, zero fallback/interventions), p50
  406.42 / p95 506.02 / p99 626.91 ms. Full native JSON and CSV exports match
  all 1,005 messages, with 393 raw envelopes; priorities stayed at nice 0,
  every request has an outcome and Rule reference, and no credential fields
  appear. Native report matches fresh raw-counter recomputation with 180 s
  unique background coverage. Evidence: `output/native-shadow-jev-1/`.
- Native Jev failure checks passed after explicit live enablement. Pausing the
  exact app-owned helper produced deadline then helper-unavailable failures;
  disconnecting its private pipe by terminating that helper produced two
  helper-unavailable failures. Both visibly latched to Rule, kept telemetry
  flowing (largest gaps 1.02 / 1.09 s) and heartbeat age below one second, and
  completed sandbox cleanup. These test helper failure, not a network outage.
  Reports: `output/native-jev-failures/a726a44d528e421f8a71578881574d35/`
  and `output/native-jev-failures/47b2d7274a27485fa6abd1dbf8e4f7be/`.
- Native recording playback exposed stale workload editor values when a new
  workload set kept the same selected table cell. Explicitly refreshing the
  editor fixes both New playground and experiment replacement. The regression
  failed before the fix and passes after; the full suite now passes 159 tests.
  The running bundle predates this cosmetic fix and will be rebuilt after the
  key-dependent checks, preserving the current memory-only Jev credential.
- Native Laya MPS readiness passed all 108 contexts with zero timeouts:
  p50 199.64 / p95 201.59 / p99 299.83 ms, cold start 7.09 s, peak helper
  RSS 2,505,162,752 bytes and maximum input 726 tokens. All proposals were
  lower; the guard remains responsible for authorizing any real intervention.
  Evidence: `output/native-readiness/readiness-87c65b0bbc48-55deb848.json`.
- Native three-background Laya shadow exposed a deadline failure on its first
  live input. The original helper cancellation made the next window unavailable
  and latched Rule. All 180 measurement seconds, 35 fallback windows and zero
  interventions are retained. JSON/CSV and raw recomputation agree; every task
  stayed at nice 0. Measurement/safety passed, but Laya availability did not.
  Evidence: `output/native-experiments/78735cb1299144a69f80ade28615fee6/`.
- A six-request replay at five-second spacing reproduced variable helper latency
  on identical 363-token input: 216–1,397 ms, including one deadline miss.
  Evidence: `output/laya-live-diagnostics/f810561504ac416f81c39ff6be463675/`.
- Guard revision priority-v4 rejects the expired request immediately, then drains
  and audits any late reply. A fresh window may use the same recovered helper;
  a helper still busy at that window causes the second failure and is terminated.
  Stop/mode change still terminates expired inference immediately. No retries,
  queued windows, late controls, deadline extension or device fallback were added.
  RED-to-GREEN regressions cover recovery, hung-helper latching, stop, consumed
  validation replies, real pipe reuse and late-response resource accounting.
- Readiness corpus v2 includes 0–3 trend windows, unknown task CPU/work and
  realistic decimal counters. Full readiness uses the actual five-second
  dispatch cadence; connection-only checks remain one request. Guard/corpus
  changes invalidate all earlier readiness fingerprints. Rebuilt-app readiness,
  native shadow/live checks and remaining performance gates must run again.
- The rebuilt app passed deep/strict signing. The first relocated/offline
  corpus-v2 run at five-second spacing failed readiness: 97/108 within one
  second (89.81%), 11 retained deadline failures, complete audit and zero
  unauthorized actions. Observed response/deadline-detection p50/p95/p99 were
  310.58/1,056.47/1,060.12 ms; these timeout observations include cancellation
  time and do not measure when the unanswered inference would have finished.
  Peak observed helper RSS was 3,017,883,648 bytes, maximum accepted input 742
  tokens. Its reported cold-start value is the latest helper restart, not the
  initial startup; initial cold-start latency is unavailable for this run.
  Evidence: `output/offline-paced-readiness/readiness-1731274857f5-854334b7.json`.
  The packaging harness stopped at the failed readiness assertion, so the new
  run did not repeat corruption/missing-file checks. Prior packaging evidence
  remains historical. Live Laya acceptance is not established by this run.
- Resumed only the packaging checks against that same relocated artifact,
  preserving the failed readiness report: corrupt/missing weights and missing
  Jev credentials disabled cleanly; external cache remained empty; checkpoint
  verification and deep/strict signing passed. The combined report correctly
  retains `passed: false` because readiness failed:
  `output/bundle-acceptance/a097f4a067374e289e6ded21cc8bf097.json`.
- Rebuilt native Jev corpus-v2 readiness passed 106/108 (98.15%), with one
  retained deadline failure and one invalid proposal; complete audit, zero
  unauthorized actions, p50/p95/p99 401.32/501.24/600.12 ms. Returned model was
  jev-1.13.0, maximum input 1,238 tokens, peak helper RSS 43,450,368 bytes.
  Report: `output/native-readiness/readiness-1172d19ddc55-d8b351a1.json`.
- Full current source suite passed all 168 tests. An isolated native Jev delay
  was rejected after expiry and the same helper recovered on the next window.
  Telemetry gap stayed below 1.10 s, heartbeat age below 183 ms, zero priority
  changes during the injected delay. Evidence:
  `output/native-jev-failures/0d089ab1f89d4c38ac96016c5a773994/`.
- Current native hung-helper and pipe-disconnection checks both passed. The
  hung helper produced deadline then helper-busy, latched Rule and was killed;
  pipe disconnection produced two helper-unavailable failures and latched Rule.
  Maximum sample gaps were 1.084/1.644 s; heartbeat ages 349/501 ms. Both
  sessions stopped cleanly, with any changed background restored to nice 0.
  These are owned-helper failures, not a host network outage. Reports:
  `output/native-jev-failures/15a1fe0dace94071a12f118ade84b578/` and
  `output/native-jev-failures/d872b4499eb5475c9fab749c021a34d5/`.
  Full JSON/CSV and request/acknowledgement audits passed in
  `output/native-jev-v4/`. The first session had one actual Jev lowering and
  one Rule restoration; the second had no model-originated priority changes.
- Current native Laya MPS readiness also failed: 88/108 within one second
  (81.48%), with 20 deadline failures retained, complete audit and zero
  unauthorized actions. Response/deadline-observation p50/p95/p99 were
  344.18/1,176.27/1,447.73 ms; timeout rows include cancellation overhead.
  Its cold-start field again describes the latest restart. Of accepted
  proposals, 87 were lower and one hold. No live Laya or five-arm comparison
  qualification is established. Report:
  `output/native-readiness/readiness-1731274857f5-ae1ce736.json`.
  Host memory pressure was observed (about 22 GB swap in use); causation has
  not been established. User was asked whether unused apps can be closed
  before further timing measurements. No unrelated applications were altered.
- After the user closed unused applications, a second native MPS run failed
  at 71/108 (65.74%), retaining 37 deadline failures. Exact frozen-corpus,
  unique-request, schema and readiness-recomputation audits passed; all 108
  rows were audited and unauthorized actions remained zero. Accepted proposals
  were 70 lower and one hold, maximum 742 tokens / 1,450 input bytes, peak
  helper RSS 3,014,033,408 bytes. Version-1 observed latency p50/p95/p99 was
  401.89/1,372.15/1,790.61 ms, including timeout cleanup overhead.
  Evidence and 30-second app/helper/host samples:
  `output/native-readiness-observations/9aa07c1ea0634694add5aaca978753b4/`.
  The host continued swapping during observation; this is context, not proof
  of the cause. The previous failed results were not replaced.
- Following a successful user-operated Jev connection test (which unloaded
  Laya), sequential source-runtime diagnostics used the same failed frozen
  contexts with 1/3/7 backgrounds, four repetitions and five-second dispatch
  spacing. MPS met one second for 8/12, with end-to-end p50/p95/p99
  301.46/1,469.33/1,469.33 ms; forward-pass p95 was 1,439.58 ms, encoding
  p95 7.21 ms per call. CPU with two threads met one second for 9/12,
  end-to-end p50/p95/p99 612.56/1,231.17/1,231.17 ms. Cold starts were
  9.75 s MPS and 8.21 s CPU. These are instrumented diagnostic observations,
  not 100-context readiness evidence; MPS forward synchronization changes
  timing. They issue no priority commands and authorize neither device.
  Evidence: `output/laya-stage-profiles/745cc6e915b544dc8d59c3ce072a8992/`
  and `output/laya-stage-profiles/83d58cb504ea4bafad3c214f6996bf3f/`.
- Readiness report version 2 preserves initial and subsequent cold starts,
  their percentiles and the context before each startup. Timeout detection
  and response-validation latency now exclude separately recorded cleanup
  time. The one-second gate and provider fingerprints are unchanged. Two
  regressions failed before this fix and pass after; the full suite passes
  170 tests. Historical version-1 reports retain their original semantics.
- The updated app is staged at `build/staged-desktop/Adaptive OS Controller.app`.
  Build and deep/strict signature verification passed; pinned checkpoint
  integrity, matching tested helper/lock and bundled licences were checked.
  `build_desktop.py --output-dir` allows this without moving or replacing the
  running app or earlier bundles. The current native process and its
  memory-only Jev key remain open in `dist`; the staged bundle has not been
  exercised through native UI. Native automation repeatedly fails before
  accessing any window with `failed to start Node runtime: No such file or
  directory (os error 2)`, including after resets and user-requested retries.
- After the user restarted the Codex/MCP session, CUA startup succeeded and
  native window reads, screenshots, tab changes and window resizing worked.
  The previous service had inherited a removed `CODEX_CLI_PATH`; the Node
  executable itself was present. This startup issue is resolved. Separate
  intermittent ScreenCaptureKit -3812/-3811, `cgWindowNotFound` and
  `noWindowsAvailable` errors remain during native input/capture. Rebinding
  restored reads, but Qt experiment selection is not yet dependable. The
  existing app and masked key were preserved; no new trial was started.
- Native playback → New playground verified the editor refresh. Current
  3/7-background UI checks, experiment-replacement editor verification,
  two 30-minute Laya soaks, native held-out calibration and the 50-trial
  comparison remain outstanding. Laya live remains gated; no performance
  advantage is claimed. No project sandbox or diagnostic helper is left running.
- Fresh native Auto: Jev demonstration completed in session
  `5955a6c5-9eb9-4d79-b2d3-38abb68b8f30`: one 16/20 ms periodic foreground
  and one Normal-importance CPU hashing background, both initially nice 0.
  Jev produced eight holds and one lower (907.92 ms), with a linked verified
  nice 0→10 acknowledgement. Exactly one Jev intervention, zero Rule
  interventions; disabling policy restored nice 0 and the sandbox stopped.
  Thirty-second raw-metric phases show foreground CPU 49.60→80.23→49.56%,
  background CPU 49.63→19.08→49.60%, and foreground misses
  99.93→11.07→99.93%. Background throughput fell about 61%; this is a visible
  CPU demonstration, not a passing performance gate or AI superiority result.
  Full JSON/CSV parity and audit passed; all evidence and the phase report are
  in `output/native-ai-demo/5955a6c5-9eb9-4d79-b2d3-38abb68b8f30/`.
- That run exposed another recovery defect: a returned transient unavailable
  response killed a healthy helper and manufactured a second failure on the
  next window. Two failing regressions established the behavior. Guard
  priority-v5 retains that helper; two actual failures still latch Rule.
  All 172 source tests pass. The rebuilt app passed deep/strict signing,
  checkpoint/helper/dependency-lock/licence verification and native launch;
  it is now at `dist/Adaptive OS Controller.app`, with the prior bundle saved
  as `dist/previous-0cf5633b.app`. Its new fingerprint requires fresh readiness.
  The masked key field is focused and user key entry is pending; no credential
  was read or persisted. See `docs/ai-mode-verification.md` for the exact demo
  configuration and evidence limits.
- Fresh native guard-v5 Jev readiness passed 107/108 (99.07%), retaining one
  invalid proposal and no timeouts. Exact frozen-corpus and report-recomputation
  audits passed; 36 cases each for one, three and seven backgrounds. Warm
  p50/p95/p99: 402.89/518.68/568.91 ms; cold start 154.10 ms. Evidence:
  `output/native-readiness-observations/6cf3782fead14492af4776d4f1e36d41/`.
- The rebuilt native app then completed session
  `ece22bfe-82d9-438b-8171-791e05dcce16` using the same 16/20 ms foreground
  and one Normal-importance background. All 44 proposals were accepted within
  one second: 41 holds, two lowers, one restore. Three verified model-originated
  priority changes, no Rule changes or fallbacks; final manual disable restored
  the last lowering and stop cleaned up. Foreground nice remained 0 and model
  acknowledgements obeyed cooldown. Thirty-second CPU phases were
  49.55/49.59 → 80.43/18.80 → 49.42/49.42%; foreground misses
  99.93 → 28.00 → 100.00%. The model's restoration reversed the improvement.
  Background throughput cost was ~63%; no performance gate or superiority
  claim follows. JSON/CSV parity, audit and raw phase reports passed in
  `output/native-experiments/b20b491f44f9470685cb3c5976759591/`.
  Its observer has zero experiment trials; per-trial totals in observation.json
  are not live intervention counts. Use audit.json/cpu-effect-report.json.
