# Native AI CPU demonstration — 2026-10-06

Jev made a real, acknowledged priority change in the native app, and the CPU
measurements changed with it. Laya has not passed the current one-second
readiness gate, so its live CPU-control path is not verified. This is a
diagnostic demonstration, not completion of the full integration acceptance
matrix or evidence that AI outperforms Rule.

## Configuration to reproduce

Open `dist/Adaptive OS Controller.app` with Docker Desktop running.

1. In **Providers**, enter the Jev key in the masked, memory-only field, test
   the connection, and pass **Validate Jev readiness** for the current build.
2. In **Live playground**, use the default **Periodic task** as Foreground,
   set its **CPU budget to 16 ms**, and save settings. Its period is 20 ms.
   Keep its priority at **Normal (nice 0)**.
3. Use one **CPU hashing** workload as Background with **Normal** importance
   and **Normal (nice 0)**. Keep the standard single sandbox CPU placement;
   this run used workload CPU 0 and collector CPU 1.
4. Start the sandbox, wait for reversibility preflight, and start both workloads.
   Leave policy disabled for about 30 seconds to see the approximately 50/50
   CPU split. Select **Auto: Jev** and press **Enable policy**.
5. Watch the policy status for the requested/effective provider, proposal,
   latency, applied action and cooldown. The workload table and CPU traces show
   the resulting CPU use; the event timeline confirms the acknowledged nice
   value. Allow at least 10 seconds of valid observation after warm-up and
   several five-second decision windows. A model may choose hold; an
   intervention is not guaranteed.
6. Press **Disable policy** to restore the captured nice values. After
   acknowledgement, CPU use should return toward the original split. Stop the
   sandbox when finished; the recording contains the complete event chain.

Use **Auto**, not Shadow, for this demonstration: Shadow intentionally makes
no priority changes. If the effective policy says Rule, inspect the proposal
and command linkage before attributing a change to AI. Two consecutive
unusable model replies latch Rule until explicitly re-enabled.

The model chooses **hold, lower or restore**; deterministic guards choose the
eligible background and apply a Linux nice change. It does not specify a CPU
percentage. Both workloads contend on the same logical CPU, making the change
visible. The 16/20 ms setup deliberately creates strong contention; the
preregistered 6/20 ms benchmark remains separate and unchanged.

## Recorded result

Native session: `ece22bfe-82d9-438b-8171-791e05dcce16`, Jev `jev-1.13.0`,
guard `priority-v5`. Each row uses 30 seconds of raw records; post-change rows
skip five seconds to settle, and boundaries align with the foreground ticks.

| Phase | Background nice | Foreground CPU | Background CPU | Foreground deadline misses | p95 lateness |
|---|---:|---:|---:|---:|---:|
| Before Jev lowering | 0 | 49.55% | 49.59% | 99.93% | 31.25 ms |
| After Jev lowering | 10 | 80.43% | 18.80% | 28.00% | 1.80 ms |
| After Jev restoration | 0 | 49.42% | 49.42% | 100.00% | 32.58 ms |

CPU 100% means one logical CPU. Foreground priority remained nice 0 throughout.
Background throughput fell from 1,448,326 to 540,233 units/s (about 63% loss).
That cost exceeds the planned 40% background-loss gate; this demonstration
proves an observable effect, not a passing performance result.

Jev produced 44 accepted proposals: 41 holds, two lowers and one restore.
There were exactly **three model-originated interventions, zero Rule-originated
interventions and zero fallbacks**. Maximum decision latency was 507.54 ms.
The model's first lower links request `5ca36870ad2d450f9327eb23cc6597c0` to
command `27009043-0637-46c8-ba88-11788f51dc4b`; its restore links
`16787153680b42c3bdf9fbc88bb708eb` to
`fd0c8459-5d2f-4c95-8e19-a4b1b0e4c932`. Both have successful Linux
acknowledgements. The model lowered again later; disabling Auto restored that
final change before clean shutdown. All model changes respected the cooldown.
The model restoration worsened the foreground's measurements again: model
control is operational, but this run does not establish scheduling quality.

Full JSON/CSV export parity, proposal/command/acknowledgement linkage, unchanged
foreground, restoration and completed sandbox cleanup passed audit. The
recording contains 2,263 messages and 972 raw progress records, with no
credential fields. Evidence:

- [CPU effect report](../output/native-experiments/b20b491f44f9470685cb3c5976759591/cpu-effect-report.json)
- [Full recording](../output/native-experiments/b20b491f44f9470685cb3c5976759591/recording.json)
- [Audit](../output/native-experiments/b20b491f44f9470685cb3c5976759591/audit.json)

The earlier guard-v4 demonstration is preserved in
`output/native-ai-demo/5955a6c5-9eb9-4d79-b2d3-38abb68b8f30/`; it included one
model lowering, a later Rule fallback without further changes, and manual
restoration. It is not substituted for the fresh guard-v5 run above.

## Recovery fix and remaining checks

The earlier demo exposed a recovery defect: a returned transient provider-unavailable
error closed a healthy helper, so the next window manufactured a second
helper-unavailable failure. Guard `priority-v5` keeps the helper after that
first returned error; two actual failures still latch Rule. Two regression
tests failed before the fix and passed afterward. All **172 source tests pass**.

The rebuilt app passed deep/strict signing, checkpoint verification, matching
tested helper and dependency-lock checks, and bundled-licence checks. The old
app is preserved as `dist/previous-0cf5633b.app`. Fresh native guard-v5 Jev
readiness passed **107/108 (99.07%)**, with one retained invalid response, zero
timeouts, complete audits and zero unauthorized actions. Cold start was
154.10 ms; warm p50/p95/p99 were 402.89/518.68/568.91 ms. The exact frozen
corpus contains 36 cases each with one, three and seven backgrounds; maximum
input was 1,238 tokens and all expected context fields were retained. The
readiness report and independent recomputation audit are in
`output/native-readiness-observations/6cf3782fead14492af4776d4f1e36d41/`.
The native live run above verifies this rebuilt revision after readiness.

Current Laya MPS evidence remains unsuccessful: the latest native paced run
accepted 71/108 contexts within one second (65.74%, required at least 95%).
Neither that run nor the small CPU diagnostic authorizes live Laya. Native
three/seven-background acceptance, two 30-minute Laya soaks, held-out native
calibration and the 50-trial comparison remain incomplete.
