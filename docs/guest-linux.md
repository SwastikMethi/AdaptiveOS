# Guest Linux backend — UTM / Plasma X11

The source contains the Guest Linux resource backend. **Milestone 1 passed on the
UTM/Plasma baseline on 2026-10-10.** Fresh ownership, network, recovery and UI-state
evidence is retained in `.implementation/utm-plasma/verification.json`.
Historical UTM/Xfce measurements do not qualify this desktop. Docker remains the default, with its
protocol v5, recordings, controls and readiness gates unchanged. The guest uses
`guest-linux/1`, with additive resource-control capabilities and compatible historical
recordings. Milestone 2 adds 100 → 50 → 25 background CPU weights; its live
CPU mechanism gate passed on 2026-10-10, with the UI verification limits below.
The original 100 → 90 → 100 feasibility probe remains
available through the API. Memory protection/profiling, shared directional HTB,
Rule, guest-specific Jev qualification and paired comparison harnesses are now
implemented. Their current evidence and gate decisions are recorded below.

The Qt controller runs **inside the guest desktop**, as the ordinary user. A
root-owned service accepts that user's Linux `SO_PEERCRED` identity on a local
Unix socket. The client also requires a root service peer. No command strings,
paths, arbitrary executables, resource values or Jev keys cross that boundary.
The desktop's experimental Docker override is rejected for this backend.

## Host and VM prerequisites

Run this on the Mac before downloading or allocating a VM:

```sh
.venv/bin/python -m adaptive_os.guest_linux.preflight --output output/guest-host-preflight.json
```

The conservative prerequisite is **180 GiB available local storage**: 64 GiB each
for the working disk and a powered-off recovery copy, 8 GiB for installation
media, 12 GiB for recordings, and 32 GiB of headroom. Sparse disks may initially
occupy less, but must have room to grow. The report
contains exact available bytes and does not treat purgeable space as available.
The earlier storage and missing-UTM blockers were resolved: the current host
has UTM 4.7.5 and had 205.7 GiB free after both cold recovery copies.

Use UTM virtualization, Ubuntu **24.04 LTS ARM64**, **4 vCPUs**, **8 GiB RAM**,
a **64-GiB disk**, and shared networking. Install the server ARM64 image and
KDE Plasma on **X11**; make the root filesystem cover the disk (Ubuntu's default LVM layout
can leave half unallocated). Use the [UTM installation guide](https://docs.getutm.app/guides/ubuntu/)
for the VM creation flow; its example OS version is older than this project's
required Ubuntu release.

Inside the guest:

```sh
sudo apt update
sudo apt install kde-plasma-desktop sddm kwin-x11 xserver-xorg konsole \
  spice-vdagent qemu-guest-agent geany dbus-x11 python3-venv \
  iproute2 nftables curl conntrack tcpdump xauth x11-utils libxcb-cursor0 \
  libxcb-icccm4 libxcb-keysyms1 libxcb-xkb1 libxkbcommon-x11-0 libegl1 libopengl0
```

Install **native Firefox**, using Mozilla's signed APT repository and its key
verification instructions in [Mozilla's Linux installation guide](https://support.mozilla.org/en-US/kb/install-firefox-linux).
The guest adapter expects `/usr/bin/firefox` and rejects the Ubuntu Snap wrapper.
Snap/Flatpak launchers can move applications outside the owned unit; they are not
accepted by this feasibility backend. Resolve any Firefox/AppArmor sandbox errors
using package-supported configuration; do not disable the browser sandbox.

Select SDDM as the display manager and **Plasma (X11)** at login. Keep the UTM
SPICE/QEMU guest tools. VMware Fusion and open-vm-tools are not required.
Confirm `echo "$XDG_SESSION_TYPE"` reports `x11` and
`echo "$XDG_CURRENT_DESKTOP"` includes `KDE`. Before controller deployment,
check Firefox, Geany and Konsole typing, scrolling, resizing, clipboard, DNS,
HTTPS and a checksum-verified download. Record versions, renderer and idle
pressure; resolve desktop stutter before experiments. Shut down and verify a
complete VM bundle recovery copy. UTM live snapshots are not a substitute.

Copy this checkout into the guest; create a separate guest virtual environment.
Run installation from a Plasma terminal, preserving its actual display authority
path (SDDM may use a session-specific path):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop]'
sudo bash scripts/install_guest_linux.sh "$USER" "$DISPLAY" "${XAUTHORITY:-$HOME/.Xauthority}"
./start.sh --backend guest-linux --jev-env-file .env
```

The desktop extra installs PySide6 and the TLS certificate bundle used by Jev.
The env file is optional. It uses the existing redacted parser and prefills a
disabled masked field. Manual and Rule do not make Jev calls; readiness, shadow
and qualified Jev mode use the isolated unprivileged helper. The privileged install
copies only the service modules to `/opt/adaptive-os-guest` under root ownership.
Its isolated Python entry point never imports the writable checkout. The install
preserves existing `/etc/adaptive-os/guest.json` when no display arguments are
given. Explicit display arguments atomically update it for the same registered
UID. Xauthority must be a private regular file owned by that user, and an
unprivileged `xdpyinfo` check must authenticate successfully. After a session
change, reconfigure the display before launching new managed applications.
Stop the service before updating its installed code. Stopping it restores the
run's resource settings and leaves applications open.

Preflight also requires root-owned `/etc/adaptive-os/baseline.json` (not group or
world writable), with a fresh UUID `baseline_id`, `utm_version`, and true
`desktop_gate_passed` and `recovery_copy_verified` flags. Set these only after
retaining the corresponding evidence. Include the copy manifest, desktop settings
and verification report paths in that manifest. Snapshots record this baseline
plus kernel, architecture and guest package versions as a fingerprint. Collect
fresh calibration for Plasma; never compare its measurements directly with old
Xfce or Docker measurements. Keep KDE's configuration fixed across all arms.

## Ownership and recovery

Each launch gets a permanent task UUID, an ordinary-user profile directory under
`/var/lib/adaptive-os/apps`, a private D-Bus session, a network namespace and an
independent systemd service directly under `/adaptiveos.slice`. `ExitType=cgroup`
keeps ownership alive while descendants remain. Units have no `PartOf` or
`BindsTo` dependency on the controller or resource service. Application profiles
are retained when applications exit; no application files are deleted.

The parent has a fixed 4-GiB `memory.max`, with `memory.low=0`, `memory.min=0` and
`memory.high=max` through Milestone 2. Tasks use CPUs 1–3; the guest service and Qt
controller use CPU 0. Memory runs temporarily establish the protection hierarchy
described below; the fixed hard cap can still cause OOM.

Root-only SQLite state uses WAL and FULL synchronization. Task ownership and run
baselines commit before mutations; command intents commit before invoking kernel
tools. Acknowledgements follow CPU kernel read-back. Request IDs are durable and
duplicate mutations are rejected. The baseline is restored on End, controller
loss after ten seconds, normal service shutdown, or service restart. Restore
keeps the current run open. A replaced application identity is never written;
failed restorations retain their baseline and are retried. When the journal is
full, that already-durable baseline still authorizes restoration; new control is
blocked until recovery can be recorded. Diagnostic telemetry is separate from
identity/CPU read-back and does not hold the resource writer lock. The next
controller must observe the service and begin a fresh run. Heartbeats use a
separate lock and handler, independent of the serialized resource writer.

The socket and namespaces live outside application runtime-directory ownership.
The service runtime directory is preserved on restart. Its state lives in
`/var/lib/adaptive-os/journal.sqlite3` with WAL files. **Do not delete or replace
that state while managed applications or network objects exist.** This milestone
preserves applications across controller/service restarts, not VM reboot.

## CPU weights — Milestone 2

Choose a foreground application and each task's Low, Normal or High importance,
then choose **cpu** and **Begin run**. These choices stay fixed for that run. Foreground
is an explicit experiment selection, not inferred from the active X11 window.
All tasks must start at weight 100 with effective CPUs 1–3. **Favor foreground**
lowers one eligible background from 100 to 50, then from 50 to 25. Selection uses
importance first (Low before Normal before High), then highest eligible weight,
then stable task UUID. The foreground's weight remains 100; placement never changes.

These are relative weights for competing runnable cgroups, not CPU quotas or
guaranteed utilization. See the [kernel's weight model](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#weights).
The service records CPU usage and pressure separately from memory consumption.

The service issues an observation token bound to its generation, run, task set,
identities, current weights and placement. A CPU decision needs ten valid
observation seconds, a token at most three seconds old, at least five seconds
since the last decision and a thirty-second cooldown after a weight change.
Missing/invalid samples reset observation readiness; duplicates, consumed tokens,
stale generations, identities, weights and placement drift are rejected. Kernel
read-back and journal acknowledgement must succeed before a Favor is acknowledged.
A partial operation restores the whole captured run. Task-set changes end CPU
control and restore remaining applications.

**Restore** removes all current manual interventions, retaining the run and its
captured settings. **End run**, controller loss for ten seconds, controller
shutdown and service recovery restore the baseline and leave applications open.
Recovery bypasses observation and cooldown gates. There is one outstanding control
request, with independent heartbeat handling and telemetry outside the resource
writer. Rule and qualified Jev use these same bounded operations and observation
gates; model inference has an independent one-second deadline.

Run the CPU proof inside the guest with exactly one managed Firefox and one
managed Geany. Close the controller with `./stop.sh` first; its applications stay
open, and a second snapshot collector cannot supersede the verifier's observation
token. Each command retains failed evidence. Supply
`--saved-file PATH` to compare a known test file's checksum before/after recovery.

```sh
.venv/bin/python -m adaptive_os.guest_linux.cpu_verify --scenario contention --output output/guest-cpu/contention.json
.venv/bin/python -m adaptive_os.guest_linux.cpu_verify --scenario normal --output output/guest-cpu/normal.json
.venv/bin/python -m adaptive_os.guest_linux.cpu_verify --scenario controller-loss --output output/guest-cpu/controller-loss.json
.venv/bin/python -m adaptive_os.guest_linux.cpu_verify --scenario service-restart --output output/guest-cpu/service-restart.json
.venv/bin/python -m adaptive_os.guest_linux.cpu_verify --scenario service-crash --output output/guest-cpu/service-crash.json
```

Contention proof uses `sudo -n` only to prepare temporary ordinary-user CPU
helpers inside the two registered cgroups and their task network namespaces.
The helper validates the journalled task and live systemd invocation; it cannot
target a caller-supplied PID or arbitrary cgroup. Helpers use CPUs 1–3, their
supervisor/verifier CPU 0, and terminate on completion or a bounded timeout.
Application processes are never signalled. Service-interruption scenarios use
`sudo -n systemctl` for the named guest service.

The contention report measures equal/50/25/restored distribution over steady
30-second windows after five seconds settling. Acceptance requires at least
2.7 CPU-seconds per second across the three task CPUs, shares within ten percentage
points of the configured ratio, an eight-point foreground increase at each step,
and restoration within eight points of the initial share. Every sample must retain
task identities and placement. This validates the mechanism; it is not one of the
calibrated randomized Default/Manual/Rule or Jev comparison trials.

The verified CPU evidence is in `.implementation/guest-cpu/verification.json`:

| Background weight | Foreground share of consumed task CPU | Window |
|---|---:|---:|
| 100 | 49.80% | 30.85 s |
| 50 | 66.68% | 30.90 s |
| 25 | 79.95% | 30.90 s |
| Restored 100 | 50.00% | 30.80 s |

All windows sustained approximately 2.99 task cores; placement and task identities
were unchanged. `contention.json` retains every sample and helper cleanup result.
The four recovery reports prove 100→50→25→100 under normal end, controller loss,
service restart and service crash, plus duplicate/stale-command rejection.
Saved-file checksums passed the first three scenarios. `worker.json` and its
exports prove the complete live controller-worker flow and nested JSON/CSV parity;
five older M1 recordings still load. `native.json` records the native Qt launch,
keyboard Begin, readiness, clean shutdown and 67-message export parity. The native
Favor/Restore buttons were not independently confirmed through UTM input; their
UI wiring has source tests and the underlying worker completed the live flow.

`service-crash-interrupted.json` and `interrupted-boot.log` retain an invalid
attempt interrupted by a login-screen reboot. `upgrade-interruption.log` records
a subsequent scheduled Ubuntu package upgrade that restarted the new editor;
the service rejected its replaced identity. The upgrade completed before the
successful crash repeat with fresh tasks. The user requested skipping repeated
manual saved/unsaved-text checks after these interruptions. No such repeat is
claimed; surviving processes alone do not prove unsaved text preservation.

Keep scheduled package upgrades outside verification and comparison windows;
they can restart managed application units. Finish running upgrades before a
trial, record package versions, and restore any temporarily paused timers after
testing. Do not cancel dpkg mid-upgrade. After an OS update, finish any required
restart and refresh the desktop baseline/cold copy before calibration. No direct
comparison between the pre-upgrade contention proof and later measurements is
valid. CPU control does not preserve unsaved work across VM reboot, logout or
external application restarts.

## Unshaped routing

The reserved task subnet is `10.203.0.0/16`; conflicting routes are rejected.
There is one root `h-down` ↔ gateway `g-up` transit link, on `10.203.0.0/30`.
Task slot N has a private `tN` ↔ `gN` link on `10.203.N.0/30`, with task address
`.2`, gateway address `.1` and a single default route. A root-owned resolver file
contains reachable IPv4 nameservers, excluding the host loopback stub.
The supported Ubuntu resolver layout links `/etc/resolv.conf` to either
`/run/systemd/resolve/stub-resolv.conf` or `/run/systemd/resolve/resolv.conf`.
Each task has a private tmpfs at `/run/systemd/resolve` and a read-only bind of
the managed resolver file. This keeps host resolver file replacement from
removing the task's override. Preflight rejects other symlink layouts; snapshots
check the private mount and exact resolver content before allowing control.

Gateway routing performs no NAT. Root postrouting masquerades task traffic on
the VM uplink; reverse NAT restores the destination before routing down `h-down`.
See the [Netfilter hook ordering](https://wiki.nftables.org/wiki-nftables/index.php/Netfilter_hooks).
IPv6 is disabled in task and gateway namespaces. Outside Network runs the shared
devices have no HTB tree. The network remains while applications remain; last-task
exit removes its owned objects and restores the previous root forwarding flag.
Other guest firewall rules are not flushed or disabled. If they block forwarding,
resolve that explicitly and retain the failed connectivity evidence.

## Memory protection and headroom — Milestone 3

Choose **memory**, a foreground task, and **Begin run**. A new profile starts
every child at `memory.high=max`. Exercise representative activity for at least
30 continuous pressure-free seconds, then use **Establish headroom**. The comparison
calibrator exercises the busy memory workload during this pressure-free profile,
then separately tests external contention against the frozen thresholds.
The peak `memory.current` includes cache; it is a conservative footprint estimate,
not an exact working set. Each high threshold is rounded up to 64 MiB from
`peak + max(256 MiB, 25% of peak)`. If their total cannot fit the 4-GiB safety
envelope, thresholds stay unlimited and the profile is unsuitable.

Tasks sit directly under root-level `adaptiveos.slice`. During Memory runs its
low/min/high/max are 3 GiB / 0 / max / 4 GiB. Initial child protection targets
are equal, at least 256 MiB, rounded down to 64 MiB, and bounded by the smallest
established high. Favor uses unused protection first, then transfers 64 MiB
from an eligible background. It never lowers a background high threshold.
The UI and recordings keep consumption, low, high, parent cap, pressure,
reclaim/refault and OOM/high counters separate.

Growth or throttling permanently invalidates that profile for scored use.
**Increase headroom** only raises thresholds (or retains max when headroom cannot
fit); it does not make the failed profile eligible again. Recalibrate before a
fresh comparison. Policy disable restores starting protection, retaining an
upward safety adjustment. End, controller loss and service recovery restore child
and parent pre-run memory settings, keeping the fixed parent cap while tasks exist.

## Shared bandwidth — Milestone 4

Choose **network** and **Begin run**. Exactly one 20-Mbit/s HTB parent on gateway
`g-up` handles all uploads by original source IP; one on root `h-down` handles
downloads by destination IP after reverse NAT. Every task has a child under each
parent. Guarantees start equal; Favor transfers five percentage points per
direction, bounded by a 5% minimum and an 80% foreground maximum. Child ceilings
are 20 Mbit/s for borrowing, with 256-packet leaf queues.

Restore returns to equal run-start shares. End/recovery removes the owned
shapers and preserves routing/NAT and application namespaces. Partial updates
restore both directions from the durable baseline. Ubuntu's tc 6.1 emits text
for class statistics even with `-j`; the backend verifies its exact integer rates
with a tested strict parser while retaining JSON for qdiscs and classifiers.

## Policies, qualification and comparisons

Manual exposes the bounded Favor/Restore controls. Rule uses measured foreground
CPU pressure, memory pressure/refaults, or directional queue backlog. Both Rule
and Jev keep five-second decisions, ten valid observation seconds and a
thirty-second intervention cooldown. There is one outstanding model request;
invalid, late or unavailable responses fall back to Rule. Authentication failure
is latched rather than retried every cycle.

In the desktop controller, select **Jev**, click **Apply policy**, then **Begin
run**. Normal interactive runs need a configured Jev key, but do not require a
readiness report or shadow qualification. The **Active** label shows the applied
policy. Failed policy changes return the selector to the active mode, and the
error remains visible through monitoring refreshes until another command succeeds.
Readiness and shadow checks remain available under **Setup and diagnostics**.
All observation, action validation, deadline, cooldown, fallback and kernel guards
still apply to direct Jev control.

Scored Jev experiments require a separate 108-context, five-second-paced readiness report and at
least 36 live shadow decisions for each resource. At least 95% must be valid
within one second; all requests must be audited, with zero unauthorized actions.
Qualification binds the VM baseline, installed privileged service, controller
sources, resource and model. It expires after 24 hours or a relevant change.
Failed evidence supersedes older passes. Docker readiness and experimental
overrides cannot qualify guest experiments. The experiment harness keeps this
qualification requirement; the desktop's interactive setting cannot bypass it
for a scored run.

Run these commands as the ordinary guest user, with one managed Firefox and
one managed Geany. Stop the desktop controller first so another snapshot
collector cannot supersede the verifier's observation token. Use new output
directories; retain failures. On the Mac, run the bounded test endpoint:

```sh
.venv/bin/python -m adaptive_os.guest_linux.fixtures --serve --host 192.168.64.1 --port 8766
```

Inside the guest (repeat for cpu, memory and network where applicable):

```sh
.venv/bin/python -m adaptive_os.guest_linux.experiments calibrate --resource memory --output output/guest/calibration
.venv/bin/python -m adaptive_os.guest_linux.resource_verify --resource memory --scenario normal --calibration CALIBRATION.json --output output/guest/memory-normal
.venv/bin/python -m adaptive_os.guest_linux.resource_verify --resource network --scenario contention --calibration NETWORK-CALIBRATION.json --output output/guest/network-contention
.venv/bin/python -m adaptive_os.guest_linux.experiments compare --resource memory --calibration CALIBRATION.json --output output/guest/memory-three-arm
.venv/bin/python -m adaptive_os.guest_linux.experiments readiness --resource memory --output output/guest --jev-env-file .env
.venv/bin/python -m adaptive_os.guest_linux.experiments shadow --resource memory --calibration CALIBRATION.json --output output/guest --jev-env-file .env
.venv/bin/python -m adaptive_os.guest_linux.experiments compare --resource memory --calibration CALIBRATION.json --output output/guest/memory-four-arm --model --jev-env-file .env
```

Recovery scenarios are `normal`, `controller-loss`, `service-restart`,
`service-crash` and `contention`. Calibration includes measured light activity and
a steady 30-second contention pilot; insufficient pressure/saturation or required
headroom adjustment rejects calibration. Comparisons use five randomized paired
blocks per light/contention profile, 30 seconds warm-up and 180 seconds measurement:
30 trials per resource for Default/Manual/Rule, then a fresh 40 if Jev qualifies.
Each three-arm resource comparison takes at least 105 minutes.

Probes run as the ordinary user inside the real applications' owned cgroups and
network namespaces. Reported latency is synthetic probe latency, **not GUI latency**.
Placement, highs, caps, workloads and starting allocations stay fixed across arms.
Settings drift, identity changes, OOM or headroom failures invalidate the trial;
failed and unrun slots remain recorded. No AI-usefulness claim is valid without
improvement over Rule and the ≥20% foreground / ≤40% background-loss gate versus
Default. Controller CPU/RSS currently covers the controller process; helper
overhead and per-trial RSS attribution remain a documented limitation.

## Verification and evidence

Source regression checks, including actual Unix-socket framing and concurrency
with simulated kernel operations:

```sh
.venv/bin/python -m unittest discover -s tests -q
bash -n scripts/install_guest_linux.sh start.sh stop.sh
```

In the guest UI, launch Firefox and Geany. Save a file, type additional unsaved
Geany text, and open a Firefox page with visible state. Keep a second instance
of each application open to check profile and instance independence. Record the
task IDs, invocation IDs, profile paths and process/network namespace inventory.
End any UI probe run before running the following verifier:

```sh
.venv/bin/python -m adaptive_os.guest_linux.verify --scenario observe --output output/guest/observe.json
.venv/bin/python -m adaptive_os.guest_linux.verify --scenario normal --output output/guest/normal.json
.venv/bin/python -m adaptive_os.guest_linux.verify --scenario controller-loss --output output/guest/controller-loss.json
sudo -v
.venv/bin/python -m adaptive_os.guest_linux.verify --scenario service-restart --output output/guest/service-restart.json
sudo -v
.venv/bin/python -m adaptive_os.guest_linux.verify --scenario service-crash --output output/guest/service-crash.json
```

The loss scenario deliberately omits heartbeats. Also force-kill **only the Qt
PID recorded by this checkout's launcher**, wait at least ten seconds, reopen the
controller and capture another observation. Never kill a Firefox or Geany PID.
The restart/crash verifier invokes `sudo -n` only for the named service and retains
failures. Each report includes before/after identities, CPU settings, placement,
profiles, namespace data and service journal events. Passing that script proves
only those checks; `milestone_gate_passed` stays false pending the remaining proof.

After **each** scenario, verify the saved-file checksum, unsaved Geany text,
Firefox page state and independent application windows. Retain screenshots and
notes with the matching task IDs. A surviving profile directory alone is not
evidence that unsaved application state survived.

Use the Firefox `main_pid`, namespace, gateway and uplink from the snapshot. In
a guest terminal, substitute those recorded values for `PID`, `NS`, `GW`, `NIC`:

```sh
sudo nsenter -t PID -n -m -- setpriv --reuid="$UID" --regid="$(id -g)" --init-groups getent ahostsv4 example.com
sudo nsenter -t PID -n -m -- setpriv --reuid="$UID" --regid="$(id -g)" --init-groups curl --fail --max-time 30 https://example.com/
sudo ip netns exec NS ip -4 route
sudo ip netns exec NS ip -6 route
sudo ip netns exec GW nft list ruleset
sudo nft list ruleset
sudo conntrack -L -f ipv4
sudo tcpdump -ni h-down -w /tmp/guest-transit.pcap
sudo tcpdump -ni NIC -w /tmp/guest-uplink.pcap
```

Capture inside `NS` and `GW` as well. Correlate original task source IPs on `g-up`,
translated uplink addresses, reverse-NAT task destinations on `h-down`, and
conntrack tuples. Record all descendant cgroups and namespace IDs. Browser
isolated namespaces are expected only when they have no external route; identify
the browser's actual networking processes and prove they use the managed path.
This audit is mandatory: process inventory alone does not prove absence of an
escaped helper.

Host a known file on a temporary Mac endpoint, record its SHA-256, download it
from each task network path before and after restoration, and compare SHA-256.
Record public DNS and HTTPS separately. Keep captures local; they may contain
browsing metadata. The Milestone 4 verifier measures both shared directions,
task guarantees and sole-task borrowing over steady 30-second windows.

Guest recordings use `output/desktop/guest-linux/sessions-v2.sqlite3`. The guest
Recordings tab shows saved snapshots and acknowledgements and exports full JSON
or CSV; the CSV payload retains all nested guest fields. Existing Docker playback
and exports continue to use their own database and protocol.

## Milestone gates

Earlier CPU decision: **PASS for the Milestone 2 CPU mechanism**. Manual UI-state repetition was skipped at the user's
request; native button-path coverage remains partial as detailed above. This does
not qualify automatic Rule/Jev control or any scored comparison. Source regression
passed 308 tests; the guest ran 70 with 2 skipped and no failures. Fresh Docker v5
live recording/export regression passed. Its evidence is linked from the CPU
aggregate report. All CPU interventions were restored and the applications remain
open. These counts describe the earlier CPU milestone, before memory and HTB implementation.

The following records the earlier Milestone 1 gate:

The Plasma desktop gate and cold recovery copy passed on 2026-10-10: KDE
5.27.12/X11, SDDM 0.20.0, SPICE 0.22.1, QEMU guest agent 8.2.2, user-confirmed
smooth interaction, DNS/HTTPS and a 4-MiB checksum download. Rendering uses
llvmpipe; keep this configuration fixed. Evidence is in `.implementation/utm-plasma/`;
the cold copy and SHA-256 manifest are under `output/guest-vm/recovery/`.
The copy itself has not been boot-tested.

Milestone 1 decision: **PASS; CPU implementation may proceed**.
This is a functional feasibility result, not performance calibration or approval
to enable later resource policies. The small probe remains available through the API.

Fresh evidence in `.implementation/utm-plasma/` includes:

- `ownership-before.json` and `ownership-after.json`: all managed descendants
  remain owned; Firefox Socket Processes use the task path, while browser-isolated
  namespaces have no IPv4/IPv6 route. The latter audit identifies the separate
  user-launched ordinary Geany by PID, birth time, parent and user scope.
- `packet-path-proof.json` and `guest-plasma-m1/network-{before,after}/`: all four
  task DNS/HTTPS/4-MiB checksum checks, captures at task/gateway/transit/uplink,
  and conntrack correlation. The two remaining tasks also passed final checks
  after the user closed one idle Firefox and one idle Geany between trials.
- `guest-plasma-m1/{normal,controller-loss,service-restart,service-crash,qt-normal,qt-crash}.json`
  and `journal-recovery-proof.json`: weight restoration, stable identities,
  placement, profiles and namespaces, with journal intent/ack ordering.
- `managed-geany-preservation.json` and the `managed-geany-*.json` recovery reports:
  saved file checksum and unsaved `managed-unsaved` text survived all four
  scenarios. Geany's status pane identifies its unique managed profile path.
  The initial fixture was opened in ordinary Geany; that failed fixture evidence
  remains in `ui-preservation.json` and is excluded from the managed-text pass.
- `guest-plasma-m1/recordings/report.json`: actual Qt completed/interrupted
  playback and full JSON/CSV parity. The normal run used an API probe after a
  UI start; the crashed controller's Begin and Probe actions were performed in
  the UI. No guest Jev calls were made.
- `docker-live-regression.log` and `docker-restored.json`: fresh Docker v5
  regression/export/cleanup pass and restoration of the two original RAG
  Playground containers. Source suite: 285 passed; guest subset: 47 run, 2 skipped.

Remaining limits: automated UTM pointer/key delivery is unreliable, although the
user's manual desktop check was smooth. Managed Geany windows include
`(new instance)` in their title; a new tab in ordinary Geany is not a managed
instance. Keep the saved and unsaved test windows open when reproducing recovery.
One cold boot under severe host memory pressure hit a graphics soft-lockup;
the retained retry boot took 6.336 seconds after Docker was stopped. That
correlation is not a proven root cause. Recovery copies have matching file hashes
but have not themselves been boot-tested. Recalibrate on a stable host before
scored trials; do not mix the first Docker-off tests with later Docker-on checks
as performance comparisons.

Follow the supplied corrected plan without changing its
bounds: CPU weights 100/50/25 and fixed placement; a distinct 3-GiB memory.low
pool with independently profiled memory.high and the fixed 4-GiB parent cap;
then two common 20-Mbit/s HTB parents. Freeze calibrated settings across the
30-trial Default/Manual/Rule comparisons. Jev needs fresh guest-specific paced
108-context readiness, ≥95% timely valid replies, complete audits and resource
shadow checks before the fresh 40-trial four-arm comparisons. Docker readiness
and experimental overrides cannot qualify the guest. No AI usefulness is claimed.

## Current whole-system verification

The remaining implementation is in place. The live worker has passed Manual and
Rule flow for all three resources, normal restoration, historical playback and
184-message JSON/CSV parity. Actual interruption after the first memory or HTB
kernel write recovers child/parent settings and both network directions while
preserving application identities and connectivity. The earlier four-scenario
Memory/Network recovery series and packet/rate/share/borrowing proof are retained
in `.implementation/guest-remaining/`; final recalibration and comparison results
are retained under `output/guest-remaining/verified-runs/` inside the guest. The
latest complete Network comparison is `network-final-20261012-r9/`; the aggregate
local evidence is `.implementation/guest-remaining/verification.json`.

Fresh CPU and Network calibration pass on the kernel-146 baseline. Memory's
independent high thresholds remain stable and reclaim is observed, but the bounded
pressure fixture does not meet the sustained-pressure acceptance criterion.
Memory scored comparisons remain blocked. The previous `tuned-runs/` comparison
set is explicitly invalid because Docker's host runtime state changed during it;
its raw evidence is retained and is never merged with the replacement set.

Historical Jev gates before the snapshot-audit correction:

| Resource | Timely valid readiness | Timely valid shadow | Jev control |
|---|---:|---:|---|
| CPU | 106/108 | 7/36 | Blocked: unavailable action proposals and one deadline |
| Memory | 105/108 | Not run | Blocked: contention calibration failed |
| Network | 108/108 | 35/36 | Passed on that revision; fresh qualification required after the audit fix |

All requests were audited with zero unauthorized actions. Every applied shadow
action passed the kernel guard. Qualification is not evidence of usefulness;
the paired comparison results are tracked separately.

The CPU Default/Manual/Rule set completed all **30 valid trials**, with five paired
blocks per profile and measurement windows of 180.058–180.462 seconds. Task
identities, CPUs 1–3, independent memory settings and the 4-GiB parent cap stayed
fixed. The following uses the mean paired foreground p95 change against Default;
positive means faster. Background loss is the worst Geany throughput loss across
the five blocks.

| CPU profile | Arm | Foreground p95 improvement | Worst background loss |
|---|---|---:|---:|
| Light | Manual | 3.1% | 5.8% |
| Light | Rule | 5.6% | 2.3% |
| Contention | Manual | 9.0% | 13.3% |
| Contention | Rule | -61.8% | 4.2% |

The adverse Rule mean is dominated by a retained 193.47-ms trial against
47.87 ms for its paired Default. Its cause has not been isolated; no trial was
dropped. These results do not establish a CPU policy performance benefit for
interactive applications. Raw outcomes, deadline misses, throughput, pressure,
fallback and process overhead remain in `cpu-three-arm-comparison.json` under
`.implementation/guest-remaining/`; its companion checks report verifies the
complete schedule and fixed settings. This CPU set binds its own frozen source
revision. The later snapshot-audit correction does not revise its measurements.
The companion `cpu-three-arm-comparison-metrics.json` summarizes deadline misses,
every task's throughput and peak consumption, memory counters, fallback and
controller overhead. Across these trials there were no new high/max/OOM events;
task memory PSI accumulated 2,029 microseconds of `some` stalls in total. Mean
controller CPU per arm/profile was 0.56–0.64% of one core, excluding the helper
and service. Maximum RSS is a process lifetime peak, not per-trial incremental use.

The earlier network three-arm set retained 13 valid trials, one failed trial and
16 unrun slots. Its first four-arm trial was interrupted and remains unscored,
with 39 unrun slots recorded. The rejected observation's exact cause was not saved;
sampling now records every authenticated snapshot before validating it. Rejected
data still cannot become the latest control observation. The fix passed a failing
regression first. The next network set exposed the test endpoint's 10-second idle
timeout after a slow request induced a longer paced background pause. A real-socket
test reproduced the disconnect; idle retention now covers a bounded fixture while
client request timeouts remain 10 seconds. All slow requests and failed sets remain
recorded. Latest source tests: 365 host passes; 125 guest passes with two platform
skips. One earlier relay cleanup assertion failed, then passed in isolation and
in the full repeat without changing relay code.

A later network calibration measured 18.77-Mbit/s upload, below the 19-Mbit/s
minimum, so it correctly blocked comparisons. The following set then retained 21
valid trials and stopped on a vanished SSH session during preflight enumeration.
The saved rejected snapshot proved Plasma and both managed applications were
unchanged. Preflight now skips only the exact error for that vanished candidate;
an absent or invalid desktop, other errors and timeouts still fail closed. After
the service update, 80 live observations passed with 36 successful short SSH
connections overlapping the check. That r4 set completed 30 three-arm and 40
four-arm trials, but final public DNS failed after host systemd-resolved replaced
the resolver file. Its exact failure time is unknown; all raw outcomes remain,
with an explicit annotation excluding the set from final acceptance.

The resolver directory is now private to each task. A real systemd probe proved
that the old bind lost managed DNS after each of five atomic file replacements;
the new directory mount preserved it through all five. Journalled in-place repair
kept the existing applications open. Their DNS survived resolved/service restarts
and a forced service crash during Network control. Final restoration, public
HTTPS and a checksum download passed. The r5 replacement was interrupted when
the user closed Firefox during trial 13; 12 valid trials and 17 unrun slots remain
recorded. DNS was healthy before that confirmed closure. Geany and the closed
Firefox profile were preserved, and unused namespace cleanup completed. A fresh
managed Firefox launch passed DNS/HTTPS/checksum checks. The next verification used
`output/guest-remaining/network-final-20261011-r6/`, with fresh Jev qualification
before its four-arm set. No partial results are combined.

R6's three-arm set has now completed **30/30 valid trials**. An independent
artifact check confirms all five paired blocks per profile, 30-second warm-up,
180.075–180.436-second measurement windows, one calibration, stable identities
and placement, unchanged CPU weights, and fixed child/parent memory settings.
Mean paired foreground p95 improvement and worst background throughput loss are:

| Network profile | Arm | Foreground p95 improvement | Worst background loss |
|---|---|---:|---:|
| Light | Manual | 19.0% | 12.5% |
| Light | Rule | 22.3% | 1.8% |
| Contention | Manual | 16.5% | 0.9% |
| Contention | Rule | 13.0% | 1.7% |

These are synthetic probe results. The complete report and independent checks
are `network-r6-three-arm-comparison.json` and
`network-r6-three-arm-comparison-checks.json` in `.implementation/guest-remaining/`.
`network-r6-three-arm-comparison-metrics.json` includes deadline misses, every
task's throughput and peak consumption, memory-event/PSI deltas, fallback and
controller overhead. No new high/max/OOM events or task memory PSI stalls were
observed in these 30 measurement windows. Mean controller CPU per arm/profile was
0.60–0.78% of one core, excluding the helper and service. Network throughput in
this summary combines completed upload and download bytes; directional kernel
rates remain in the raw recordings and mechanism-verification reports.
Fresh Network Jev readiness passed **108/108** timely valid contexts, with a
425-ms median response. Live shadow passed **35/36** timely valid decisions
(one deadline fallback), with a 405-ms median response. Every request was audited,
all 36 shadow decisions passed the kernel guard, and unauthorized actions were
zero. These historical reports bind that revision's private-resolver source and installed service.
That r6 four-arm set stopped at trial 14, retaining 13 valid trials, the failed
trial and 26 unrun slots. A snapshot received at age 1.743 seconds, followed by a
924.5-ms model response and dispatch, exceeded the service's three-second limit.
The service correctly rejected the decision before writing, and End restored
settings while both application identities and managed DNS survived.

The controller now audits a held fallback for the exact pre-write freshness
rejections, never replays that binding, and waits for a new observation. It counts
fallback once and retains the failed kernel guard in model audits. Settings drift,
service-generation changes and transport failures still surface. The three-second
service limit and one-second inference deadline are unchanged. New regressions
passed RED→GREEN. A real delayed-command test for both CPU and Network verified
unchanged settings after rejection, no replay, later acceptance, restoration,
application preservation and JSON/CSV parity. Its deterministic helper fixture is
not model qualification.

The completed r6 three-arm set remains evidence for its frozen revision. The r7
run used fresh calibration, 108-context readiness and live shadow before a new
40-trial set containing all four arms. R7 calibration passed. Readiness passed **107/108** timely
valid responses (one invalid proposal; median421ms), and shadow passed **36/36**
(median405ms), with complete audits, zero unauthorized actions and all kernel
guards passing. At the user's request, r7 stopped before a temporary network
outage: 10 trials completed, trial 11 was interrupted, and 29 were unrun. The
applications and restored resource settings survived. After the user resumed,
DNS, public HTTPS and a checksum download passed. The replacement
run, r8, passed readiness (105/108) and shadow (36/36), then stopped with 15 valid
trials, failed trial 16 and 24 unrun slots. A telemetry read exhausted three busy
responses from the service's 50-ms writer-lock acquisition. Its original lock
holder was not recorded. A healthy 600-ms writer reproduced the rejection in a
real-socket regression. The service now waits up to one second for the lock;
heartbeats remain independent, rejected writes remain unapplied, and freshness
and authorization guards are unchanged. Full host/guest regressions and 80 live
snapshots passed, as did restored-state and connectivity checks after installing
the service. The `output/guest-remaining/network-final-20261012-r9/` run completed
fresh calibration, readiness and shadow and all **40 valid trials**. No partial
sets are combined. Fallback proportions include warm-up decisions; measured controller
CPU and memory-counter deltas use the measurement window.

R9 readiness passed **108/108** timely valid responses (median 381 ms), and live
shadow passed **36/36** (median 354 ms). All 144 requests were audited, with zero
unauthorized actions and all 36 shadow kernel guards passing. The comparison's
independent artifact checks confirm five paired blocks for each profile, full
180.076–181.389-second windows, one calibration, stable application identities
and placement, unchanged CPU weights, and fixed child/parent memory settings.

| Network R9 profile | Arm | Foreground p95 improvement | Worst background loss |
|---|---|---:|---:|
| Light | Manual | 13.1% | 12.0% |
| Light | Rule | -1.9% | 31.7% |
| Light | Jev | 8.0% | 30.2% |
| Contention | Manual | 13.3% | 2.8% |
| Contention | Rule | -3.2% | 4.9% |
| Contention | Jev | 7.2% | 3.7% |

Jev's mean foreground p95 is better than Rule's in both profiles, but it fails the
required 20% improvement against Default. **The AI usefulness gate did not pass.**
These are synthetic probe measurements, not browser/editor interaction latency.
Mean Jev fallback proportions are 1.38% and 1.48% for light and contention runs,
including warm-up decisions. No new memory high/max/OOM events or task memory PSI
stalls occurred during measurement. Controller CPU averaged 0.85–1.56% of one core
across arms/profiles; helper/service costs are excluded and RSS is a process
lifetime peak. Complete per-task costs and counters remain in the metric report.

The final actual desktop worker passed two audited, kernel-guarded Jev decisions,
End, shutdown, history/playback and **32-message JSON/CSV parity**. Final checks
before and after restoring Docker and the guest update timers passed: both app
identities, profiles, namespaces and placement survived; CPU weights are 100;
child protection/throttling and parent protection are restored; the fixed 4-GiB
parent cap remains; both egresses are unshaped; DNS, public HTTPS and checksum
downloads work. Network qualification still matched the guest after cleanup.
It retains the existing 24-hour expiry and invalidates on baseline/source changes.

The original RAG Playground Postgres and Qdrant containers are running and healthy.
CPU Jev remains unqualified, and scored Memory remains blocked by insufficient
sustained-pressure calibration. Historical CPU30 and Network R6/30 evidence stays
bound to its own revision. Full raw recordings remain in the guest; compact local
reports, including failed attempts, are under `.implementation/guest-remaining/live/`.

Recheck the completed Network artifacts from the Mac project directory with:

```bash
.venv/bin/python .implementation/guest-remaining/check-comparison.py .implementation/guest-remaining/network-r9-four-arm-comparison.json
.venv/bin/python .implementation/guest-remaining/comparison-metrics.py .implementation/guest-remaining/network-r9-four-arm-comparison.json
```

Failed calibration and verification attempts remain alongside passing evidence.
Firefox startup initially exceeded the profile safety envelope; a later light-only
profile missed busy-workload growth. The calibrator now profiles representative
busy activity without external pressure. Memory background probes read/checksum
1 MiB per operation and foreground probes 64 KiB, retaining every operation's
latency and byte count with less recording overhead. These are synthetic workload
measurements, not browser or editor interaction latency.
