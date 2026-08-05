# Sleepy VMs — Feature Plan (revised)

**Goal:** Free host RAM by automatically pausing idle VMs. An idle VM captures its
memory state (the existing `memory_snapshot_on_stop` fast path), frees its RAM on the
host, and resumes in milliseconds when the first connection arrives. The satellite proxy
holds the incoming TCP connection, calls Atlas `wake()`, and forwards once the VM is
Running.

---

## Design decisions (locked)

| Question | Answer |
|---|---|
| Activity signal | nftables byte counters on the host (all traffic, not just HTTP) |
| Snapshot type | Ephemeral in-jail memory snapshot (`snapshot-stop-vm.py` path) |
| Wake trigger | Manual — operator clicks **Start** (fast resume from snapshot); proxy auto-wake is a follow-on |
| State model | New `Sleeping` status + per-VM `sleep_on_idle` / `idle_timeout_seconds` fields |
| Host-reboot behaviour | Sleeping VMs must **not** auto-restart — enforced via a `SLEEPING` marker file + `ConditionPathNotExists` in the systemd unit template (not `systemctl disable`, which has backwards-compat hazards and enable/disable atomicity issues) |
| `wake()` execution model | Synchronous (blocking SSH Task), exactly like `start()` and `stop()` |
| Proxy integration | Deferred — satellite auto-wake on first connection is a separate follow-on feature |

---

## 1. New fields on `Virtual Machine`

| Field | Type | Default | Notes |
|---|---|---|---|
| `sleep_on_idle` | Check | `0` (off) | Opt-in per VM, like `memory_snapshot_on_stop` |
| `idle_timeout_seconds` | Int | `300` | Must be ≥ 120 (enforced in `validate()` — two poll cycles is the minimum safe window) |
| `last_traffic_at` | Datetime | null | Stamped by the nftables poller; seeded to `now()` at provision and at every `start()`/`wake()` success |

No raw counter fields on the VM row. The poller returns `active: bool` per VM (whether
counters grew since last poll), not raw bytes, so there is nothing to store in Frappe.
This keeps volatile host state off the durable VM record. See §3 `poll-vm-traffic.py`.

---

## 2. New VM status: `Sleeping`

`"Sleeping"` must be added to the `status` field's `options` in the `Virtual Machine`
DocType JSON (the Frappe validation source of truth) **and** to the Python
`DF.Literal[...]` annotation in `virtual_machine.py`. Omitting the DocType JSON update
would cause a `ValidationError` on every `sleep()` save and create permanent status
drift (host sleeping, DB says Running).

Full state machine (additions marked **NEW**):

```
Pending  → Running    (provision)
Running  → Paused     (pause)
Paused   → Running    (resume)
Running  → Stopped    (stop — deliberate)
Paused   → Stopped    (stop — deliberate)
Running  → Sleeping   (sleep — auto, idle timeout)             ← NEW
Sleeping → Running    (wake — proxy or explicit Start/wake)    ← NEW
Sleeping → Stopped    (stop — operator explicitly stops a sleeping VM)  ← NEW
Sleeping → Terminated (terminate)                              ← NEW
{any}    → Failed / Terminated  (unchanged)
```

Status checks use the open-set rule (guard on what is allowed, not "anything but X"):
- `sleep()` accepts only `Running`
- `wake()` accepts only `Sleeping`
- `stop()` accepts `Running | Paused | Sleeping`
- `terminate()` accepts any non-Terminated state (unchanged)
- `pause()`, `resume()` — unchanged; neither accepts `Sleeping`

### Lifecycle operation guards on `Sleeping`

| Operation | Sleeping VM behaviour |
|---|---|
| `pause()` | Rejected — no live vCPUs to freeze |
| `snapshot()` | Rejected — wake to `Stopped` first (see §10) |
| `rebuild()`, `restore()`, `resize()`, `regenerate_host_keys()` | Rejected — same "must be Stopped" rule; operator must stop the VM first |
| `terminate()` | Allowed; `terminate-vm.py`'s `rm -rf` sweeps the jail including in-jail snapshot files |
| `stop()` | `Sleeping → Stopped`: removes in-jail snapshot files and the `SLEEPING` marker without starting the VM |
| `capture_warm_snapshot()` | Rejected — requires a live guest |
| `start()` | Delegates to `wake()` when `status == "Sleeping"` (operator convenience; desk **Start** button just works) |

`snapshot()` and disk operations deliberately require an explicit stop (`Sleeping → Stopped`)
rather than silently consuming the memory snapshot. Auto-consuming would (a) destroy the
fast-resume capability without the operator asking to, and (b) add hidden multi-phase
complexity inside a single method call. The operator or Central must call `stop()` first;
the controller error message says so explicitly.

---

## 3. New and changed scripts

### `sleep-vm.py` (new)

Shares core snapshot logic with `snapshot-stop-vm.py` via the shared library
(`scripts/lib/atlas/`). Does not shell out to `snapshot-stop-vm.py` — duplicating the
invocation chain is wrong because the outer script's `ATLAS_RESULT=` line would conflict.
Instead, the shared snapshot helper lives in `scripts/lib/atlas/snapshot.py` (or is
inlined if the library is thin enough).

Sequence (the `systemctl stop` precedes the marker write):

1. Pre-flight (disk space: `memory_megabytes + 256 MiB` free on host FS; launcher
   check; API socket present).
2. Pause vCPUs via Firecracker API socket.
3. `PUT /snapshot/create` — write `jail/snapshot/{vmstate.bin, mem.bin}`.
4. Write `jail/snapshot/READY` marker — only after both files are confirmed on disk.
5. `systemctl stop firecracker-vm@<uuid>.service`.
6. Write the `SLEEPING` marker file: `/var/lib/atlas/virtual-machines/<uuid>/sleeping`
   — written **after** the unit is stopped (host-file write, not an SSH sub-command).

**Fallback** (any step 1–4 fails): fall back to plain `systemctl stop` (same as
`snapshot-stop-vm.py`'s fallback). Emit `memory_snapshot=false` + reason. Write the
`SLEEPING` marker anyway — the VM lands `Sleeping` without a snapshot; the next `wake()`
cold-boots it. Emit `memory_snapshot_bytes=0` in that case. The `SLEEPING` marker is
always written on success (step 6) regardless of whether a snapshot was captured.

**On any failure of the stop itself (step 5)**: exit non-zero. The VM stays Running.
The SLEEPING marker is NOT written. The controller keeps `status = "Running"`.

Emits `ATLAS_RESULT={"memory_snapshot": bool, "reason": "...", "memory_snapshot_bytes": N}`.

### `wake-vm.py` (new)

1. Remove the `SLEEPING` marker file: `rm -f /var/lib/atlas/virtual-machines/<uuid>/sleeping`.
2. `systemctl start firecracker-vm@<uuid>.service`.
3. The per-VM launcher detects `jail/snapshot/READY` (if present) → idle Firecracker
   start → `vm-restore.py` loads snapshot, consumes READY marker, resumes. No marker →
   plain cold boot. Self-healing fallback as documented in spec/05.
4. Poll `systemctl is-active` until active or timeout.

Step 1 (marker removal) happens **before** `systemctl start` so that a host reboot
between steps 1 and 2 leaves no sleeping marker and the unit starts normally. This is
the safer ordering.

Idempotent: if the SLEEPING marker is already absent (second concurrent call), the
`rm -f` is a no-op. `systemctl start` on an already-starting unit is also idempotent.

### `systemd/firecracker-vm@.service` (amended)

Add one directive to `[Unit]`:

```ini
ConditionPathNotExists=/var/lib/atlas/virtual-machines/%i/sleeping
```

This causes systemd to skip starting the unit on host reboot if the `sleeping` marker
exists — the VM stays sleeping across reboots without any `systemctl disable`, preserving
the WantedBy symlink for Running VMs. The unit is never disabled; only the condition
gate prevents the unwanted start. A sleeping VM that missed its snapshot (fallback path)
similarly stays put — the `SLEEPING` marker is always written.

This approach has no backwards-compat hazard with old-style non-jailed VMs because
`ConditionPathNotExists` is additive to the unit template and is path-based, not
unit-state-based. Old VMs that never have a `sleeping` marker file will pass the
condition on every boot.

### `poll-vm-traffic.py` (new, server-scoped Task)

This is a **server-level** Task (attached to the `Server` DocType via `run_task(...,
server=name, script="poll-vm-traffic", variables={"vm_uuids": [...]})`), not a VM-level
Task. The Task row's `virtual_machine` FK is left blank; the `server` FK is set. This
preserves the one-audit-row-per-server-operation contract without forcing N SSH Tasks
for N VMs.

The script reads nftables byte counters for specified VM UUIDs. Interface name mapping:
each VM's host-side veth is `atlas-h<uuid.hex[:7]>` (derived in the same way as the
tap device derivation in `networking.py`). The script:

1. Runs `nft -j list table inet atlas` to get the full JSON ruleset.
2. For each requested UUID, computes the veth name `atlas-h{uuid.replace("-","")[:7]}`.
3. Filters rules where `expr[*].match.left.payload.name == veth_name` (the iifname
   match on the forward chain rule that carries the counter).
4. Sums `expr[*].counter.bytes` across the matching rule's expressions.

Output: `ATLAS_RESULT={"counters": {"<uuid>": {"active": bool}, ...}}` where `active=true`
if the counter value (as read now) differs from the previous poll's value stored in a
per-VM file on the host at `/var/lib/atlas/virtual-machines/<uuid>/last-counter` — the
delta comparison lives **on the host** inside `poll-vm-traffic.py`, not in the controller.
This eliminates raw counter fields from the Frappe DB entirely: the script writes the
last-seen counter to the host-side file and returns only the boolean.

**Counter reset handling**: if the new counter value is less than the stored last value
(rule reload or host reboot reset counters to zero), the script returns `active=true`
for that VM (defensive — treat "we don't know" as "had traffic"). After the reset, the
script writes the new zero-based value as the baseline. This prevents a VM from being
slept immediately after a network rule reload even if it was actively transferring.

The nftables `counter` statement must be added to the per-VM forward rules in
`vm-network-up.py`. The current rule text (approximate):
```
iifname "atlas-h1234567" accept
```
Changes to:
```
iifname "atlas-h1234567" counter accept
```
The idempotency guard in `vm-network-up.py` checks for existing rule text before
inserting; the guard string must change from `"accept"` to `"counter accept"` for the
affected rules. Existing VMs without the counter rule will have it added the next time
`vm-network-up.py` runs for that VM (host reboot, or next start).

---

## 4. Controller methods

### `Virtual Machine.sleep()`

```
Requires: status == "Running"
```
- Checks `stop_protection` **at execution time** (not just at enqueue — the scheduler
  enqueue and the method execution are separate; a flag set after enqueue must also be
  respected). If set, silently return (no throw, no Task) — same as the scheduler skip.
- Runs `sleep-vm.py` SSH Task.
- On Task success: `status = "Sleeping"`, `has_memory_snapshot = <result.memory_snapshot>`.
  `last_traffic_at` is NOT updated (it reflects real traffic, not the sleep event).
- On Task failure: status unchanged (VM stays Running); scheduler retries next cycle.
- If called when `status != "Running"` (scheduler race: VM was stopped/terminated
  between enqueue and execution): **silently return** — do not throw. A race is not an
  error from the scheduler's perspective.

### `Virtual Machine.wake()`

```
Requires: status == "Sleeping"
```
- Acquires a DB-level row lock on the VM before checking status (prevents concurrent
  `wake()` calls from both passing the status check). Use
  `frappe.db.sql("SELECT name FROM `tabVirtual Machine` WHERE name=%s FOR UPDATE", name)`
  before loading the doc, inside the same transaction.
- Runs `wake-vm.py` SSH Task **synchronously** (blocking, like `start()`/`stop()` —
  `run_task` is not enqueued).
- On Task success: `status = "Running"`, `last_started = now()`,
  `last_traffic_at = now()` (prevents immediate re-sleep), `has_memory_snapshot = 0`
  (the READY marker was consumed by `vm-restore.py`).
- On Task failure: status stays `Sleeping`; caller retries.
- Idempotent at the script level (concurrent second call removes an already-absent
  marker and does `systemctl start` on an already-starting unit — both are no-ops).
- No new API surface needed in this iteration; the desk **Start** button routes through
  `start()` → `wake()`. The `wake_vm` satellite API endpoint is part of the proxy
  follow-on (§7).

### `Virtual Machine.stop()` (amended)

Adds handling for `status == "Sleeping"`:
- Runs a new script flag (or a dedicated `discard-sleep-snapshot.py` helper, see below)
  that:
  1. Removes `jail/snapshot/READY`, `vmstate.bin`, `mem.bin` (if present).
  2. Removes the `SLEEPING` marker file.
  3. Does NOT run `systemctl start` — the VM remains stopped on the host.
- On script success: `status = "Stopped"`, `has_memory_snapshot = 0`.

**Implementation decision**: add `--sleeping` flag to `stop-vm.py` that triggers the
discard path before the stop steps. The unit is already stopped (sleep left it stopped);
the script only removes the snapshot files and marker, then exits. This is one SSH Task,
one script, one audit row — consistent with the existing model. The flag makes the
behaviour explicit and preserves idempotence (removing absent files is a no-op).

### `Virtual Machine.start()` (amended)

Add at the top:
```python
if self.status == "Sleeping":
    return self.wake()
```

This makes the desk **Start** button work from Sleeping without operator confusion.

### `Virtual Machine.provision()` and `Virtual Machine.start()` (amended)

Both must set `last_traffic_at = now()` on success, so a freshly started VM is not
immediately eligible for auto-sleep before the first poll cycle.

---

## 5. Scheduled jobs

### `poll_vm_traffic` — every 60 seconds

```python
# Group sleep_on_idle VMs by server
servers_with_sleepy_vms = frappe.get_all(
    "Virtual Machine",
    filters={"sleep_on_idle": 1, "status": "Running"},
    fields=["server", "name"],
)
for server, vms in group_by_server(servers_with_sleepy_vms):
    # Deduplicate: skip if a poll Task is already pending/running for this server
    if existing_task(server=server, script="poll-vm-traffic", status_in=("Pending", "Running")):
        continue
    uuids = [vm.name for vm in vms]
    run_task(server=server, script="poll-vm-traffic", variables={"vm_uuids": uuids})
    # parse result and update last_traffic_at
    for uuid, info in result["counters"].items():
        if info["active"]:
            frappe.db.set_value("Virtual Machine", uuid, "last_traffic_at", now())
```

Poll Tasks are attached to the Server row (`virtual_machine` FK is blank). They appear
in the Task list for the Server but not in the VM's Task history, keeping VM audit logs
clean.

Poll Tasks should be marked with a `task_type = "Poll"` (a new Literal option on the
Task DocType) or have `is_hidden = 1` so they are filtered from the default operator
Task list view. This prevents 1,440 poll rows/server/day from flooding the audit log.

### `sleep_idle_vms` — every 60 seconds

```python
# Compute the deadline: VMs with last_traffic_at older than idle_timeout
# SQL NULL < timestamp is NULL (falsy), so VMs that were never polled are skipped safely.
candidates = frappe.get_all(
    "Virtual Machine",
    filters={
        "sleep_on_idle": 1,
        "status": "Running",
        "last_traffic_at": ("<", add_to_date(now(), seconds=-idle_timeout_seconds)),
    },
    fields=["name", "idle_timeout_seconds"],
)
for vm in candidates:
    # Deduplicate: skip if a sleep Task is already pending/running for this VM
    if existing_task(vm=vm.name, script="sleep-vm", status_in=("Pending", "Running")):
        continue
    frappe.enqueue(
        "atlas.atlas.doctype.virtual_machine.virtual_machine.sleep",
        name=vm.name,
        queue="long",
    )
```

**Note on `last_traffic_at = null`**: SQL `NULL < <timestamp>` evaluates to NULL (not
true), so a VM that has never been polled is **excluded** from the candidate set. This
is the safe default: a new VM is not auto-slept before the first poll cycle. The
`provision()` stamp of `last_traffic_at = now()` reinforces this. If `provision()` is
not amended, the NULL exclusion is the safety net.

**Note on per-VM `idle_timeout_seconds`**: the `candidates` query uses a fixed global
cutoff timestamp. The correct approach is either (a) filter with a per-VM comparison
(requires a SQL expression, not a simple filter dict), or (b) post-filter in Python after
the query. Post-filtering is simpler and correct for small candidate sets.

---

## 6. nftables counter integration

See §3 `poll-vm-traffic.py` for the full specification including the idempotency guard
string change in `vm-network-up.py`.

---

## 7. Wake on the first inbound TCP connection — SHIPPED (host-level, not the proxy)

The original "hold the TCP connection in the satellite proxy, call Atlas, forward on
resume" follow-on was **superseded by a host-level trap** that needs no proxy change and
no inbound-to-Atlas API. It is now implemented; see
[spec/31-sleepy-vms.md](../../spec/31-sleepy-vms.md) for the full mechanism.

Because a proxy-forwarded connection is just a TCP dial to the VM's `/128`, a host-side
trap catches **both** proxy traffic and a direct `ssh [vm]:22`, so the proxy stays a dumb
pipe. On sleep, `park()` keeps the `/128` reachable — proxy-NDP + an off-link `/128` route
out a shared `atlas-park0` dummy (so an inbound packet is forwarded through `inet atlas
forward`) + one rule `ip6 daddr <vm> tcp flags syn / fin,syn,rst,ack counter name
wake_<uuid> drop`. The always-on `atlas-wake-trap` daemon polls those named counters and,
on the first SYN to a still-sleeping VM, does the local wake (remove marker + `systemctl
start`, which unparks then rebuilds the real net). `reconcile_sleeping_vms` mirrors the
host-initiated wake back into the `Sleeping → Running` status, row-lock-safe against
`wake()`. TCP only — ICMP/UDP fall through and never wake.

---

## 8. Capacity accounting

`server_capacity.py` must split the RAM and disk axes for `Sleeping` VMs:

- **RAM contribution: 0** — sleeping VMs free their host RAM (the cgroup is released).
  Exclude `Sleeping` from the RAM sum:
  ```python
  # Before
  filters={"server": server, "status": ["not in", ("Terminated",)]}
  # After
  ram_filters={"server": server, "status": ["not in", ("Terminated", "Sleeping")]}
  disk_filters={"server": server, "status": ["not in", ("Terminated",)]}
  ```
- **Disk contribution: unchanged** — the disk LV still exists; the in-jail `mem.bin`
  occupies host FS (outside the LVM pool). No change to the disk axis.

This is the core correctness requirement: without it, a host with 10 sleeping 4 GB VMs
appears 40 GB overcommitted and placement refuses new VMs — defeating the feature.

The `refresh_capacity` Task on the `Server` form already re-measures capacity; it will
pick up sleeping VMs correctly once the query filter is amended.

---

## 9. Interaction with existing features

| Feature | Impact |
|---|---|
| `stop_protection` | `sleep()` silently skips if set, checked at execution time (not only at enqueue) |
| `termination_protection` | No change |
| `memory_snapshot_on_stop` | Orthogonal flag; a VM with both uses the `sleep-vm.py` path for idle auto-sleep and `snapshot-stop-vm.py` for deliberate stops |
| Host reboot | `ConditionPathNotExists=.../sleeping` in the unit template prevents sleeping VMs from auto-starting. The `SLEEPING` marker survives reboots. Frappe DB stays authoritative. |
| VM migration (spec/24) | `preflight_checks` must add `"Sleeping"` to the invalid-state list (with a clear error: "Wake or stop the VM before migrating"). Migration from Sleeping is not supported. |
| Snapshot backup to S3 (spec/29) | The in-jail snapshot is not a `Virtual Machine Snapshot` row and is not S3-backable. Cold disk snapshots are unaffected. |
| Fake provider | `fake_tasks.py` adds: `sleep-vm` (emit `memory_snapshot=true`, write fake SLEEPING marker), `wake-vm` (emit success, remove fake marker), `poll-vm-traffic` (return `active: false` for all VMs — tests that want to trigger sleep call `vm.sleep()` directly). |
| Placement | New VMs can be placed on hosts with sleeping VMs now that capacity accounting correctly counts sleeping RAM as free. |

---

## 10. State machine guard changes

All amended guards:

| Method | Before | After |
|---|---|---|
| `stop()` | `Running \| Paused` | `Running \| Paused \| Sleeping` |
| `snapshot()` | `Stopped` | `Stopped` (unchanged — Sleeping explicitly rejected with message "Stop the VM first") |
| `rebuild()` / `restore()` / `resize()` / `regenerate_host_keys()` | `Stopped` | `Stopped` (unchanged — Sleeping explicitly rejected) |
| `pause()` | `Running` | `Running` (unchanged — Sleeping rejected) |
| `start()` | `Stopped` | `Stopped \| Sleeping` (Sleeping delegates to `wake()`) |

---

## 11. Validate constraints

Add to `Virtual Machine.validate()`:

```python
if self.sleep_on_idle and self.idle_timeout_seconds < 120:
    frappe.throw("Idle timeout must be at least 120 seconds (two poll cycles).")
```

---

## 12. Desk UI

- VM status badge: `Sleeping` shown distinctly.
- `sleep_on_idle` and `idle_timeout_seconds` fields on the VM form, same tab as
  `memory_snapshot_on_stop`.
- `last_traffic_at` shown as a read-only informational field.
- **Start** button wakes sleeping VMs (via the `start()` → `wake()` delegation).
- No new explicit **Wake** button needed; the desk's existing **Start** just works.

---

## 13. New spec document

`spec/31-sleepy-vms.md` — covers:
- State machine additions and the `SLEEPING` marker file pattern
- The nftables polling design (server-scoped Task, host-side delta, counter reset handling)
- Capacity accounting split (RAM vs. disk)
- Satellite wake protocol (synchronous `wake_vm` call + no polling loop)
- Host reboot behaviour (`ConditionPathNotExists`)
- Interaction with existing features (migration, stop_protection, cold-boot fallback)
- Clock-skew caveat on wake (same as existing memory snapshot docs in spec/05)
- Disk space accounting for the in-jail `mem.bin` file

Add a row to spec/README.md operator use-case table:

| Use case | Operator action | Spec |
|---|---|---|
| Auto-sleep idle VMs | Enable `sleep_on_idle` + set `idle_timeout_seconds` on the VM form | 31-sleepy-vms.md |

---

## 14. New e2e use case

`atlas/tests/e2e/use_cases/sleepy_vms.py`:
1. Enable `sleep_on_idle` on a Running VM.
2. Call `vm.sleep()` directly (bypasses idle timer).
3. Assert `status == "Sleeping"`, `SLEEPING` marker exists on host, systemd unit
   condition is unsatisfied (`systemctl show --property=ConditionResult` → `no`).
4. Assert host RAM freed (cgroup no longer active — `systemctl is-active` → `inactive`).
5. Call `vm.wake()`.
6. Assert `status == "Running"`, VM reachable over SSH, `last_started` updated,
   `last_traffic_at` updated (not stale pre-sleep value).
7. Assert `SLEEPING` marker is gone, unit is active.
8. Run `poll-vm-traffic.py` Task; assert the poll Task is server-scoped (no VM FK).
9. Unit test: `idle_timeout_seconds = 60` fails `validate()`.

---

## Open questions / deferred

- **Per-VM idle timeout in the scheduler query.** The `sleep_idle_vms` query uses a
  fixed cutoff. Supporting per-VM `idle_timeout_seconds` in the SQL filter requires a
  raw query or post-filtering; the plan describes post-filtering as the simpler path.

- **Wake on inbound connection.** SHIPPED as a host-level TCP-SYN trap (§7,
  spec/31-sleepy-vms.md), superseding the deferred satellite `wake_vm()` design — no
  proxy change and no inbound-to-Atlas API. Covers both proxy-forwarded traffic (a dial
  to the `/128`) and direct SSH to the VM. TCP only: ICMP/UDP do not wake.

- **Poll Task audit log pruning.** ~1,440 poll Task rows/server/day. A scheduled reaper
  for old poll Tasks (`task_type="Poll"`, older than 7 days) is a near-term follow-up
  before production load.

- **Overlapping poll Tasks.** The deduplication check prevents double-running, but a
  hanging poll Task means VMs on that server are not polled for that cycle — at most
  60 s extra idle time. The stuck-task reaper (spec/09 roadmap) is the backstop.
