# Boat — the per-host daemon, and what Atlas keeps

> **Status: design, spec-first.** No Boat code ships against this chapter until
> §11's six correctness invariants have been written here and reviewed. That
> gate is why the chapter exists before the daemon does. The repository is
> `github.com/frappe/boat`; the contract IDL is `api/openapi.yaml` in that
> repo and **this chapter governs it** — the Boat repo's README points here.
> Delivery order is §15.

## The split

Atlas today is a smart Frappe/Python controller driving dumb hosts. Every host
mutation is one SSH invocation of a staged idempotent Python verb whose result is
scraped off an `ATLAS_RESULT=` line ([04-tasks.md](./04-tasks.md)), and the Frappe
DB is declared the source of truth with the host "a rebuildable cache"
([01-architecture.md](./01-architecture.md)).

That is no longer what the fleet does. **The host already decides** — §0. What it
lacks is a store, an API, and a name. This chapter gives it all three and draws
the line.

- **Atlas** stays the regional **control plane**: fleet-aggregate state,
  placement, vendor and Central APIs, cross-host coordination, service
  installation. It decides **intent**.
- **Boat** is a **native-Go daemon set**, one per host, that owns every VM
  operation (lifecycle, snapshots, resize, migration execution) and host/VM
  networking, and is **the source of truth for that host's observed bare-metal
  state**. It knows a VM only as a UUID plus resource numbers.

The dividing rule, which settles every "does this belong in Boat?" argument:

> **Boat may decide any question answerable from (VM UUID) + (host-local facts:
> nft counters, on-disk markers, `host_signature`, free RAM, launcher support).
> Boat may NOT decide any question whose answer would differ based on who owns
> the VM, what runs in it, or what it costs.**
>
> Boat owns **realization and reflex**. Atlas owns **enrolment and intent**.

Two worked consequences, because the rule is only useful if it cuts:

- **Sleep-on-idle and wake-on-inbound-SYN are pure mechanics.** The *reflex* —
  poll the counter, drop the marker, start the unit — is Boat's. The *enrolment*
  — `sleep_on_idle`, `idle_timeout_seconds`, the per-VM firewall map — is Atlas
  policy handed down ([32-sleepy-vms.md](./32-sleepy-vms.md)).
- **Capacity self-report stops at raw numbers.** Boat reports free cores, free
  RAM, pool bytes. The Sleeping-axis accounting that excludes a sleeping VM from
  the RAM sum but keeps it in the disk sum
  ([`server_capacity.py`](../atlas/atlas/api/server_capacity.py),
  [28-placement.md](./28-placement.md)) stays in Atlas — otherwise "Sleeping is
  billing-relevant" leaks into a daemon that must not know what billing is.

A third tier sits above both and is unaffected: **Atlas-services** — proxy,
gateway, bench-site, TLS — a capability attached to a VM *after* it exists,
driven over a plane Atlas owns (§7). Core VMs and Boats are oblivious to it.

## §0. The precedent — this is the third resident daemon, not the first

Read this before contradicting anything. Two shipped chapters already overturned
"no agent runs on the server," and Boat claims them as precedent rather than
re-arguing the point.

**ANCP** ([31-ancp-network-control-plane.md](./31-ancp-network-control-plane.md),
shipped) put an authoritative gossip daemon on every host and *deleted* the
controller's networking module (`atlas/atlas/host_mesh.py`, `reconcile_host_mesh`,
`sequenced_migration_cutover`). Three consequences bind Boat:

1. **The mesh is not in the Atlas ↔ Boat contract at all.** Any statement of the
   form "Atlas computes mesh peers and Boat applies them" is wrong.
2. **The VM ↔ network seam already exists and is a file.** ANCP §11.3 defines
   `/etc/atlas-networkd/local-ownership.json`, written atomically by
   `vm-network-up` / `vm-network-down`, read by the daemon, with deliberately no
   Frappe fallback. Boat simply becomes that file's writer. Zero rework of
   shipped ANCP code.
3. **The Taste exception is already granted.** ANCP §6 documents in the spec that
   a long-running host daemon is a sanctioned deviation from "one operation = one
   script = one Task row" and "no agent runs on the server"
   ([README](./README.md) principle 5). Boat inherits it.

**Sleepy VMs** ([32-sleepy-vms.md](./32-sleepy-vms.md), shipped) put a resident
wake-trap daemon on every host that decides, with no DB consult, when a VM comes
back to life: it polls `wake_<uuid>` nft counters once a second, and the first
inbound TCP SYN to a parked `/128` wakes the VM. It re-sweeps park state from
on-disk markers at boot, DB-free. This is the purest existing instance of the
dividing rule, and Boat absorbs it natively.

Underneath both, `firecracker-vm@.service` already rebuilds netns, routes, nft
and disk from an on-disk `network.env` after a reboot, and `atlas-pool.service`
rebinds the loopback PV. Neither consults Atlas.

**What that leaves for Boat:** VM lifecycle, per-VM networking, observed state,
adoption, crash recovery, cross-host op execution, and supervision of the sibling
units.

## THE RULE

**Every host-side service is written in Go, and every one of them is a separate
systemd service invoked through the same `boat` binary.** Separate units,
separate processes, **one build artifact** — the busybox model.

```
ExecStart=/usr/local/bin/boat daemon                # the API daemon + reconciler
ExecStart=/usr/local/bin/boat networkd              # was atlas-networkd.service
ExecStart=/usr/local/bin/boat pool                  # was atlas-pool.service
ExecStart=/usr/local/bin/boat wake-trap             # was atlas-wake-trap.service
ExecStart=/usr/local/bin/boat gateway               # was gateway.service
ExecStart=/usr/local/bin/boat mgmt-firewall         # was atlas-mgmt-firewall.service
ExecStartPre=/usr/local/bin/boat vm-network-up %i   # firecracker-vm@ hooks
```

This is **not a new grammar** — it is the one the host already has.
[`_cli.py`](../scripts/lib/atlas/_cli.py) is already a multi-call dispatcher,
[`install.sh`](../scripts/install.sh) already symlinks `/usr/local/bin/atlas` to
it, and `scripts_catalog.py` already models a Task as a verb run on the host.
`/usr/local/bin/boat` replaces that symlink. The scope is therefore the **whole
host surface**: every `scripts/*.py` verb, every `scripts/lib/atlas/` module,
every `networkd/` module, and every unit in `scripts/systemd/`.

**What it buys.** Two bug classes deleted, not two conveniences:

- The **durable-package staleness class**, outright. No `/var/lib/atlas/bin`
  module shadowing, no venv, no `sys.path.insert` shim, no stale-staged-package
  purge, no "re-bootstrap to refresh the lib" contract
  ([04-tasks.md § the shared `atlas` package](./04-tasks.md)).
- **Version skew between a host's components**, because every unit is literally
  the same build. Today the host has five invocation styles — inline `python -c`,
  durable `python /var/lib/atlas/bin/x.py %i`, the `atlas` console script, raw
  `nft -f`, and `jailer-launch.sh` — each with its own refresh path and its own
  way of being stale. The rule collapses them to one.

**What it costs, and this is the hard part.** One binary swap re-points *every*
unit at once. There is no partial-version window, which is the benefit, and no
partial-version *escape*, which is the cost: an update restarts the daemon under
live VMs. **This is precisely why self-update is hard-gated on re-attaching to a
running Firecracker rather than restarting it** (§3.3, §5). Auto-update cannot
ship before re-attach does.

## §1. Source of truth — desired versus observed

Atlas remains authoritative for **desired state** (intent). Boat becomes
authoritative for **observed state** (fact).

**This refines [01-architecture.md](./01-architecture.md) principle #2; it does
not reverse it.** A lost host is still rebuildable *from desired state* — that
property is untouched, and it is the whole content of "the host is a rebuildable
cache." What changes is the reverse direction: Atlas now keeps a **read-through
mirror** of Boat's observed state, and that mirror is **disposable and never
authority**. Losing the mirror costs a `GET /v1/export` round trip (§2.5).
Nothing is ever rebuilt *from* the mirror; nothing is decided by it that a CAS
verb (§11.2) does not re-check against the host.

| Field / fact | Class | Authority | Reconciliation |
|---|---|---|---|
| `name` (UUID), `title`, `tenant`, `image`, `ssh_public_key` | Desired (identity) | Atlas | Immutable; Boat receives, never mutates |
| `server` (placement) | Desired | Atlas | CAS-gated; only migration cutover repoints (§8) |
| `vcpus`, `cpu_max_cores`, `cpu_mode`, `memory_megabytes`, `disk_gigabytes`, `data_disk_gigabytes` | Desired (spec) | Atlas | Boat applies; reports `observed_*` back |
| **`desired_power`** ∈ {Running, Stopped} | **Desired** | **Atlas** | The only input to Boat's power reconciler (§11.3) |
| **`observed_status`** | **Observed** | **Boat** | Replaces today's "status set from Task success" |
| `sleep_on_idle`, `idle_timeout_seconds` | Desired (enrolment) | Atlas | Boat runs the reflex, never chooses the policy |
| `ipv6_address` | Desired-binding | Atlas (v1); Boat later (§6.2) | CAS-gated; union-reconciliation law (§11.4) |
| `mac_address`, `tap_device`, `private_address` | Derived (pure fn of UUID / tenant) | Either (deterministic) | [`networking.py`](../atlas/atlas/networking.py) `derive_*`, recomputed identically on both sides |
| `has_memory_snapshot`, `last_started` / `last_stopped`, `last_traffic_at`, `boot_id` | **Observed** | **Boat** | Marker- and counter-driven |
| `public_ipv4` (reserved-IP attach) | Desired-binding | Atlas (vendor alloc) → Boat (NAT apply) | CAS-gated on the host's reserved-IP slot (§11.5) |
| LV inventory, real sizes, thin-pool fullness | **Observed** | **Boat** | Feeds placement |
| `vcpus_total`, `memory_megabytes_total`, `pool_disk_gigabytes_total` | **Observed** (host facts) | **Boat** | A live fact, not a bootstrap snapshot |
| Running firecracker PID / API socket | **Observed** | **Boat** | Boat re-attaches on restart (§3.3) |
| **`boat_version` (running binary)** | **Observed** | **Boat** | In every export (§2.5); drift against desired drives §5 |
| `boat_version` (desired) | Desired | Atlas | `Server.boat_version`; Atlas pushes, staggered (§5) |
| `boot_epoch` (per-UUID fence) | Control | **Atlas issues**, Boat mirrors and enforces | §11.1 |

The observed status vocabulary is Running / Stopped / Paused / Sleeping /
Failed, plus **Unknown** when Boat cannot read the host. Boat derives it from the
unit's `ActiveState`/`SubState` and the on-disk markers — **never** from a
command having succeeded.

**The drift guard, reworked.** Today
[`virtual_machine.py`](../atlas/atlas/doctype/virtual_machine/virtual_machine.py)
`validate()` freezes the resource fields (`RESIZE_MUTABLE`) because Atlas has no
truthful readback. With Boat owning observed state, drift (`desired ≠ observed`)
becomes a **surfaced state** on the observation fields: Atlas's reconcile flags
it, Boat's reconciler drives observed toward desired. **But drift is corruption,
not a display nuisance, for contended reservations** — `server`, `ipv6_address`,
the reserved-IP slot, the capacity gate. Those stay
**frozen-except-through-CAS-verbs** (§11.2). Identity immutability
(`IMMUTABLE_AFTER_INSERT`) is unchanged, and Boat keys its store on the same
UUID.

Two flags carry the transition, both additive and both reversible:
`Server.boat_enabled` (Check, default 0) gates whether Atlas ever calls a Boat at
all, and `Virtual Machine.observed_authority` ∈ {DB, Boat} (default DB) gates
per-VM whether Boat's observation wins. A Fake-backed server
([01-architecture.md](./01-architecture.md)) never gets a Boat call: `BoatClient`
honours `is_fake_server()` exactly as `run_task` does.

## §2. The Atlas ↔ Boat contract

### 2.1 The API is the complete functional surface

**Every capability Boat has is an endpoint.** Lifecycle, bootstrap (§4),
self-update (§5), sibling-unit supervision, host facts, whole-host export. The
`boat` CLI and the systemd units are **clients of that same surface** — never
alternate paths with powers the API lacks.

This is what makes Atlas able to drive a host completely, and what makes the CLI
a truthful break-glass tool rather than a second implementation that drifts. A
capability reachable only from the CLI is a capability Atlas cannot audit, cannot
replay, and cannot recover after a partition.

### 2.2 Transport — HTTP/JSON over the management tunnel, typed by an IDL

- **HTTP/1.1 + JSON on the wire, SSE for streams.** Not gRPC: Atlas already
  speaks HTTP/JSON to DigitalOcean, Scaleway, S3 and Central through `requests`,
  and `grpcio` would violate the few-dependencies principle and become every
  downstream app's dependency. Boat's Atlas-side client has the shape of
  [`digitalocean.py`](../atlas/atlas/digitalocean.py).
- **A real IDL.** An OpenAPI 3 document at `api/openapi.yaml` in the Boat repo is
  the source of truth; the typed Go server and the typed Python client are
  **generated from it at build time and checked in** — zero new runtime
  dependency. This carries the typing discipline `TaskInputs`/`TaskResult`
  already give ([04-tasks.md](./04-tasks.md)) across the language boundary.
  Explicit `/v{N}` path versioning.
- **Boat listens only on the management-tunnel address and `/run/boat/boat.sock`,
  never on a public interface.** The tunnel is the transport-security boundary,
  exactly as it is for Central ↔ Atlas ([21-tunnel.md](./21-tunnel.md)).
- **Boat shares the Central-managed tunnel. There is no second tunnel.** A
  tunnel re-provision is therefore a handled event, not an outage: exponential
  backoff on reconnect, observed reports buffered across the blip, no operation
  lost.

### 2.3 Auth and least privilege

Summarised here, specified in §12: non-root daemon under a pinned sudoers
allow-list; the verb allow-list enforced at the API boundary; short-lived
per-host tokens minted by Atlas. Requests over the unix socket are authenticated
by unix peer credentials instead — the socket is `0660`, group-owned by the
service user. `GET /v1/health` is the one unauthenticated operation, so a
supervisor can probe a Boat that has not yet been handed a token; it reports
nothing an unauthenticated caller should not see.

### 2.4 The operation set

All operations are idempotent, replayable and versioned. Every mutating request
carries an `op_id`.

- **A. Desired-state apply — the durable primitive.** `PUT /v1/vms/{uuid}` with
  the full desired spec and `boot_epoch`. Boat diffs against its store and runs
  forward to converge. This is how Atlas re-asserts intent on reconnect.
- **B. Lifecycle verbs.** `POST /v1/vms/{uuid}/<verb>` for `start`, `stop`,
  `pause`, `resume`, `sleep`, `wake`, `resize`, `snapshot`, `warm-snapshot`,
  `rebuild`, `terminate` and `reserved-ip`; plus `POST /v1/images/sync` and the
  migration sub-operations of §8. **A verb
  mutates desired state and lets the reconciler act; verbs never touch the host
  directly** (§11.3).
- **C. Observed read and watch.** `GET /v1/vms/{uuid}`, `GET /v1/vms`,
  `GET /v1/host`, `GET /v1/export` (§2.5), `GET /v1/watch` (SSE deltas and
  operation progress). CAS reads carry `If-Match: <observed-epoch>` (§11.2).
- **D. Host control.** `POST /v1/bootstrap` (§4), `POST /v1/update` (§5),
  `GET|POST /v1/units/{name}` (sibling-unit supervision).

The verb grammar is **identical to the host CLI's today** — `atlas <verb>
--kebab-flags`, one `ATLAS_RESULT=` line out. `boat` is that CLI's Go successor
with the same grammar, so [`scripts_catalog.py`](../atlas/atlas/scripts_catalog.py),
`_variables_to_flags` and `task_results.parse_result` survive the port unchanged
on the Atlas side.

### 2.5 Whole-host export and one-shot sync

`GET /v1/export` returns Boat's **entire** observed state in one document: every
VM's observed doc, host facts, LV and thin-pool inventory, network state,
sibling-unit liveness, every fence epoch, and the running `boat_version`. Atlas
ingests it **in one transaction** to rebuild its mirror.

**State the symmetry explicitly:** `PUT` desired is how Atlas re-asserts intent;
`GET /v1/export` is how Boat re-asserts fact. Those two calls, run back to back,
fully resynchronize a host from any state. That is the whole recovery story, and
it has four consumers, each of which is a hard case today: reconnect after
partition (§9), first adoption (§3.4), rebuilding a lost Atlas mirror, and the
post-update health check (§5).

The export **supersedes the periodic `GET /vms` sweep** as the mirror's backstop.
`/watch` SSE remains, for low-latency deltas; the export is the truth-restoring
primitive.

Three properties are load-bearing:

- It must be a **consistent snapshot**: one short bbolt `db.View`, materialized
  and released *before* streaming, never held under the write lock (§11.6).
- It carries a monotonic **observed-epoch**, so the CAS verbs of §11.2 can be
  `If-Match`-ed against the exact snapshot Atlas ingested.
- It carries the **running `boat_version`**, which is what closes the update
  loop: Atlas pushes a desired version (§5) and observes the running one here, so
  version drift is ordinary observed state rather than a separate bookkeeping
  channel.

**Atlas lands the export in two places, deliberately both:**

- **Hot fields denormalized onto `Server`** — capacity, unit liveness,
  `observed_boat_version` — because placement queries them on every provision and
  cannot afford a document parse. This reuses the existing capacity fields
  (`vcpus_total`, `memory_megabytes_total`, `pool_disk_gigabytes_total`) that
  **Refresh Capacity** already stamps ([28-placement.md](./28-placement.md)).
- **The full document archived as a `Host State Snapshot` row**, keyed by host
  and observed-epoch, for debugging, mirror rebuild and drift forensics.
  Retention is bounded per host, so the mirror stays obviously disposable.

### 2.6 Push commands, pull truth

Commands push, Atlas → Boat. State is pulled: Atlas maintains its mirror from the
`/watch` SSE stream with the §2.5 export as backstop. Boat additionally POSTs a
signed heartbeat on significant transitions, reusing the signed-webhook shape of
[`satellite_events.py`](../atlas/atlas/satellite_events.py). **Push for liveness,
pull for truth** — a pushed state update that is lost has no self-healing path,
while a pulled one re-converges on the next sweep.

### 2.7 A Boat operation and a Frappe Task row

Boat keeps its own append-only operation journal; that journal is
crash-recovery truth. The Frappe `Task` row stays Atlas's operator-facing audit
and replay record. **They share one identifier: `op_id == Task.name`.**

The sequence: Atlas creates the Task (`Pending`), calls Boat with
`op_id = task.name`; Boat streams progress over SSE; Atlas folds each chunk onto
`live_output` / `progress_line` through the existing `task_log` realtime event —
the streaming seam of [22-observability.md](./22-observability.md) survives
verbatim, sourced from SSE instead of an SSH log tail. On completion Boat returns
the typed result and Atlas writes `stdout` / `exit_code` / `status`.

**Replay never double-runs.** Re-POSTing an in-flight or completed `op_id`
returns the recorded operation unchanged and runs nothing. An `op_id` already
recorded against a *different* verb or VM is a `409`: replay is only replay when
it is the same operation. The claim is a single store transaction, so two
concurrent posts of one identifier can never both come back claimed.

## §3. Boat internals

### 3.1 Store — bbolt

Single file, pure Go, zero CGO, transactional; ideal for a static binary.
Buckets: `vms/<uuid>`, `ops/<op_id>` (the append-only journal), `host`,
`alloc/ipv6`, `fence/<uuid>`, `held/ipv6` (forward leases, §11.4), `progress/`
(§11.6). **On-disk artifacts — `network.env`, LV names, markers — are the ground
truth Boat re-derives from on adoption; bbolt is the fast transactional index,
not a second truth.**

### 3.2 Reconciler and forward-only state machines

Every operation is a **forward-only** state machine: ordered, idempotent,
checkpointed steps, always run forward, never unwound. This is the discipline
[`migration.py`](../atlas/atlas/migration.py) already encodes for migration
(`PHASE_ORDER`, `advance_migration`), generalized to every operation. A
background **reconciler** drives observed toward `desired_power` and the desired
spec, and self-heals a dropped command. **One actor per VM** serializes the
reconciler and any verb so the two never double-drive the same machine (§11.3).

### 3.3 Crash recovery — re-attach, do not restart

On start Boat scans for running firecracker processes and their API sockets
(deterministic per-UUID paths, ported from
[`paths.py`](../scripts/lib/atlas/paths.py)), **re-attaches** over the
Firecracker API socket, replays the `ops` journal — re-entering non-terminal
operations at their checkpoint — and rebuilds the observed store from the host
scan (§3.4). This daemonizes what the systemd units and `atlas-pool.service`
already do on reboot, under one resident process with a durable journal.

**Re-attach is load-bearing far beyond crash recovery: it is what makes
auto-update safe**, because a binary swap restarts the daemon under live VMs
(§5). A Boat that restarts firecracker to regain control of it is a Boat that
cannot be upgraded without an outage.

**Fail closed on an empty fence store.** A Boat that lost bbolt refuses to boot
any UUID until it re-registers and re-pulls desired state and epochs (§11.1).

### 3.4 Re-adoption and the host scan

The enumerators already exist, inverted, in
[`reset-server.py`](../scripts/reset-server.py) (`_list_vm_directories`,
`_list_units`, `_list_netns`, `_list_atlas_links`, `_list_ndp_proxy`,
`_list_atlas_lvs`). Boat's Go port uses the same enumeration to **ingest**: each
artifact is read, firecracker sockets are cross-checked for liveness, and the
observed document per VM is reconstructed.

**Ambiguous or partially torn-down artifacts — a crash mid-terminate — go to a
`quarantine` state requiring confirmation, never into the observed set.** A
half-deleted VM ingested as truth is a VM Atlas will try to start.

### 3.5 The native-Go rewrite is gated by differential testing

Full native Go, but not blind. The Python host surface is the **conformance
oracle**: the `scripts/lib/atlas/` modules, the `networkd/` modules, the host
verbs, and their millisecond unit tests.

- Each Go module is validated against a **golden corpus** captured from its
  Python counterpart: same inputs, byte-identical rendered commands and results.
  The parameterized-quoting model of [`_run.py`](../scripts/lib/atlas/_run.py) is
  the specification the Go command builder must match.
- A **differential phase runs both implementations side by side on a real host**
  — the Go daemon shelling out to the Python verb as reference — and asserts
  identical host effects, before that operation cuts over to native-only. This
  retires the big-bang risk (LVM CoW ordering, jail nesting, EUI-64, off-link
  routing, wg peer-table rendering) one module at a time.
- **Port order is restart-sensitivity and hot path first**: adoption scan,
  firecracker re-attach, wake-trap loop, network apply; then LVM, rootfs and
  image sync; cold paths last.

The gate is honoured per module. Skipping it under schedule pressure re-creates
exactly the risk it exists to retire.

### 3.6 CLI and daemon

The `boat` CLI talks to the resident daemon over `/run/boat/boat.sock` (0660) speaking
the same HTTP/JSON API (§2.1): `boat vm start <uuid>`, `boat vm ls`,
`boat host facts`, `boat export`, `boat adopt`. This is also the operator's
break-glass path when Atlas is unreachable.

### 3.7 The unit set

`boat daemon` supervises the sibling units: it owns their start, stop and
restart, and surfaces each one's liveness in `GET /v1/host`. **It never reaches
into networkd's gossip state** — supervision is lifecycle only, and the ANCP
boundary of §0 stays intact.

Unit template follows
[`atlas-networkd.service`](../scripts/systemd/atlas-networkd.service):
`Type=notify`, `WatchdogSec`, `StartLimitIntervalSec` / `StartLimitBurst`,
`RuntimeDirectory` + `StateDirectory`, `Restart=on-failure`.

## §4. Bootstrap, registration, re-adoption

**`boat bootstrap` brings a bare host to Active by itself** — thin pool, network
scaffold, firecracker and jailer install, sudoers, unit installation, then
self-registration to Atlas. It replaces
[`bootstrap-server.py`](../scripts/bootstrap-server.py) as an SSH-driven Task,
and like it, is idempotent and safe to re-run on an Active server.

- **Landing the binary reuses the existing SSH path.** `Server.bootstrap()` and
  [`install.sh`](../scripts/install.sh) place the signed binary; no new channel
  is invented. `install.sh` shrinks to "drop the signed binary, install the
  units"; `Server.bootstrap()` shrinks to "drop binary, invoke `boat bootstrap`,
  await registration". SSH stays, for bootstrap and for break-glass (§12).
- **Registration mirrors the armed auto-revert handshake** of
  [`central_link.py`](../atlas/atlas/api/central_link.py) `provision_tunnel` /
  `confirm_tunnel` ([21-tunnel.md](./21-tunnel.md)): on first boot Boat generates
  its token or keypair and registers to Atlas, and a failed handoff reverts
  rather than bricking the host.
- **Re-adoption**: on a host that already has VM state — an upgrade, a restart —
  Boat runs the §3.4 scan and adopts existing VMs idempotently, re-attaching to
  live firecracker rather than restarting it.

**One constraint comes from shipped ANCP and is not Boat's to relax.** ANCP's
bootstrap contract (spec/31 §8, §19.4, §19.5) — the WireGuard and ed25519 signing
private keys, `seed.json`, the operator public key, and the one-shot newcomer
introduction certificate — is signed with the **operator provision key, which
Atlas holds and Boat must never hold**. A Boat-generated introduction certificate
is self-signed and is rejected by every existing host. Those artifacts are
therefore Atlas-written, they ride the same channel that lands the binary, and
they must exist before `boat networkd` starts. *Which side of the bootstrap
handshake writes them is open — see §16.*

## §5. Self-update

Boat updates itself. The required shape, all seven steps:

1. **Desired version lives in Atlas** (`Server.boat_version`) and **Atlas pushes
   it**. There is no host-side poll. Atlas already knows which hosts are Unknown
   or mid-operation, so it is the only place a staggered rollout can be driven
   correctly; a host-side poll would have to reconstruct cohorts and jitter
   locally, which is exactly where fleet-wide simultaneous updates come from. An
   unreachable host simply updates late, on reconnect. The **running** version
   comes back in every export (§2.5).
2. **Verify signature and checksum before anything else**, then **atomically
   rename** over `/usr/local/bin/boat`. Keep N-1 on disk.
3. **Quiesce first.** Refuse new operations; checkpoint in-flight ones into the
   journal (§3.2). The journal is what makes an interrupted update replayable.
4. **Restart the units in a defined order, and re-attach to running firecracker
   rather than restart it** (§3.3). This is the crux: THE RULE means one binary
   backs every unit, so a swap re-points all of them at once. **Sleeping VMs must
   stay asleep across an update** — the `sleeping` marker is authoritative
   ([32-sleepy-vms.md](./32-sleepy-vms.md)) and a restart must not trip the wake
   path.
5. **Health-check after the swap** — a `GET /v1/export` round trip plus unit
   liveness — and **roll back to N-1** on failure.
6. **Atlas staggers the fleet**: canary first, then waves. A simultaneous
   fleet-wide auto-update is the one failure mode that can brick every host at
   once, and nothing inside a single Boat can prevent it.
7. It extends [23-supply-chain.md](./23-supply-chain.md): signed releases,
   checksum-pinned install, reproducible builds, provenance.

## §6. The networking split, after ANCP

### 6.1 The tiers

| Concern | Owner | Boat's role |
|---|---|---|
| Private `fdaa::/16` mesh, membership, ownership gossip, `wg-mesh` peer table | **ANCP** (decentralized, §0) | supervises the unit; **writes `local-ownership.json`** |
| Per-VM netns / veth / tap, NAT44, proxy-NDP, `/128` route, per-VM nft isolation | **Boat** | computes and applies |
| Park / wake trap, per-VM firewall | **Boat** (reflex) | applies; enrolment from Atlas |
| Reserved-IP 1:1 NAT (public v4) | **Boat** | applies; CAS on the host slot |
| Customer-gateway host forwarding | Atlas-computed | Boat applies |
| Vendor reserved-IP allocate / assign / release; public IPv6 allocation | **Pure Atlas** | none |
| Central management tunnel | **Pure Atlas** | none (plus the host) |

The rule behind the table: a pure function of *(this VM, this host)* is Boat's; a
function of *(fleet, placement, tenancy)* is Atlas-computed and Boat-applied; a
function of *(vendor account, Central)* is pure Atlas; **and the private-plane
mesh is nobody's — it is gossiped.**

### 6.2 Public IPv6 allocation stays in Atlas for v1

`allocate_ipv6(server)` is **cluster-aware**: after a keep-address migration
([24-vm-migration.md](./24-vm-migration.md)) a live VM on host B can own an
address out of host A's range, so filtering by `server ==` alone double-allocates.
The naive "birth-host allocates" model is therefore **unsound** — reuse-after-
terminate collides with a permanent vendor forward, and vacate-then-reallocate
races a keep-address move.

**Accepted v1 constraint: allocation stays in Atlas, unchanged and
cluster-aware.** Boat only *applies* the address it is handed and reports the
in-use set as observed state. Pushing allocation down to Boat is gated on the
**union-reconciliation law** and the **forward-lease** (§11.4) being live and on
the fence epoch being enforced (§15, WO-6).

This is the **public** plane. The private plane is already decentralized by ANCP
and is not part of this question.

## §7. The guest-service plane stays in Atlas

### 7.1 Atlas keeps the direct guest-SSH plane

Guest-service configuration — proxy maps, bench and site deploy, in-guest gateway
config — is *guest-plane* work: application config inside a customer OS,
definitionally not bare-metal host state. It stays in Atlas over
`connection_for_guest` ([04-tasks.md](./04-tasks.md)): SSH to the guest's public
`/128` with the Atlas key.

**Boat does not mediate guest traffic and stays workload-agnostic.** Routing
guest-exec through Boat is rejected: it would make Boat a generic guest-command
proxy and re-entangle service semantics with the daemon that must not know them.

### 7.2 Guest identity is an opaque blob

Boat owns the rootfs and runs identity injection at provision (the port of
[`rootfs.py`](../scripts/lib/atlas/rootfs.py)), but **treats guest identity as
opaque bytes**:

- Boat receives a `ProvisionSpec` whose `identity` is `{uuid, ipv6, ipv4_link,
  private_address, authorized_keys_blob, extra_env: [{path, content}]}`.
- Boat computes hostname and machine-id **from the UUID by a fixed rule it owns**
  — naming a host after its UUID is mechanics — regenerates host keys, and writes
  `authorized_keys_blob` and every `extra_env` entry **verbatim, without
  parsing**.
- **Service-semantic fields are not named in Boat's schema.**
  `routing_base_url` ([18-bench-self-routing.md](./18-bench-self-routing.md))
  arrives as one anonymous `extra_env` entry that Boat cannot tell from any other
  guest env file. By contrast `host_signature` (the warm-restore guard) and
  reserved-IP NAT are guest-agnostic host mechanics, and Boat owns them outright.

### 7.3 Accepted constraint — dark and private-only VMs

`connection_for_guest` requires a public `/128`, which dark VMs
(`public_networking = 0`) lack. **For v1 the service layer supports public VMs
only**; private-only *service* VMs are out of scope. Tenant private-only VMs that
run no Atlas-managed service are unaffected. The future escape hatch is a single
bounded `boat.dial_guest(uuid, port) → socket` primitive — an L4 forward into the
VM's netns, with Boat never receiving a command, a shell, or the guest credential
— and Atlas's SSH layered over it. It is not specified here and not built.

## §8. Cross-host operations are sagas

Atlas is the **saga orchestrator**; each Boat runs a **local idempotent state
machine**. This is a near-verbatim relocation of
[`migration.py`](../atlas/atlas/migration.py), already a resumable phase machine
with `reconcile_migrations` as its safety net
([24-vm-migration.md](./24-vm-migration.md)); what changes is that each phase
becomes an RPC to the relevant Boat instead of an SSH `run_task`.

| Atlas phase | RPC target | Boat-local effect |
|---|---|---|
| Export | source Boat | export base and disk, checkpointed |
| TargetPrepare | target Boat | receive base, build dm-clone; if keep-address, arm the forward tunnel |
| InjectIdentity | target Boat | identity injection (opaque blob, §7.2) |
| Cutover | target Boat (+ source stop) | boot on dm-clone read-through; source fast-stops |
| Hydrate | target Boat | poll hydration to 100% — a long forward-only copy, self-paced |
| Collapse | target Boat | swap dm-clone to linear once local |
| Repoint | Atlas | re-point Subdomains and proxy, record the new `server`, **bump `boot_epoch`** |
| Cleanup | source Boat | clean up the source |

**Cutover completion requires positive fencing of the source, not just a target
boot.** Atlas must not advance to Repoint until it has an acked heartbeat from
the target at the new epoch **and** has fenced the source — the epoch bump acked,
or the source confirmed Unknown (§11.1).

The same shape covers warm-snapshot fan-out (each target Boat pulls the golden,
validates `host_signature`, restores) and S3 snapshot sync
([29-snapshot-backup.md](./29-snapshot-backup.md)): **Atlas presigns and owns the
S3 credentials; Boat transfers the bytes through the presigned URL. Atlas never
proxies bytes.**

Ownership changes propagate to the private plane for free: the VM's `/128` leaves
the source's `local-ownership.json` and appears in the target's, and ANCP gossips
it. Atlas does not sequence the mesh.

## §9. Partition and failure semantics

**Boat when Atlas is unreachable — autonomous.** It keeps every VM running (no
desired change means no action), keeps serving host-local reflexes (wake traps,
reboot recovery from `network.env`, pool rebind), keeps ANCP gossiping
independently, keeps the local `boat` CLI working as break-glass, and buffers
observed reports to replay on reconnect.

**Atlas when a Boat is unreachable — the host is `Unknown`, not dead.** This is
the whole rule and it is easy to get wrong:

- **Atlas must not assume the VMs died.** An unreachable daemon is evidence about
  the daemon, not about firecracker.
- **Placement excludes an Unknown host from new arrivals but does not evict its
  VMs.** Evicting on unreachability turns a management-plane blip into a
  fleet-wide outage, and — without the fence epoch — into two live copies of
  every VM the moment the host comes back.
- **The stale mirror freezes, flagged stale. It is never nulled.** A nulled
  mirror reads as "this host has no VMs," which is the input placement and
  capacity accounting would act on.

**Reconnect** is the §2.5 symmetry: Atlas re-`PUT`s desired state (idempotent
no-ops where it matches) and pulls `GET /v1/export` to rebuild the mirror in one
transaction; Boat replays its buffered transitions; `/watch` resumes. Any
in-flight operation is resumed by Boat's state machine, and `op_id` dedupe makes
that exactly-once (§2.7).

**Split-brain is prevented by the fence epoch (§11.1), not by phase ordering.**
Ordering is a property of a saga that completes; the fence is a property that
holds when one does not.

## §10. Observability and audit

The [22-observability.md](./22-observability.md) model survives intact, merely
re-sourced. The Frappe `Task` row stays the live progress carrier —
`live_output`, `progress_line`, `stdout`, `status` — and Boat's operation streams
over SSE while Atlas folds each chunk on through the existing `task_log` realtime
event. Boat's `ops` journal is crash-recovery truth, readable at
`GET /v1/ops/{op_id}`.

The operator's audit surface stays the Task row plus the fleet-wide *Running
Operations* view, which now also reflects **Boat-reported observed transitions** —
a wake-trap flip, a crash-restart — giving a truthful fleet picture Atlas could
never show before.

**Audit parity is a hard requirement, not an aspiration:** every operation writes
an append-only record equivalent to today's immutable `Task` and `SSH Command
Log` rows.

## §11. Correctness invariants

**These six are the gate.** They must be written here and reviewed before Boat
code is written, because every one of them is cheap to build in and expensive to
retrofit. Each is stated as a rule with its failure mode: a rule whose reason is
missing gets deleted by the next person who finds it inconvenient.

### 11.1 The fence epoch

**Rule.** A per-UUID monotonic `boot_epoch`. **Atlas is its sole issuer.** It is
stored on the VM row and mirrored into each Boat's `fence/<uuid>` bucket on every
desired `PUT`. **Boat refuses to boot a UUID unless its local epoch ≥ the on-disk
unit's epoch AND desired `server == self`.** An **empty fence store means boot
nothing**. The epoch bumps at exactly one point: migration Repoint (§8).

**Failure mode.** Without it, a partitioned migration produces two live copies of
one VM: the source Boat, reconnecting with a desired state that still says
Running, boots the VM whose disk the target already owns. Two writers on one
disk, and two hosts answering NDP for one `/128` — which ANCP will correctly
report as a conflict and blackhole (spec/31 §18), turning silent corruption into
a visible outage, but only after the corruption. The same failure needs no
migration at all: a Boat that lost bbolt and boots everything it finds on disk is
the single most dangerous state the system can reach, which is what "empty fence
store means boot nothing" exists to forbid. The epoch also makes force-reprovision
safe by construction rather than by operator care.

### 11.2 CAS on contended reservations

**Rule.** Placement, the capacity gate, migration-target choice and reserved-IP
attach all go through `PUT` with `If-Match: <observed-epoch>`. **Boat returns
`409` if its state moved since the epoch the mirror was built from.** `server`,
`ipv6_address` and the reserved bindings stay frozen except through those CAS
verbs.

**Failure mode.** Drift on an observation field is a display nuisance; drift on a
reservation is corruption. Without CAS, two provisions read the same stale mirror
and both place into the last slot of RAM on one host, or one reserved IP is
DNAT'd to two guests. The mirror is disposable (§1) precisely because no
contended decision is ever *taken* from it — CAS is the mechanism that makes that
sentence true rather than aspirational.

### 11.3 `desired_power` versus `observed_status`

**Rule.** Verbs mutate `desired_power`. The reconciler is the **single per-VM
actor** that drives observed toward desired; verbs never touch the host directly.
**Precedence: an explicit `desired_power = Stopped` outranks the wake trap — a
stopped VM is not woken by traffic.**

**Failure mode.** Three distinct failures, one rule. Without the split, "status"
means "the last command succeeded," so a VM that died an hour ago still reads
Running and nothing ever notices — which is the defect Boat exists to fix.
Without the single actor, a verb and the reconciler double-drive one machine and
a start races a stop on the same UUID. Without the precedence rule, a VM the
operator deliberately stopped is resurrected by a stray port scan: the operator's
stop silently undone by an unauthenticated packet.

### 11.4 Forward lease and union reconciliation for public IPv6

**Rule.** A `/128` is **in use if Atlas claims it OR the host sees it**, and
**free only if both agree**. A kept address (a keep-address migration,
[24-vm-migration.md](./24-vm-migration.md)) is a **lease on its source range**,
recorded in `held/ipv6` **before** `vm-network-down` runs, and is **not freeable
while any host still forwards it**.

**Failure mode.** Ownership of a public `/128` is genuinely cluster-wide (§6.2),
so any single-sided view double-allocates. Atlas-only: a host that still forwards
a kept address is invisible, and the address is handed to a new VM while packets
for the old one keep arriving. Host-only: an address allocated by Atlas but not
yet applied looks free to the host that will apply it. Without the lease
recorded *before* teardown, a crash between "stop forwarding" and "release"
strands the address in neither book. The observable result in every case is two
VMs on one public address. This law is the precondition for ever moving
allocation into Boat, which is why §6.2 is gated on it.

### 11.5 Write-ahead journaling of the decision

**Rule.** The journal records the **non-idempotent decision** — which address,
which reserved IP, which host slot — **before** the host side effect, so a crash
then a retry replays deterministically. Allocation, LV create and operation
completion commit in one bbolt transaction. Reserved-IP attach is CAS on the
host's reserved-IP slot.

**Failure mode.** "Every verb is idempotent, so retry equals re-run"
([Taste](../llm/Taste.md), [04-tasks.md](./04-tasks.md)) only holds if the retry
makes the *same choice*. A crash between choosing an address and writing it into
`network.env` otherwise leaves that address allocated in nobody's book and in use
in nobody's plane — or, worse, re-chosen for the next VM while the first VM's
half-built netns still carries it. Journaling the decision converts a
non-idempotent choice into an idempotent replay, which is the only way a
forward-only state machine (§3.2) can be resumed at all.

### 11.6 bbolt isolation

**Rule.** Heartbeat and fence reads are **lock-free** — served from an in-memory
atomic snapshot, never the write lock. Long operations (image sync, migration
hydration, RAM snapshot) write **only** the `progress/` bucket, never the hot
state bucket. SSE readers and `GET /v1/export` take a **short `db.View`,
materialize, and release before streaming**.

**Failure mode.** bbolt has a single writer. A 900-second image sync that holds
the write lock stalls the heartbeat; Atlas marks a perfectly healthy host
`Unknown`, placement stops sending it VMs, and the operator investigates a
partition that is really a busy disk. An export streamed *under* the lock is
worse still: the stall becomes proportional to the client's read speed, so a slow
Atlas can freeze a healthy host. **A busy host must never be mis-declared
partitioned** — every §9 behaviour keys off Unknown, so a false Unknown is a
false everything.

## §12. Security — no worse than today is the bar

- **Non-root daemon.** Boat runs as a service user under the **existing pinned
  NOPASSWD sudoers allow-list** ([`sudoers.d/atlas-tunnel`](../scripts/sudoers.d/atlas-tunnel)
  is the template: enumerated `wg`, `nft -f …`, specific `systemctl` and
  `firecracker` invocations). The individual privileged calls need root; the
  daemon does not. This is the single biggest blast-radius reduction the split
  buys — today Atlas SSHes to the host as root.
- **Verb allow-list enforced at the API boundary.** `scripts_catalog.allowed_scripts()`
  ports into the API layer. **There is no arbitrary-command endpoint, ever.** The
  ad-hoc surface stays the SSH Console ([04-tasks.md](./04-tasks.md)), which is
  operator-only and fully logged.
- **Short-lived scoped tokens** (or mTLS over the tunnel), minted per host by
  Atlas. **Rotation under partition:** if Boat cannot reach Atlas it serves the
  last valid token until a **hard expiry**, and Atlas re-mints on reconnect.
  There is never an unreachable-and-trusting-a-stale-token-forever window, and
  never a partition that locks the operator out of their own host.
- **Audit parity** — §10. Append-only operation records equivalent to today's
  immutable `Task` / `SSH Command Log` rows.
- **Supply chain is a NEW threat, created by self-update.** SSH-driven scripts
  were fetched from the controller per Task; a self-updating binary fetches and
  executes code on its own. Signed releases, checksum-pinned install,
  reproducible builds and provenance are therefore mandatory, extending
  [23-supply-chain.md](./23-supply-chain.md), and the signature check happens
  **before** anything else in §5. The Boat repo records every accepted Go
  dependency and its rationale so the list stays legible for that sign-off; Go
  dependencies are argued per dependency in review, with the standard library as
  the default.
- **SSH is retained as break-glass.** Inbound key SSH with the fixed verb catalog
  stays as the out-of-band channel to restart or replace a wedged Boat. **The
  verb-port cutover does not delete it** until a proven equivalent recovery
  channel exists (§15, WO-6).

## §13. What is explicitly not Boat's

- **The ANCP gossip plane.** Boat supervises the `networkd` unit and writes
  `/etc/atlas-networkd/local-ownership.json`. It **never touches membership, the
  ownership table, the generation counters, or the wg peer table**. Porting
  `networkd` to Go (§15, WO-5) is a port, not a redesign: the wire format, record
  shapes and timer defaults of spec/31 §7, §13 and §14 are unchanged, and a mixed
  cluster of Python and Go hosts must converge.
- **The guest-service plane.** It stays in Atlas over direct guest SSH (§7.1).
- **Vendor and Central APIs.** Droplet and Elastic Metal create/destroy, reserved
  IP allocate/assign/release, DNS, ACME, S3 credentials, the Central link. Boat
  holds no vendor credential. Where bytes must move (S3 snapshot sync), Atlas
  presigns and Boat transfers (§8).
- **Public IPv6 allocation**, for v1, because it is cluster-aware (§6.2).
- **Service-role knowledge.** The de-fusion of the generic VM controller — moving
  `is_proxy`, `is_gateway`, `build_mode`, `pilot_credential_id` and
  `terminate()`'s service fan-out into an `atlas/services/` module with a
  `Service` / `Service Binding` registry — runs in parallel and has no Boat
  dependency (§15, Track S). It lifts the seam of
  [30-core-service-boundary.md](./30-core-service-boundary.md) **in-app**: that
  chapter's federated separate-deployment answer is superseded, its boundary is
  not. The dependency stays one-directional: services depend on core, never core
  on services.

**On transport.** [04-tasks.md § Why SSH, not HTTP](./04-tasks.md) measured both
transports and found them statistically indistinguishable, and concluded not to
switch for latency. **That conclusion stands and Boat does not dispute it** — the
lifecycle wall time is gated by what the verb does on the host, not by how it is
delivered, and Boat is not a latency change. The transport moves because **the
state** moves. The Task model itself — a typed verb, `--kebab-case` flags in, one
typed result out, one audited row — survives the port unchanged (§2.4, §2.7); only
the delivery changes, and only for hosts with `boat_enabled`.

## §14. Repo conventions

The Boat repo follows Atlas's style and semantics, seeded in its own `CLAUDE.md`
and `llm/Taste.md`:

- Small functions, files of 100–300 lines, packages under ~15 files, **no
  abbreviations** (`virtualMachine`, not `vm`, outside five-line scopes), clean
  over clever, reuse over new code, always tests, tests next to the code they
  cover.
- **One operation = one verb = one op record** — the Go analogue of "one
  operation = one script = one Task row". Compose *inside* a verb, never by
  chaining RPCs from the caller.
- **Every verb idempotent.** Retry is re-run; there is no special repair mode.
- **Fail loud at the boundary, never fall back** — the rule that makes Atlas
  raise on an SSH or vendor error rather than degrade.

## §15. Delivery order

Work orders, one line each — enough to place any commit. Each is gated behind
`Server.boat_enabled` (and, from WO-2, per-VM `observed_authority`), and each
rolls back by clearing its flag.

| WO | Ships |
|---|---|
| **WO-0** | Walking skeleton: a `boat` binary that starts, serves the API on the tunnel and the unix socket, persists to bbolt, and starts/stops one real VM driven from Atlas through `BoatClient`. |
| **WO-1** | Observed state: adoption scan, firecracker re-attach, `GET /v1/export`, `/watch` SSE, the `Host State Snapshot` mirror, and the fence store — advisory only, the DB still authoritative. |
| **WO-1b** | `boat bootstrap`: a bare host brought to Active by the binary itself, with the armed auto-revert registration handshake (§4). |
| **WO-2** | Full lifecycle and reflexes: every VM verb through Boat, the per-VM reconciler, the journal, and the wake trap resident in Boat; per-VM authority flips to Boat. |
| **WO-3** | Unit supervision and host-local network apply: the sibling units re-pointed at `boat <sub>`, `local-ownership.json` written by Boat, reserved-IP NAT and gateway forwarding applied under CAS. |
| **WO-4** | Cross-host sagas: migration, warm fan-out and S3 sync driven over Boat RPCs, with Repoint gated on positive source fencing (§8). |
| **WO-5** | `boat networkd`: the ANCP daemon in Go, same binary, own unit, byte-identical wg and nft output — a port, not a redesign (§13). |
| **WO-5b** | Auto-update (§5). Hard-gated on WO-1's firecracker re-attach; cannot ship before it. |
| **WO-6** | Verb-port completion and cutover: the remaining verbs as `boat <verb>`, the venv and durable package retired, public-IPv6 allocation pushed down **only** once §11.4 is proven. SSH break-glass and `connection_for_guest` are **not** deleted. |
| **Track S** | Services de-fusion (§13, last bullet). Parallel, no Boat dependency; one service moved at a time, green each commit. |

Verification is spec'd with the work, not after it: the §11 invariants reviewed
here before any Boat code; a per-operation differential test on a live host before
that operation cuts to native-only (§3.5); partition drills at WO-2 and WO-4 —
kill Atlas and Boat keeps VMs running and self-recovers on reboot, kill a Boat and
Atlas marks the host Unknown without evicting, and a partitioned-migration drill
proves the fence epoch stops two live copies; an update drill at WO-5b that swaps
the binary under a running VM and a sleeping VM and proves a corrupted binary and
a failing health check both roll back; and `run_all_smoke`
([README § Testing](./README.md)) green against one shared bootstrapped host at
every work order.

## §16. Open

Everything else in this chapter is decided. These are not.

1. **Who writes ANCP's bootstrap trust artifacts after the split** (§4). The
   constraint is fixed — they are operator-signed, Atlas holds the key, Boat must
   never hold it, and they must land before `boat networkd` starts — but whether
   Atlas writes them in the same SSH step that lands the binary or `boat
   bootstrap` requests them during registration is not settled.
2. **API version negotiation.** Atlas must speak `[vN-1, vN]` of the Boat API for
   at least one release window, negotiated on connect. The paths are `/v{N}`; how
   a client discovers `N` is unspecified.
3. **The Boat-side equivalent of `sleep-vm`'s wake-trap gate.** Today
   `sleep-vm` refuses outright when `atlas-wake-trap.service` is not active,
   because a slept-but-unparked VM is silently worse than a VM left awake
   ([32-sleepy-vms.md](./32-sleepy-vms.md)). The equivalent precondition when the
   reflex is resident in Boat is not named.
4. **Quarantine resolution** (§3.4). A partially-torn-down VM lands in quarantine
   requiring confirmation; the operator surface and the API that clears it are
   not specified.
5. **Two numbers**: the `Host State Snapshot` retention bound per host (§2.5),
   and the hard expiry on a token served under partition (§12).
6. **Dark-VM service reach** (§7.3) is deferred, not open — the `dial_guest`
   escape hatch is named as a shape, and specifying it is a decision for whenever
   a private-only service VM is actually required.
