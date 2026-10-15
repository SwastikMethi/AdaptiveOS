# CPU, Memory and Network modes

The desktop playground now controls one resource at a time. Select **CPU**,
**Memory**, or **Network** before starting a sandbox. The selector stays locked
through the run. Memory and Network default to one explicitly marked foreground
and three backgrounds. Choose 1–7 backgrounds before starting. CPU retains the
existing periodic/hash workloads and acknowledged nice-priority controls.

All controls affect owned synthetic workloads inside the same sandbox. These are
application-managed cache capacities and relay shares, not kernel per-process
memory limits, physical-interface shaping, or native Mac application controls.
The read-only, non-root, 1 GiB container and ten-second host heartbeat lease are
unchanged. External networking stays disabled. Docker's `none` driver retains
[the loopback interface](https://docs.docker.com/engine/network/drivers/none/).

## Run a demonstration

1. Select Memory or Network and start the sandbox, then **Start all**.
2. Keep exactly one task marked **Foreground**. Set background importance before
   enabling automatic control. CPU priorities remain fixed at nice 0.
3. Select **Auto: Rule** and enable the policy, or use **Favor foreground** and
   **Restore allocation** manually. Allocation charts show confirmed amounts.
4. Expand a Decision activity entry to inspect resource, requested and confirmed
   changes, guard result, call timing and the first full measurement window after
   acknowledgement. Measured effects are observational, not causal estimates.
5. Disable the policy to restore captured budgets. Stop the sandbox to save the
   recording. Recordings retain resource metadata; old CPU recordings still open.

Manual controls also use the bounded, identity-checked transaction path. Automatic
decisions run every five seconds, require ten valid observation seconds, allow
one outstanding request and enforce a thirty-second cooldown after acknowledgement.
The model deadline remains one second. Invalid, late or unavailable model replies
use Rule; two consecutive failures latch Rule until explicitly re-enabled.
Shadow modes record proposals without sending allocation or priority commands.

## Memory

Each cache uses anonymous mappings in releasable 16 MiB chunks. Pages are committed
on allocation; a fixed seeded request stream and controlled 2 ms miss-refill cost
make capacity affect actual memory and cache behavior. Entries are 64 KiB and the
cache uses LRU eviction. The default working set is 144 MiB per 128 MiB cache.

The managed pool is at most 512 MiB, with a 16 MiB minimum per workload. The default
four caches start at 128 MiB each. Favor transfers 16 MiB from a background to the
foreground. Donors are chosen by Low → Normal → High importance, then largest
available capacity, then stable workload ID. For example, one background changes
128→112 MiB and the foreground changes 128→144 MiB.

The donor releases and acknowledges its memory before recipient allocation.
Recipient failure rolls the donor back. Missing acknowledgement, a failed rollback,
or a mismatched identity disables control and cleans up the owned sandbox; the
pool cannot temporarily grow past its limit. Restoration prioritizes High
importance and reverses each background's intervention history. Configured
capacity, confirmed capacity, RSS, hits/misses and completed requests are separate
metrics. RSS includes interpreter overhead and is not the allocation budget.

Initial cache startup has a 30-second allocation-confirmation timeout because it
commits all pages. This does not alter the one-second model deadline, three-second
per-step resize timeout, or ten-second heartbeat lease. Heartbeats and telemetry
remain on their independent supervisor/worker paths.

## Network

Real framed TCP transfers run over loopback through a dedicated relay thread.
The default link is 20 Mbit/s. A token bucket limits total bytes with at most 10 ms
idle credit; weighted scheduling distributes traffic between active flows and
lets them borrow unused capacity. Each flow has one outstanding application
record, capped at 256 KiB, with bounded socket buffers and backpressure. Telemetry
and host heartbeat handling do not wait for a transfer to complete.

The foreground requests 64 KiB responses every 100 ms with a 100 ms completion
deadline. Backgrounds continuously transfer 256 KiB records. Favor transfers five
percentage points to the foreground. Every flow keeps at least 5%; foreground
share is capped at 80%. Both share changes commit together under the relay lock.
The protocol binds the actual configured foreground and both process identities.

Charts/details show delivered Mbit/s, assigned shares, queue delay, completion
latency, deadline misses, completed application records and failed transfers.
Application records are not TCP packets. The AI changes shares on five-second
windows; the relay schedules continuously.

## Providers and qualification

Connection, helper-loaded state, and full readiness are separate. Unloading a
helper updates the UI immediately. Only the latest full result for the exact
provider/resource/prompt/guard/device/runtime fingerprint can qualify live
control; a later failed full run supersedes a previous pass. A one-call connection
test cannot grant readiness. Memory and Network use Hold / Favor foreground /
Restore; deterministic code chooses the target and amount.

A supervised Memory/Network playground demonstration can be launched explicitly
with `--allow-unqualified-jev-demo`. This process-only option enables unqualified
live Jev control and displays an experimental-demo notice in the full app view.
It does not mark readiness as passed or qualify experiments, CPU control or Laya.
The one-second response deadline, five-second cadence, bounded allocation guards,
acknowledgements, Rule fallback and restoration still apply. Recordings retain
`readiness_passed: false` and `unqualified_demo: true` for such live runs. A
successful demonstration proves the recorded intervention worked; it does not
establish reliability or scheduling improvement.

Laya remains **Shadow only—latency gate failed**. The new 108-context, five-second
source-runtime revalidations with two CPU threads, eager inference and warm-up at
1/3/7 backgrounds produced:

| Device | Timely valid responses | Result |
|---|---:|---|
| MPS | 82/108 (75.9%) | Failed ≥95% gate |
| CPU | 69/108 (63.9%) | Failed ≥95% gate |

Both reports retain every response/failure, complete audit and zero unauthorized
actions. They do not qualify the new resources or the native packaged runtime.
Live Laya additionally requires matching native shadow and visible/minimized soak
evidence. Diagnostic profiles separately record load, warm-up, idle intervals,
encoding, forward inference, validation, memory and host swap observations.
Memory pressure has not been established as the cause of latency variability.

Evidence is retained in:

- `output/resource-readiness-mps/readiness-831e10586086-aaa36882.json`
- `output/resource-readiness-cpu/readiness-eb2d799c5d0c-32770e97.json`
- `output/laya-stage-profiles/c49ae464d43d4819a874f96ce86a6346/`
- `output/laya-stage-profiles/457d8e2350f847f29353528b6b59831d/`

Native Jev validation used a key entered privately in the test app. Both new
resources failed their independent, full 108-context runs at the actual
five-second cadence:

| Resource | Timely valid responses | Deadline failures | Other invalid replies |
|---|---:|---:|---:|
| Memory | 91/108 (84.3%) | 16 | 1 |
| Network | 100/108 (92.6%) | 7 | 1 |

Both retain complete audits and zero unauthorized actions. Neither reaches 95%,
so live Jev control and its resource comparisons remain locked. The key was not
read or persisted by the verification scripts. Reports:

- `output/resource-bundle-native/model-readiness/readiness-c5e0d65c6ec5-22352fde.json`
- `output/resource-bundle-native/model-readiness/readiness-4e444ecaf0a4-d5eb687e.json`

## Experiments

Select **Resource calibration** in Experiments first. It runs three light and
three contention trials, each with 30 seconds warm-up and 30 seconds measurement.
Memory light working sets fit the initial cache; contention adds 16 MiB. Network
light uses 16 KiB foreground responses; contention starts at 64 KiB. An initial
32 KiB light calibration exceeded the miss threshold and is retained. Qualification
requires all six valid trials, light misses ≤2%, and contention misses >2%.
The exact workload configuration, importance, link, image and CPU placement are
frozen in the report and the current app session. A fresh app session recalibrates.

Final-image calibration passed for both modes, with six valid trials each and
exact raw recomputation. Memory light miss rates were 0/0/0%; contention was
10.9194/10.9081/10.8301%. Network light was 0.6667/0/0%; contention was 100% in
each trial. These establish workload separation, not a controller benefit.

**Resource comparison** requires this frozen configuration and exact-resource Jev
readiness plus a key supplied in Providers or via `--jev-env-file .env` (kept in
memory by the app and excluded from recordings). It runs five randomized paired blocks for each
profile: Default, fixed Manual, Rule, and Jev with fallback, totaling 40 trials per
resource. Every trial has 30 seconds warm-up and 180 seconds measurement. Fallback
trials remain included. Laya is excluded while unqualified.

Memory's primary metric is foreground cache-miss rate; Network's is foreground
deadline-miss rate. Reports include latency, every background's throughput cost,
fallback proportion and controller/helper overhead. A usefulness claim requires
complete paired evidence, improvement over Rule, and the existing ≥20% foreground
improvement / ≤40% aggregate background loss gate against Default. The 40-trial
comparisons have not run because both Jev resource readiness gates failed. No AI
usefulness result is claimed. Future comparisons must retain fallback trials and
use freshly qualified providers with the frozen workload configuration.

Full raw JSON/CSV exports retain identical payloads. Recompute measurements with
`scripts/recompute_desktop_report.py`; recomputing raw data alone does not grant
calibration or readiness authority.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/verify_resources.py --mode memory --backgrounds 7 --lease
.venv/bin/python scripts/verify_resources.py --mode network --backgrounds 7
.venv/bin/python scripts/verify_resources.py --mode memory --fault allocation_failure
.venv/bin/python scripts/verify_resources.py --mode memory --fault missing_ack
.venv/bin/python scripts/verify_desktop_crash.py --resource memory
.venv/bin/python scripts/verify_desktop_crash.py --resource network
.venv/bin/python scripts/verify_native_resources.py --mode memory --calibration
.venv/bin/python scripts/verify_native_resources.py --mode network --calibration
.venv/bin/python scripts/verify_model_bundle.py --bundle 'build/resource-desktop-reviewed/Adaptive OS Controller.app' --resource memory --packaging-only
```

Real Docker checks retain recordings under `output/resource-verification/` and
native Qt checks under `output/native-resource-verification/`. They verify changed
allocations, restoration, stale identity/duplicate commands, fixed CPU priority,
process exit, isolation and export parity. Failure tests deliberately affect only
their own labeled container/processes. Failed attempts remain recorded.

The final source suite passes **217 tests**. Real Docker checks passed with 1, 3
and 7 backgrounds in both modes, including fixed CPU priorities and sandbox
isolation. Actual allocation failure rolled the donor back; an unresponsive
recipient triggered timeout cleanup. Native Qt checks verified acknowledgement
markers, measured effects, restoration, playback, and stopping during a pending
manual allocation. Both resource hosts passed force-kill / lease cleanup and
interrupted-recording recovery.

The reviewed app is staged at
`build/resource-desktop-reviewed/Adaptive OS Controller.app`. Its relocated copy
launched with an isolated recording directory. Offline bundled-helper verification
passed with external networking denied, no activated virtual environment, no
external model cache, and an unchanged signed bundle. Missing/corrupt checkpoint
and missing Jev-key checks failed cleanly. The one-call packaging check grants no
readiness. Native UI automation remains intermittent; source Qt integration
checks and bundled-helper checks are recorded separately.

Selected final evidence:

| Check | Report |
|---|---|
| Memory native Rule/manual stop and restoration | `output/native-resource-verification/b35edb790978464298cd97597d83779f/report.json` |
| Network native Rule/manual stop and restoration | `output/native-resource-verification/89c86e5876f74e56a5197e8760a79fd0/report.json` |
| Memory calibrated 0/16 MiB extra working sets, raw recomputation | `output/native-resource-verification/9a8446cfd50c4efc8d36d7b7d165db05/report.json` |
| Network calibrated 16/64 KiB profiles, raw recomputation | `output/native-resource-verification/83a75cc4db4c4a3e9d607a301bb72f02/report.json` |
| Partial memory transaction rollback | `output/resource-verification/39487100-43ab-4a22-bdf7-e8f1b5f12aff/report.json` |
| Missing memory acknowledgement cleanup | `output/resource-verification/eb744da6-bf4b-47cd-888e-a5e7d22b73e2/report.json` |
| Offline relocation and bundle failure checks | `output/bundle-acceptance/e987c6b508cb403f8624bd1ac884ac0c.json` |

The execution ledger at `.implementation/resource-modes-plan.md` retains the full
verification history, failed attempts and unrun qualification gates. The final
independent review found four issues; all received failing regressions and fixes.
The existing feature checkout and its prior uncommitted changes are preserved.
Final cleanup checks found no remaining project-labeled containers or volumes.
