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
