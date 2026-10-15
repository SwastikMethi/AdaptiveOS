# Guest Linux remaining phases — 2026-10-12

CPU, separate memory protection/headroom controls, shared directional HTB, Manual,
Rule, VM-bound Jev readiness/shadow gating and the full-duration paired comparison
harness are implemented on UTM / Ubuntu ARM64 / KDE Plasma X11. Docker remains
the default with protocol v5. Implementation does not imply Jev qualification or
an application-performance improvement claim.

Final host regression: 365 tests passed. Guest-specific regression: 127 tests run,
125 passed and two platform skips; eight legacy metric tests also passed. The
documented guest launcher starts and stops cleanly. Jev desktop packaging now
includes its existing certifi dependency; three real guest requests returned valid
proposals within one second, which does not itself qualify live control.

The broad repository-suite attempt inside the guest did not pass: Docker-default
lifecycle tests require a daemon that is not installed there, and the mixed-Qt
run reported a teardown segmentation fault. Its log is retained. The guest-only
suite and actual guest launcher lifecycle passed separately; they do not turn
that broad attempt into a pass.

The live worker passed CPU/Memory/Network Favor, Restore, End and Rule decisions;
184 recorded messages retain JSON/CSV parity, and five historical M1 recordings
still load. Real partial kernel writes reconcile both resources without replacing
applications or network objects. Live growth raised only foreground headroom,
invalidated and rejected the old scoring profile, preserved protection, and
restored pre-run limits on End. Normal, controller-loss, service-restart and
service-crash proofs are retained separately from performance trials. Shared
network packet classification passed, and fresh aggregate-rate, child-share and
borrowing checks passed on the kernel-146 baseline. Fresh CPU and Network
calibration pass. Memory has stable headroom and observed reclaim, but insufficient
sustained pressure keeps its scored comparisons blocked. Network R9 completed
all 40 valid trials under `output/guest-remaining/network-final-20261012-r9/`
inside the VM. The earlier `tuned-runs/` comparison set is explicitly invalid due
to a changed Docker host state; no partial results are merged into its replacement.

Earlier CPU Jev readiness passed with 106/108 timely valid responses, but its live
shadow gate failed with 7/36. It proposed unavailable Favor actions 28 times and
missed one deadline. All requests were audited and all applied actions passed the
kernel guard. CPU Jev remains disabled; only Default/Manual/Rule comparisons are
eligible for that resource.

The CPU three-arm comparison is complete: 30/30 valid full-duration trials, with
stable identities, placement and memory settings. Manual's mean paired foreground
p95 improvement under contention was 9.0%; Rule's was -61.8%, dominated by one
193.47-ms trial. That adverse result remains included and its cause is not
isolated. Those CPU results bind their original frozen source revision. Network's
previous three-arm set stopped on trial 14 with a rejected observation; its exact
cause was not saved. That exposed an audit gap: rejected snapshots are now recorded
before validation, without allowing them into control. The incomplete sets and
unrun slots remain preserved. A new network calibration passed on the updated
source. A subsequent fixture failure exposed the test endpoint's idle timeout:
paced background activity could wait longer than its persistent-connection limit.
A real-socket regression reproduced that failure; the endpoint now retains idle
connections for the bounded fixture lifetime while client request timeouts stay
unchanged. Another calibration missed the upload saturation gate (18.77 Mbit/s);
that failure is retained. A later set stopped at trial 22 when preflight tried to
inspect an SSH session that had just ended. Plasma and both applications were
unchanged. Preflight now skips only that exact vanished-session error and still
requires a verified active local Plasma X11 session. A regression and 80 live
observations with overlapping SSH churn passed after the service update. The r4
set completed all 30 three-arm and 40 four-arm trials and the Jev desktop worker
passed with 33 messages in matching JSON/CSV exports. Its final public DNS check
then failed: host systemd-resolved had replaced its symlink target, losing the
task's file-only resolver bind. The exact failure time is unknown, so r4 is
excluded from final acceptance while all raw measurements remain retained.

Tasks now have a private resolver directory, and snapshots verify the actual
mount and managed DNS content. A real systemd probe reproduced the old failure
on five atomic replacements and preserved the new bind through all five. Both
existing application namespaces were repaired under the journal lock without
closing their windows. DNS survived resolved/service restarts and a subsequent
forced service crash with active Network control. Restored settings, original
identities, unshaped egresses, public HTTPS and a checksum download passed.
The r5 replacement retained 12 valid trials, failed trial 13 and 17 unrun slots
when the user closed Firefox. The user confirmed that action; DNS was healthy
in its last valid snapshot and no OOM increments occurred. Geany survived, the
closed profile remained intact, and unused namespace cleanup completed. A fresh
managed Firefox launch passed DNS/HTTPS/checksum checks on the new resolver mount.
The complete three-arm replacement is retained under `output/guest-remaining/network-final-20261011-r6/`.
R6 completed all 30 three-arm trials. Independent artifact checks passed for the
complete schedule, full windows, one calibration, identities, placement and fixed
CPU/memory settings. Network contention mean paired foreground p95 improvement
was 16.5% for Manual and 13.0% for Rule; worst background throughput loss was 0.9%
and 1.7%, respectively. Fresh Network Jev readiness passed 108/108 and shadow
passed 35/36 (one deadline fallback), with complete audits, zero unauthorized
actions and all 36 kernel guards passing. Its four-arm set stopped at trial 14
with 13 valid trials and 26 unrun slots. A delayed snapshot plus model processing
and dispatch exceeded the service's unchanged three-second observation lifetime.
The service correctly rejected the command before writing; the controller had
treated that normal rejection as fatal.

The controller now records a held fallback, refuses to replay that observation,
and resumes only with a fresh binding. Other write/transport errors still surface.
Regression tests passed RED→GREEN, and deliberately expired real CPU and Network
commands passed no-write/no-replay/fresh-command/restoration/export checks. Both
application identities and DNS survived. The completed r6 three-arm set remains
historical evidence for its own revision. The r7 run recalibrated and requalified
before a fresh 40-trial Default/Manual/Rule/Jev set. Its calibration passed;
fresh readiness passed107/108 and shadow36/36, with complete audits, zero
unauthorized actions and all kernel guards passing. The user requested a pause
before a temporary network outage: 10 trials completed, trial 11 was interrupted,
and 29 were unrun. Settings restored and both applications remained open. On
explicit resume, restoration, DNS, HTTPS and checksum checks passed. R8 passed
fresh readiness (105/108) and shadow (36/36), then retained 15 valid trials,
failed trial 16 and 24 unrun slots. A telemetry snapshot exhausted three busy
replies from the service's 50-ms writer-lock wait. The original lock holder was
not recorded. A real-socket test reproduced rejection during a healthy 600-ms
writer; a bounded one-second acquisition wait now passes that test while
heartbeats stay independent, rejected writes stay unapplied, and all freshness
guards remain. The complete host/guest suites and 80 live snapshots passed;
applications, restored settings, DNS, HTTPS and checksum downloads survived the
service update. R9 completed fresh calibration, readiness (108/108 timely valid),
shadow (36/36 timely valid) and all 40 trials. All 144 qualification requests were
audited, with zero unauthorized actions and all 36 shadow kernel guards passing.
Independent checks confirm the full schedule, 180.076–181.389-second windows,
unchanged identities/placement and fixed CPU/memory settings.
No partial four-arm sets are combined. These are synthetic probe results, not
desktop interaction latency.
Previous readiness and shadow results do not qualify the new source; R9 has its
own passing Network qualification. Memory remains gated. The
first 359-test host run also hit a relay thread-cleanup failure; its isolated repeat
and complete suite repeat passed without relay changes. The failed log remains.

R9 Jev improved mean paired foreground p95 by 8.0% under light load and 7.2%
under contention, with worst background losses of 30.2% and 3.7%. It outperformed
Rule's mean p95 but did not meet the required 20% improvement against Default:
**the AI usefulness gate failed**. Manual improved p95 by 13.1%/13.3%; Rule by
-1.9%/-3.2%. All adverse outcomes remain included. The full per-task costs,
deadline misses, pressure, fallback and overhead are in the companion metrics
report documented in `docs/guest-linux.md`.

The final actual Jev desktop worker passed two audited decisions, restoration,
history/playback, shutdown and 32-message JSON/CSV parity. Final state checks,
repeated after restoring Docker and both guest update timers, passed application
identity/profile/namespace preservation, weights 100, fixed placement, restored
memory settings with the retained 4-GiB safety cap, unshaped egresses, DNS, HTTPS
and checksum downloads. The original RAG Postgres and Qdrant containers are running
and healthy. Network qualification still matched after cleanup; its 24-hour and
baseline/source expiry rules remain. The aggregate local report is
`.implementation/guest-remaining/verification.json`; raw recordings remain in the VM.

A fresh scoped source review found four Important issues, all fixed with
RED→GREEN regressions: durable memory-profile invalidation, live settings-drift
rejection, failed-report/cleanup propagation, and qualification binding to the
installed service and complete controller dependencies. One Minor is deferred:
controller overhead excludes the model helper and maximum RSS is a process
lifetime peak. The guide states this measurement limit.

Repeated manual saved/unsaved-text fixtures were skipped at the user's request.
UTM still drops automated keystrokes, so native button activation is not inferred
from successful worker tests. A new kernel-146 powered-off copy has matching
checksums; it has not been boot-tested. No AI usefulness result is claimed.

The following entries describe earlier milestone evidence, not the current
implementation scope.

# Guest Linux CPU weights — 2026-10-10

Milestone 2 implements bounded manual CPU control on the existing UTM/Plasma X11
backend. The foreground and importance configuration are fixed during a run;
background selection uses importance, eligible weight and stable task ID.
Background weights follow 100→50→25, with CPUs 1–3 unchanged. Server-issued
observations enforce freshness, ten valid seconds, five-second decision spacing
and a thirty-second cooldown. Journalled writes require kernel read-back;
restoration bypasses decision gates. Docker remains the separate default backend.

The **CPU mechanism gate passed**. Across four steady 30-second contention
windows, the foreground received 49.80%, 66.68%, 79.95% and restored 50.00% of
task CPU time. All windows used approximately 2.99 cores. Duplicate commands,
consumed observations and stale generations were rejected. Normal end,
controller loss, service restart and service crash restored weight 25 to 100,
preserving task identities, placement, profiles and namespaces within each run.
The saved fixture checksum also survived the first three recovery scenarios.
These are mechanism checks, not scored application-performance comparisons.

Source regression: 308 tests passed. Guest regression: 70 tests run, 2 skipped,
the remainder passed. A fresh source review found no blocking or minor findings.
The live controller worker exercised both reductions, Restore and End; its full
JSON/CSV exports match. Five historical M1 sessions still play back. Native Qt
rendered the controls and began a CPU run through keyboard input, reached
readiness, and recorded a clean launcher shutdown; its 67 messages also export
with full parity. Native Favor/Restore button activation remains unconfirmed
through UTM's unreliable automated input. The fresh Docker v5 live regression
passed and removed only its own container and volume.

A login-screen reboot invalidated the first service-crash attempt. A subsequent
scheduled Ubuntu upgrade restarted a new editor and caused identity rejection;
those logs are retained. The upgrade finished, the blank replaced editor was
closed normally, and the service-crash repeat passed with fresh tasks. Package
timers were paused only during that verification and restored afterward. The user
requested skipping repeated manual saved/unsaved-text verification; that repeat
is not claimed. The original M1 UI-state evidence remains historical evidence.

Evidence: `.implementation/guest-cpu/verification.json` and the
[guest guide](guest-linux.md). M3 memory implementation may proceed. Before
scored experiments, finish any pending OS restart, refresh the baseline inventory
and cold copy, and calibrate again with updates outside measurement windows.
Automatic Rule/Jev control, dynamic memory, HTB, readiness and scored comparisons
remain unimplemented/unqualified in the guest. No AI usefulness result is claimed.

# Guest Linux feasibility — 2026-10-10 (Milestone 1)

Milestone 1 source infrastructure is present: a distinct authenticated guest
protocol, independently owned application units and profiles, an unshaped routed
gateway, durable CPU-probe restoration, guest Qt entry point and recordings.
Host tests are not VM evidence. Storage and UTM prerequisites are now met. The
existing UTM VM now runs KDE Plasma 5.27.12 on X11 through SDDM, with its
SPICE/QEMU tools retained. The user confirmed smooth Firefox/Geany input,
scrolling, resizing and clipboard use. Desktop DNS, HTTPS and the checksum
download passed. A powered-off Plasma bundle copy has matching SHA-256 values
for all five files; it has not been boot-tested. Source regression: 285 tests
passed. Fusion setup is cancelled. Earlier Xfce evidence is historical only.

The fresh Plasma **Milestone 1 feasibility gate passed**: two Firefox and two
Geany instances had independent profiles, cgroups and network namespaces;
descendant and browser networking-process audits passed. All four task paths
passed DNS, public HTTPS and checksum downloads before and after recovery.
Packet captures and conntrack confirm the routed gateway and root NAT ordering,
without shaping. CPU 100→90→100 restoration passed normal end, controller loss,
service restart, service crash, and actual Qt controller shutdown/crash.
Firefox page state and managed Geany saved/unsaved state survived the relevant
recovery checks. A mistaken ordinary-Geany fixture is retained as failed evidence;
the corrected managed fixture passed the repeated four-scenario sequence.

Qt completed/interrupted playback and full JSON/CSV parity passed. The fresh
Docker live regression also passed; its test container/volume were removed.
Docker Desktop and the original RAG Playground Postgres/Qdrant containers are
restored and healthy. Evidence and the next gate are in
[the guest guide](guest-linux.md) and `.implementation/utm-plasma/verification.json`.
Milestone 2 CPU implementation may proceed. Memory controls, HTB, Jev readiness,
performance calibration and comparisons remain unimplemented/unqualified in the
guest. A cold boot soft-lockup under host memory pressure is retained; the retry
booted in 6.336 seconds after Docker was stopped. No performance result is claimed.

# Resource modes — 2026-10-08

Protocol v5, real managed cache budgets, loopback bandwidth shares, resource-specific
Rule/model guards, allocation/effect timelines, charts, playback and paired experiment
reporting are implemented. Resource controls leave CPU priorities fixed. See
[the resource guide](resource-modes.md) for scope, commands and retained evidence.

Latest full paced source Laya results failed on both MPS (82/108, 75.9%) and CPU
(69/108, 63.9%). Connection success cannot grant readiness; each resource has an
independent fingerprint and the newest full failure supersedes an older pass.
Laya remains shadow-only. No AI usefulness improvement is claimed.

Real Docker allocation/share, identity, duplicate, restoration and export checks
passed with 1/3/7 backgrounds in each new mode. Native Memory and Network Rule
checks passed, including acknowledgement markers, measured effects, restoration
and recording playback. Both resource hosts passed force-kill / lease cleanup and
interrupted-recording recovery. Both final-image calibrations passed all six
trials with exact raw recomputation; final packaging and review evidence is
retained in the execution ledger. The final source suite passes 217 tests;
the four review findings have regression tests and fixes. The reviewed build at
`build/resource-desktop-reviewed/Adaptive OS Controller.app` passes offline
relocation, checkpoint-integrity and clean failure checks. Native UI automation
remains intermittent; native source Qt and bundled-helper evidence are distinct.

Native Jev full readiness failed independently for Memory (91/108, 84.3%) and
Network (100/108, 92.6%), each with complete audit and zero unauthorized actions.
The user entered its key privately in the test app. Both live resource comparisons
remain locked by the unchanged ≥95% gate. The 40-trial comparisons per resource
have not run, and neither provider has demonstrated a resource scheduling advantage.

# Decision activity UI — 2026-10-08

The Live playground now separates workload measurements from Decision activity.
Each decision has a timestamp, provider, proposal, confirmed or blocked outcome,
and response latency. Expandable details retain context, guard evidence, model
identity, probabilities and acknowledgement links. The panel separately shows
the effective controller, expected next call/check and priority cooldown;
failed dispatch is never labelled as fast model inference. Confirmed changes
are linked to CPU-chart markers. Recordings have stable 100-decision pages,
including legacy events, while full exports retain all events.

Verification: 192 source tests pass. Computer use verified native replay with
one, three and seven backgrounds, keyboard expansion, chart selection and the
resizable panel. The explicitly simulated UI fixtures exercise hold, confirmed
lower/restore, cooldown blocking and Rule fallback at 1280×900 and 1000×740.
Native verification also exposed and fixed a misleading replay summary and
Cocoa chart clipping; the chart-containment regression passes with the native
macOS Qt style. These are UI checks, not new AI-performance evidence. Existing
model readiness and comparison work below remain separate.

The signed rebuild is installed at `dist/Adaptive OS Controller.app`; the prior
bundle is preserved as `dist/previous-ui-ad0de89a.app`. A final native Rule
session recorded 23 decision cycles, two confirmed changes (nice 0→10→0), and
an acknowledged shutdown with no remaining project sandbox. The app marks a
quit-ended session as interrupted; its records were retained. Live countdowns
and compact chart containment passed. No Jev/Laya calls were made in this UI
verification. The app is reopened using its normal recording directory;
restarting cleared the memory-only Jev key.

# Decision-provider integration — 2026-10-06

The separate Jev/Laya decision providers, guarded shared controller, shadow and
readiness gates, raw-counter reporting and bundled-runtime build are implemented.
See the [integration ledger](jev-laya-integration.md) for current verification
and remaining acceptance work. The sections below preserve historical desktop
and legacy CLI status; their test counts and model availability are dated.

Evidence on that date: 172 source tests passed. Guard revision priority-v5 also keeps
the helper after a returned transient provider-unavailable error, preventing an
artificial second failure. Isolated late-reply recovery, the one-second deadline,
bounded queue and two-failure Rule latch remain intact. The rebuilt native app
is installed at `dist/Adaptive OS Controller.app`. Fresh native Jev readiness
passed 107/108 (99.07%), with zero timeouts and one retained invalid response.

A fresh native Jev run verified an actual model proposal → guarded priority
command → Linux acknowledgement → CPU change → manual restoration → clean stop.
With a 16/20 ms foreground and one CPU hashing background, 30-second averages
changed from 49.55%/49.59% foreground/background CPU to 80.43%/18.80%, returning
to 49.42%/49.42% after Jev's own restoration. The guard-v5 run recorded two Jev
lowerings and one Jev restoration, zero Rule interventions and zero fallbacks.
Disabling policy restored the final lowering. This diagnostic costs about 63% background
throughput; it is not a passing performance benchmark. See the
[reproduction steps and evidence](ai-mode-verification.md).

Earlier revision evidence is retained: offline relocated helper operation,
108/108 native readiness for each provider, Jev shadow and helper fault checks,
seven deterministic lowerings/restorations, and offscreen 6/20 ms calibration.
A subsequent three-background Laya shadow run safely fell back after a timeout.
Identical-input replay also exceeded the deadline, exposing the gap between
continuous readiness requests and actual five-second execution spacing.

The rebuilt app passes deep/strict signing. Current paced Jev readiness passed
106/108 contexts (98.15%), retaining one timeout and one invalid response.
Current offline Laya readiness failed at 97/108 (89.81%); its packaging checks
passed, including empty external cache, immutable signed bundle and clean failure
for corrupt/missing weights and missing credentials. Laya live readiness is not
established by that run. Current native Laya MPS readiness also failed at 88/108
(81.48%); the repeat after the user closed unused applications failed at 71/108
(65.74%). Instrumented MPS and explicit CPU diagnostics also exceeded the
deadline. The earlier failures remain preserved and no threshold was relaxed.

Readiness report v2 now retains every cold start and separates deadline
detection/validation latency from cleanup. The new signed bundle includes it
and guard priority-v5. Checkpoint/helper/lock/licence verification passed; the
previous app is preserved at `dist/previous-0cf5633b.app`. Native launch,
configuration editing, Providers navigation, readiness, model-originated
lower/restore, manual disable/restoration and clean stop passed. The key remains
memory-only in the current app. JSON/CSV parity and complete event audits passed.

Acceptance remains in progress: fresh native Laya readiness, native workload/shadow checks, awake
30-minute visible/minimized runs, native held-out calibration, the 50-trial
five-arm comparison, and experiment-replacement verification of the editor fix.
Playback → New playground now passes native verification. Restarting the
Codex/MCP session fixed native automation startup: window reads, screenshots
and tab navigation now work. Intermittent ScreenCaptureKit capture failures and
unreliable Qt dropdown selection still prevent dependable experiment setup.
The manually started repeat validation's saved report and resource observation
are complete and audited. The one-background live demonstration is complete,
including JSON/CSV parity and raw-metric recomputation; it exposed and led to
the priority-v5 recovery fix. No AI performance advantage over Rule is claimed.

# Desktop app update — 2026-09-30

The native single-container playground is implemented on `feat/adaptive-os-mvp`.
It runs 2–8 competing workload processes in one Linux sandbox, with native Qt
controls, live telemetry, saved recordings and JSON/CSV export. The local macOS
bundle is `dist/Adaptive OS Controller.app`; Docker Desktop must be running.

See [desktop verification](desktop-verification.md) for the 55-test suite,
eight-workload integration checks, ten-minute soak, native walkthrough, review
fixes and remaining limitations. The legacy calibration gate below does not
block desktop monitoring. No automatic policy/model controller is claimed.

# Legacy benchmark implementation status — 2026-09-29

Implemented on `feat/adaptive-os-mvp`: plan Tasks 1–6 (M0 and the M1 baseline/calibration tooling). M0 passed on this machine. The M1 **experiment gate has not passed reproducibly**, so Tasks 7–11 remain pending. This is not a policy-comparison result.

## Evidence

- Python 3.11 virtual environment and installed `adaptive-os` CLI; no third-party runtime dependency.
- Docker Desktop 4.53.0, Engine 29.0.1, Linux 6.12.54-linuxkit, cgroup v2, ARM64, 10 VM CPUs, approximately 8 GB VM memory.
- Selected benchmark CPU: 0, the first allowed CPU. The reviewed runner pins instrumentation to spare CPU 1.
- Real M0 proof: configured shares `1024 → 128 → 1024`; effective `cpu.weight` `100 → 21 → 100`. All owned probe containers were removed.
- 34 unit/regression tests passed, including exact ownership, failed restoration, interrupted preflight, stale/buffered data, abnormal workload exit and follower shutdown failure.
- Workload image builds and both Docker workload smoke tests passed. Raw deadline records, throughput/CPU summaries, decisions and lifecycle events persist in SQLite.
- Independent M0/M1 review found four important issues. All four received failing regressions and fixes; no critical or deferred minor findings remain from that review.

The corrected workload initially selected 2 ms light and 8 ms contention budgets at a 20 ms period. A post-review repeat measured 8 ms paired misses at exactly 2%, which fails the strict `>2%` contention criterion. The next bounded sweep (2/10 ms) measured solo misses of 0.80% and 1.07%, exceeding the required 0.5%. The observed variability prevents freezing reproducible profiles. Thresholds were not relaxed, and all unsuccessful attempts remain available.

The [latest sweep](../runs/calibration-20260929T170912851410Z.json) completed normally: 2 ms paired misses were 0.53%; 10 ms paired misses were 97.20% with 18.59 ms p95 lateness. Both candidates failed solo qualification. All 36 calibration trials finished and retained valid measurement records (measurement validity does not mean a profile met its acceptance thresholds). SQLite integrity passed, and the final project-container query was empty.

Current image: `sha256:6c0b40abae7f99b60b895c30521011f614b8ba0eea2c485ec498732331bc8d47`. Older image results are historical calibration evidence, not evidence for this image. Host bytecode is excluded from builds.

Local evidence is ignored by Git:

- `runs/preflight-*.json`: environment, every command outcome, weight proof and cleanup.
- `runs/calibration-*.json`: each bounded sweep, candidate budgets and its outcome.
- `runs/profiles.json`: latest calibration status; only usable when `valid` is true.
- `runs/runs.sqlite3`: raw records for every trial, including failed/invalid trials.

## Continue

The remaining prerequisite is a repeatable light and contention calibration. A longer observation on a quiet host is a useful next experiment; the CLI exposes both duration and budget controls:

```sh
.venv/bin/adaptive-os calibrate --measurement-sec 60 --budgets-ms 2 8 10 --cpu auto --runs-dir runs
```

This command is a suggested next experiment, **not a completed verification**. A different benchmark CPU must be explicitly selected with `--cpu`; moving to another machine requires fresh preflight and calibration. Do not loosen acceptance criteria or reuse a successful historical sweep to bypass the latest failed gate.

Once M1 passes, resume Task 7 (deterministic policy), then Task 8 (controller), Task 9 (repeated comparisons/report), and finally optional Laya Tasks 10–11. No model package has been installed.

## Decisions recorded during execution

1. Used this new project directory and a feature branch; no existing checkout needed isolation. Cost if wrong: move the branch to a worktree.
2. Used `.venv` Python 3.11 because host `python3` is 3.14. Cost if wrong: recreate the environment.
3. Resolved future policy contradictions in favor of two invalid windows and holding the current mode during cooldown. Cost if wrong: adjust policy tests before implementation/experiments.
4. Future model inference needs a bounded worker process, not the plan's uncancellable thread timeout. Cost if wrong: small pre-measurement process startup overhead.
5. Labeled every probe and made cleanup without an exact run ID list-only. Cost if wrong: one extra recovery argument.
6. Recorded selected Docker metadata fields rather than proxy/environment configuration. Cost if wrong: add a needed non-secret field.
7. Deferred the optional model dependency until M4. Cost if wrong: add the extra later.
8. Used separate container create/start, full IDs, UUID run IDs and combined ownership inspection. Cost if wrong: one additional launch command.
9. Skipped no-op restoration when shares are already 1024. Cost if wrong: an existing effective-weight inconsistency needs preflight verification.
10. Counted overdue periods as skipped/missed ticks. Cost if wrong: the synthetic admission policy differs from a real application; that limitation is documented.
11. Added a common VM start timestamp and relative CPU counters. Cost if wrong: startup lead time may need tuning.
12. Used `WindowAccumulator.build_window` as the single aggregation entry point. Cost if wrong: adjust caller signatures.
13. Prorated one-second CPU/work counters at window boundaries while retaining raw values. Cost if wrong: boundary interpolation error, reduced by longer trials.
14. Used a sleeping, labeled, spare-CPU probe for clock/PSI reads; single-CPU environments explicitly record shared instrumentation. Cost if wrong: small probe overhead remains in VM-wide PSI.
15. Required every measurement window to be valid for trial acceptance. Cost if wrong: conservative exclusions.
16. Exposed bounded calibration budgets and tested lighter loads before freezing profiles, retaining original failures. Cost if wrong: extra calibration time; thresholds stay unchanged.
17. Limited independent review to implemented M0/M1. Cost if wrong: later tasks still require their own verification.
18. Treated profile feasibility as an experimental result, not a review verdict. Cost if wrong: time spent on an inconclusive experiment.
19. Documented Docker/daemon CPU overhead as unmeasured, while retaining Python CPU time and command durations. Cost if wrong: apparent benefits may include instrumentation cost.
20. Kept prior-image evidence separate from current-image qualification. Cost if wrong: additional calibration time.

The full task ledger remains in `.superpowers/sdd/2026-09-29-adaptive-os-controller-mvp/progress.md` for resumption. It is retained because the overall implementation plan is unfinished.
