# Sleepy VMs — auto-sleep idle VMs, wake on demand

Free host RAM by putting an **idle** VM to sleep and resuming it on demand. A
sleeping VM keeps its disk but releases its cgroup, so its RAM returns to the host
(the whole point). It wakes three ways: an operator **Start**, a host reboot's
recovery path, or — the reason a tenant never notices — **the first inbound TCP
connection**. When a memory snapshot was captured on sleep, the wake resumes the
guest in milliseconds instead of cold-booting.

This is opt-in per VM (`sleep_on_idle`) and orthogonal to `memory_snapshot_on_stop`
([05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md)); it reuses
that same in-jail memory-snapshot fast path.

## State

A VM gains the `Sleeping` status and three fields
([02-doctypes.md](./02-doctypes.md)):

| Field | Meaning |
| --- | --- |
| `sleep_on_idle` | Opt-in. Only these VMs are polled and auto-slept. |
| `idle_timeout_seconds` | Idle window before auto-sleep. `validate()` enforces ≥ 120 (two poll cycles). |
| `last_traffic_at` | Stamped by the traffic poller; seeded to `now()` at provision and at every start/wake so a fresh VM is never immediately slept. |

Transitions (additions to the lifecycle state machine):

```
Running  → Sleeping   (auto, idle timeout)
Sleeping → Running    (wake — operator Start, or host-initiated on inbound TCP)
Sleeping → Terminated (terminate)
```

`sleep()` accepts only `Running`; `wake()` accepts only `Sleeping`; `start()` from
`Sleeping` delegates to `wake()` (the desk **Start** button just works). `pause()`,
`snapshot()`, `stop()`, `rebuild()`, `resize()` reject `Sleeping` with a message to
stop or wake first — the memory snapshot is never silently consumed.

## Detecting idleness

Two per-minute scheduled jobs
([virtual_machine.py](../atlas/atlas/doctype/virtual_machine/virtual_machine.py),
wired in [hooks.py](../atlas/hooks.py)):

- **`poll_vm_traffic`** — for each server with `Running` `sleep_on_idle` VMs, runs
  the server-scoped `poll-vm-traffic` Task. The host reads each VM's `inet atlas
  forward` nftables **byte counters** (`ip6 {s,d}addr <vm> … counter accept`,
  installed by [vm-network-up.py](../scripts/vm-network-up.py)), compares to the
  last-seen value stored on the host (`traffic-counter.json`), and returns only
  `active: bool` per VM — no raw counters touch the DB. Active VMs get
  `last_traffic_at = now()`. A counter reset (chain reload / reboot) reads as
  active, so a VM is never slept on a measurement anomaly.
- **`sleep_idle_vms`** — sleeps every `Running` `sleep_on_idle` VM whose
  `last_traffic_at` is older than its `idle_timeout_seconds`. Skips
  `stop_protection` and re-reads status under the row so a concurrent poll can't
  race it.

## Sleeping

[sleep-vm.py](../scripts/sleep-vm.py):

1. Capture a **full memory snapshot** in the jail (pause vCPUs → Firecracker
   `PUT /snapshot/create` → `READY` marker), preflighted for disk space and launcher
   support. On any failure, fall back to a plain stop — the VM still ends up
   Sleeping; only the next wake's speed differs (`memory_snapshot=false`).
2. `systemctl stop firecracker-vm@<uuid>.service`. Its `ExecStopPost`
   ([vm-network-down.py](../scripts/vm-network-down.py)) tears the VM's host
   networking **completely** down: the proxy-NDP entry, the `/128` route, the
   netns/veth/tap, and the per-VM forward rules.
3. Write the **`SLEEPING` marker** (`…/<uuid>/sleeping`). The unit's
   `ConditionPathNotExists=…/sleeping` makes systemd skip the unit on host reboot
   **without** disabling it, so the VM stays asleep across reboots while a Running
   VM's `WantedBy` symlink is untouched. The marker is the authority for the
   Sleeping status.
4. **Park** the VM for a wake-on-TCP (below).

## Capacity accounting

A sleeping VM has released its RAM but still owns its disk LV and its in-jail
`mem.bin`. [server_capacity.py](../atlas/atlas/api/server_capacity.py) therefore
splits the axes: `Sleeping` is **excluded** from the RAM/CPU sums but **included**
in disk. Without this a host with sleeping tenants looks overcommitted and
placement refuses new VMs — defeating the feature.

## Waking on the first inbound TCP connection

Because step 2 above leaves a sleeping VM's `/128` **completely unrouted** (the host
stops answering NDP for it), nothing — not even a SYN — would otherwise reach the
host to trigger a wake. Three pieces close that gap, entirely host-side: **no proxy
change and no inbound-to-Atlas API** (the [satellite](./30-core-service-boundary.md)
read boundary stays read-only). A connection through the TCP/HTTP proxy is just a
dial to the VM's `/128`, so it is trapped the same way as a direct `ssh [vm]:22`.

### 1. Parked reachability ([park.py](../scripts/lib/atlas/park.py))

On sleep, `park()` restores the **minimum** reachability to trap one SYN, with no
running guest:

- **proxy-NDP** for the `/128` on the uplink — the host keeps answering NDP, so the
  upstream router still delivers the VM's packets here.
- an **off-link `/128` route out a shared dummy** (`atlas-park0`, created at
  bootstrap). Routing off-link (not to a local address) is what makes an inbound
  packet **forwarded** — so it traverses `inet atlas forward` — instead of being
  input-delivered and consumed by the host, the same principle the reserved-IP DNAT
  relies on ([06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)).
- one **forward rule**:
  `ip6 daddr <vm> tcp flags syn / fin,syn,rst,ack counter name wake_<uuid> drop`.
  It matches only a connection-opening TCP **SYN** (SYN set; FIN/RST/ACK clear).
  `tcp flags` implies TCP, so **ICMP (`ping`) and UDP never match** — they fall
  through, are forwarded out the dummy, and are discarded **without waking**
  (the design is "TCP only"). The SYN is **dropped**, not rejected, so the client's
  TCP stack retransmits after its RTO (~1 s) — by then the VM is up and the
  retransmit reaches the live guest. The count lands in a **named** counter (only
  named counters appear in `nft list counters`, the cheap surface the daemon polls);
  the name is a pure function of the UUID, so no map is stored.

### 2. The wake trap ([atlas-wake-trap.py](../scripts/atlas-wake-trap.py))

An always-on host daemon (`atlas-wake-trap.service`, enabled at bootstrap) polls the
`wake_<uuid>` counters about once a second. On the first packet for a still-sleeping
VM it does the local wake — exactly [wake-vm.py](../scripts/wake-vm.py)'s two steps:
remove the `SLEEPING` marker, then `systemctl start` the unit. The started unit's
[vm-network-up.py](../scripts/vm-network-up.py) **unparks first** (removing the rule,
counter, and dummy route) and then rebuilds the real netns, so the retransmitted SYN
reaches the resumed guest. Counter poll (not NFQUEUE/NFLOG) because it needs no new
host dependency and we only have to *detect* the dropped SYN, not deliver it.

At startup — including after a host reboot, where a sleeping VM's unit is suppressed
so `vm-network-up` never runs — the daemon **re-parks** every VM still carrying a
`sleeping` marker, rebuilding `atlas-park0`, NDP, route, and rule from the on-disk
markers + `network.env`, DB-free (the same self-heal pattern as `atlas-pool.service`).

### 3. Adopting the wake into the DB ([virtual_machine.py](../atlas/atlas/doctype/virtual_machine/virtual_machine.py))

The host can't reach Atlas's DB, so a per-minute `reconcile_sleeping_vms` job
(ordered **before** `sleep_idle_vms`) probes each server's sleeping VMs
(`probe-woken-vms` reports whether the marker is gone) and, for each woken VM,
`_adopt_wake()` flips `Sleeping → Running`, stamps `last_started`/`last_traffic_at`
(so the same-tick idle sweep won't re-sleep it), and clears `has_memory_snapshot`.
It takes the **same row lock** `wake()` uses, so an operator wake and a host wake
serialize: whichever commits first flips the status, the other no-ops. DB drift is
at most one minute; the guest is reachable the whole time.

## Interactions

- **Host reboot** — the `SLEEPING` marker survives and suppresses the unit; the
  wake-trap daemon re-parks on boot, so a sleeping VM stays both asleep and
  wake-on-TCP after a reboot.
- **Terminate / stop** — `vm-network-down.py` also `unpark()`s, so a
  `Sleeping → Terminated` cleans the rule/counter/route even though the
  already-stopped unit's `ExecStopPost` won't re-run. `reset-server` sweeps any
  orphan `wake_*` counters; `atlas-park0` is kept as bootstrap floor.
- **Per-VM firewall** ([20-firewall.md](./20-firewall.md)) — the `public_filter`
  chain runs before `forward`, so a SYN to a **blocked** port is dropped before the
  wake rule and does not wake (correct — that port is firewalled); a SYN to an
  **allowed** port falls through to the wake rule and wakes.
- **Migration** ([24-vm-migration.md](./24-vm-migration.md)) — migrating from
  `Sleeping` is unsupported; wake or stop first.

## Files

Controller: `sleep()` / `wake()` / `start()` / the schedulers +
`reconcile_sleeping_vms` / `_adopt_wake`
([virtual_machine.py](../atlas/atlas/doctype/virtual_machine/virtual_machine.py)),
[hooks.py](../atlas/hooks.py), [server_capacity.py](../atlas/atlas/api/server_capacity.py).
Host: [sleep-vm.py](../scripts/sleep-vm.py), [wake-vm.py](../scripts/wake-vm.py),
[poll-vm-traffic.py](../scripts/poll-vm-traffic.py),
[probe-woken-vms.py](../scripts/probe-woken-vms.py),
[atlas-wake-trap.py](../scripts/atlas-wake-trap.py),
[park.py](../scripts/lib/atlas/park.py), and the park/unpark hooks in
[vm-network-up.py](../scripts/vm-network-up.py) /
[vm-network-down.py](../scripts/vm-network-down.py). Bootstrap creates `atlas-park0`
and enables `atlas-wake-trap.service` ([bootstrap-server.py](../scripts/bootstrap-server.py)).
