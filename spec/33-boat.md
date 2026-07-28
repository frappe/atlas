# Boat — the per-host daemon, and what Atlas keeps

> **Status: PARTLY BUILT.** The spec-first gate held — §11's invariants were
> written and reviewed before any Boat code — and **WO-0, WO-1 and WO-2 are
> built**: `boat` is a running daemon with an adoption scan, a whole-host export,
> a per-VM reconciler, a resident wake trap and every lifecycle verb, and Atlas
> drives real hosts through it behind `Server.boat_enabled`. `make check` is
> green. The repository is `github.com/frappe/boat`; the contract IDL is
> `api/openapi.yaml` in that repo and **this chapter governs it** — the Boat
> repo's README points here. Per-work-order status, including what WO-2 still
> owes, is in §15.
>
> The chapter carries both the design and what of it exists. **Every claim not
> yet true of the code is marked `NOT BUILT` where it is made, naming the work
> order that owns it**; everything unmarked is in the code. The four largest gaps
> are the fence *comparison* (§16.0), CAS on contended reservations (§11.2),
> write-ahead journalling (§11.5) and Firecracker re-attach (§3.3). The last two
> are the dangerous shape: the packages exist, are tested, and have **no caller**,
> so they read as finished from the file list and enforce nothing.

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
ExecStart=/usr/local/bin/boat gateway               # was gateway.service
ExecStart=/usr/local/bin/boat mgmt-firewall         # was atlas-mgmt-firewall.service
ExecStartPre=/usr/local/bin/boat vm-network-up %i   # firecracker-vm@ hooks
```

**One unit ships today.** `systemd/boat.service` runs `boat daemon`, and the
binary's dispatcher answers `daemon`, `vm`, `host` and `version` — nothing else.
Every other line above is **NOT BUILT** and lands with its work order: `networkd`
at WO-5, `pool` / `gateway` / `mgmt-firewall` / the `firecracker-vm@` hooks at
WO-3.

**One line has been struck rather than deferred: there is no `boat wake-trap`
unit, and there will not be.** The reflex runs as a goroutine inside
`boat daemon`. It has to, because the rule that an operator's `desired_power =
Stopped` outranks a stranger's SYN lives in the reconciler's planner (§11.3) and
nowhere else: a separate process could only consult it through the API, which
would make an unauthenticated inbound packet an API client. In-process, the trap
requests a reconcile pass by UUID and the planner decides — so the trap holds no
policy at all, which is exactly what the dividing rule asks of it. THE RULE is
about **one build artifact per host**, and a resident reflex inside the daemon
does not weaken that; a second binary would.

This is **not a new grammar** — it is the one the host already has.
[`_cli.py`](../scripts/lib/atlas/_cli.py) is already a multi-call dispatcher,
[`install.sh`](../scripts/install.sh) already symlinks `/usr/local/bin/atlas` to
it, and `scripts_catalog.py` already models a Task as a verb run on the host.
`/usr/local/bin/boat` takes that symlink's place **at WO-6**; until then the two
coexist on every host — `atlas` for the un-ported verbs, `boat` for the ported
ones — and `install.sh` still writes the `atlas` symlink. The scope is therefore
the **whole host surface**: every `scripts/*.py` verb, every `scripts/lib/atlas/`
module, every `networkd/` module, and every unit in `scripts/systemd/`.

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
command having succeeded. **Paused is in the contract's enum and is not yet
produced**: a paused guest's unit is still active, so `statusOf` reads it as
Running until the pause verb records the Firecracker API's own state. Atlas's
`observed_status` Select carries no Paused for the same reason, and a Running
observation is therefore accepted against a DB status of either Running or
Paused.

**What Boat reports today.** The observed rows above are the contract; the export
carries a subset. Live: `observed_status`, the unit's `ActiveState`/`SubState`,
`has_memory_snapshot`, `sleeping`, every fence epoch, the quarantine set, and the
host facts `vcpus_total` / `memory_megabytes_total` / `memory_megabytes_free` /
`pool_disk_gigabytes_total` / `pool_used_percent` / `host_signature` / kernel and
Firecracker versions / running `boat_version`. **NOT BUILT:** `last_started`,
`last_stopped`, `last_traffic_at`, `boot_id`, the `observed_*` resource numbers,
`public_ipv4` (WO-3), the running Firecracker PID (§3.3), and the LV inventory —
which adoption reads and the export then drops (§2.5). Of the per-VM fields
Atlas's mirror lands, `observed_status` is the only one
([`boat_mirror.py`](../atlas/atlas/boat_mirror.py)).

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
per-VM whether Boat's observation wins. `boat_enabled` is live and is the whole
rollback: clearing it returns the host to `run_task` with nothing else changed.
`observed_authority` **exists as a field and nothing reads it yet** — the mirror
is advisory for every VM, writing `observed_status` beside the DB's `status` and
recording every disagreement as drift without ever acting on it. Flipping it to
Boat is WO-2's remaining half. A Fake-backed server
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
  the source of truth. The typed **Go server** is generated from it (`make
  generate`, `oapi-codegen` pinned) and checked in at `internal/wire`, so a
  handler that drifts from the contract fails to compile — zero new runtime
  dependency. The **Python client is hand-written**
  ([`boat_client.py`](../atlas/atlas/boat_client.py)), one method per endpoint,
  shaped like [`digitalocean.py`](../atlas/atlas/digitalocean.py); *generating it
  is NOT BUILT*, and until it is, the Atlas side's conformance to the contract is
  a review property rather than a compile-time one. This carries the typing
  discipline `TaskInputs`/`TaskResult` already give
  ([04-tasks.md](./04-tasks.md)) across the language boundary. Explicit `/v{N}`
  path versioning; the daemon mounts the router bare and under `/v1` so a caller
  that pastes the documented server URL and one that curls the socket by hand get
  the same answers.
- **Boat listens only on the management-tunnel address and `/run/boat/boat.sock`,
  never on a public interface.** The tunnel is the transport-security boundary,
  exactly as it is for Central ↔ Atlas ([21-tunnel.md](./21-tunnel.md)). The
  socket is always bound; the tunnel listener is `--listen`, and the shipped unit
  does not set it — a host serves the socket only until an operator adds the
  address with a drop-in, because the tunnel address is handed over at
  registration and registration is WO-1b. Refusing to serve a TCP listener
  without a token is enforced; *nothing enforces that the address is not a public
  one* — that is the operator's and the unit's to get right.
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

Built today: the non-root daemon and its sudoers file, the socket's peer-credential
model, the bearer check on the tunnel listener (constant-time, and an unset token
matches nothing), and the exempt `/health`. **Minting is NOT BUILT** (WO-1b): the
token is a static per-host value read from site config
(`atlas_boat_tokens`) — see §12.

### 2.4 The operation set

All operations are idempotent, replayable and versioned. Every mutating **verb**
carries an `op_id`, and one arriving without it is refused rather than run — a
verb that cannot be recognised as a replay is a verb a retry boots twice. The
desired-state `PUT` deliberately carries none: it is idempotent by value, so
re-sending the same document is a no-op with nothing to deduplicate.

- **A. Desired-state apply — the durable primitive.** `PUT /v1/vms/{uuid}` with
  the full desired spec and `boot_epoch`. It writes the fence first and the
  intent second — the other order would leave intent recorded at an epoch this
  host was refused — and then **records and returns; it starts and stops
  nothing**. Convergence is the reconciler's, on its next pass. *A `PUT` does not
  yet wake that VM's actor, so a `PUT` alone can wait a sweep interval to take
  effect; Atlas never relies on that, because it posts the verb straight after.*
  This is how Atlas re-asserts intent on reconnect.
- **B. Lifecycle verbs.** `POST /v1/vms/{uuid}/<verb>` for `start`, `stop`,
  `pause`, `resume`, `sleep`, `wake`, `resize`, `rebuild` and `terminate`. *NOT
  BUILT:* `snapshot` and `warm-snapshot` (WO-4), `reserved-ip` (WO-3),
  `POST /v1/images/sync` (WO-6) and the migration sub-operations of §8 (WO-4).
- **C. Observed read and watch.** `GET /v1/vms/{uuid}`, `GET /v1/vms`,
  `GET /v1/host`, `GET /v1/export` (§2.5), `GET /v1/watch` (SSE deltas).
  *NOT BUILT:* operation progress on the stream (§2.7), and the CAS `If-Match:
  <observed-epoch>` reads of §11.2 — no operation takes that header today.
- **D. Host control.** *All NOT BUILT:* `POST /v1/bootstrap` (§4, WO-1b),
  `POST /v1/update` (§5, WO-5b), `GET|POST /v1/units/{name}` (sibling-unit
  supervision, §3.7, WO-3).
- **E. The journal.** `GET /v1/ops/{op_id}` (§10) and the unauthenticated
  `GET /v1/health` (§2.3). Both built.

**How a verb reaches the host, exactly.** Atlas states desired state with a `PUT`
before every Boat-routed verb, and then posts the verb, which says "now". The
verb does not merely flip a field and return: it **runs the mechanics itself,
inside the turn it takes from that VM's actor** — the same actor a reconcile pass
must hold — so a verb and a pass are one queue per UUID and can never
double-drive one machine. The reconciler is the *other* driver, not the only one:
it sweeps every desired record on a 30-second timer and runs a pass on demand
when the wake trap asks for one, and it self-heals a verb whose result was lost.
That is what §11.3's rule means in the code; read it there, not as "verbs never
touch the host."

The verb **names** are the host CLI's — `start-vm`, `stop-vm`, and an operation
record reads the same after the port as the Task row it replaces did before it —
and [`scripts_catalog.py`](../atlas/atlas/scripts_catalog.py) still names them.
The wire is not: it is JSON, so `_variables_to_flags` and
`task_results.parse_result` do **not** run on the Boat path. `run_boat_task` maps
a verb to its endpoint, and the typed operation record — `status`, `exit_code`,
`output` — replaces the scraped `ATLAS_RESULT=` line.

**Almost every verb's whole request is its `op_id`**, and that is a deliberate
consequence of the desired-state split rather than a minimal first cut: a resize
reads its numbers from the store, not the wire, so two attempts apply the same
shape and a request can never state a shape the store disagrees with. `stop`
carries the two knobs that are not desired state (`graceful`,
`stop_timeout_seconds`), and `rebuild` carries the source to lay down and the
guest identity to inject (§7.2) — neither of which has an answer in desired
state, because which image to reinstall from is a choice made at the moment of
asking. A verb that needs a per-VM number the store could hold and takes it from
the wire instead is the shape to refuse in review.

### 2.5 Whole-host export and one-shot sync

`GET /v1/export` returns Boat's **entire** observed state in one document: every
VM's observed doc, host facts, LV and thin-pool inventory, network state,
sibling-unit liveness, the quarantine set, every fence epoch, and the running
`boat_version`. Atlas ingests it **in one transaction** to rebuild its mirror.

**Three of those are NOT BUILT and the document simply omits them:** the LV
inventory (adoption enumerates it and the scan result is dropped rather than
stored — the fix is a store bucket, not a new command), **network state** (WO-3
owns per-VM networking), and **sibling-unit liveness** (WO-3 owns supervision;
Atlas already has the `observed_units_down` field waiting for it). Everything
else in the list is carried. The omissions are absences, not empty arrays: an
empty `logical_volumes` would assert "this host has no volumes", which is a claim
nothing in the daemon has looked closely enough to make.

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
- It carries a monotonic **observed-epoch**, read in the same transaction as the
  records it describes, so the CAS verbs of §11.2 can be `If-Match`-ed against
  the exact snapshot Atlas ingested. The epoch is live and Atlas orders its
  ingests by it — *the `If-Match` half is NOT BUILT* (§11.2).
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
  Retention is bounded per host — 20 epochs — so the mirror stays obviously
  disposable. Each row also carries the drift list computed at ingest, so an
  operator diffs one epoch against its neighbours without re-deriving it.

An ingest at an epoch the mirror already holds is a no-op, and so is an older
one: the epoch is monotonic per host, so a re-poll cannot duplicate a snapshot
and a late answer from a slow request cannot overwrite a newer one.

### 2.6 Push commands, pull truth

Commands push, Atlas → Boat. State is pulled: Atlas maintains its mirror from the
`/watch` SSE stream with the §2.5 export as backstop. Boat additionally POSTs a
signed heartbeat on significant transitions, reusing the signed-webhook shape of
[`satellite_events.py`](../atlas/atlas/satellite_events.py). **Push for liveness,
pull for truth** — a pushed state update that is lost has no self-healing path,
while a pulled one re-converges on the next sweep.

**Only the pull half exists.** Boat serves `/watch` and publishes an event on
every observation it writes; **Atlas has no SSE consumer**, and the **signed
heartbeat is NOT BUILT**. What is built is the backstop, and it is the right half
to have built first: `boat_mirror.sweep_mirrors` runs every five minutes and
enqueues one `GET /v1/export` ingest per `boat_enabled` host. So the mirror's
freshness bound is the sweep interval rather than the transition, and a dropped
stream costs nothing because there is no stream. The consequence to keep in mind
until the consumer lands: a host that becomes unreachable is flagged `Unknown`
within five minutes, not within seconds.

### 2.7 A Boat operation and a Frappe Task row

Boat keeps its own operation journal; that journal is crash-recovery truth. It is
one record per `op_id` — written when the operation is claimed and rewritten
exactly once when it ends, **first completion wins** — so a record is never
removed and an outcome is never overwritten by a later one, which is the property
"append-only" is being asked for here. The Frappe `Task` row stays Atlas's
operator-facing audit and replay record. **They share one identifier:
`op_id == Task.name`.**

The sequence: Atlas creates the Task (`Pending`), calls Boat with
`op_id = task.name`; Boat streams progress over SSE; Atlas folds each chunk onto
`live_output` / `progress_line` through the existing `task_log` realtime event —
the streaming seam of [22-observability.md](./22-observability.md) survives
verbatim, sourced from SSE instead of an SSH log tail. On completion Boat returns
the typed result and Atlas writes `stdout` / `exit_code` / `status`.

**The streaming middle of that sequence is NOT BUILT.** Today the verb is one
bounded request: Boat runs it to a terminal record before it answers, and
`run_boat_task` folds the record's `output` onto `stdout` and its one-sentence
`error` onto `stderr` in a single `_finalize` — the same row shape `run_task`
writes, through the same helpers, so nothing downstream can tell which transport
ran. What an operator loses meanwhile is live progress on a long verb, and what
Atlas loses is a way to distinguish a slow verb from a lost one: a non-terminal
status coming back is treated as a protocol surprise and raised, because on this
shape it cannot happen.

**Replay never double-runs.** Re-POSTing an in-flight or completed `op_id`
returns the recorded operation unchanged and runs nothing. An `op_id` already
recorded against a *different* verb or VM is a `409`: replay is only replay when
it is the same operation. The claim is a single store transaction, so two
concurrent posts of one identifier can never both come back claimed.

## §3. Boat internals

### 3.1 Store — bbolt

Single file, pure Go, zero CGO, transactional; ideal for a static binary. Records
are stored as **indented JSON**, deliberately: on a host too wedged to answer its
own API the only tools an operator has are `strings` and a hex dump, and every
key in the file is a key they can also search for in Atlas.

Buckets, as built: `virtual-machines` (by UUID), `operations` (by `op_id`),
`desired` (by UUID), `fence` (by UUID), `quarantine` (by identifier, latest-wins
per scan rather than a journal — a resolved quarantine must stop being reported),
and `meta`, which today holds the observed epoch and nothing else.

*NOT BUILT:* `alloc/ipv6` and `held/ipv6` (forward leases, §11.4 — WO-6) and
`progress/` (§11.6), which has no writer because no long operation has been
ported yet. A **second bbolt file** sits beside the store, `journal.db`, holding
the write-ahead decisions of §11.5; that split is a defect, not a design — see
§11.5.

**On-disk artifacts — `network.env`, LV names, markers — are the ground truth
Boat re-derives from on adoption; bbolt is the fast transactional index, not a
second truth.**

### 3.2 Reconciler and forward-only state machines

Every operation is a **forward-only** state machine: ordered, idempotent,
checkpointed steps, always run forward, never unwound. This is the discipline
[`migration.py`](../atlas/atlas/migration.py) already encodes for migration
(`PHASE_ORDER`, `advance_migration`), generalized to every operation. A
background **reconciler** drives observed toward `desired_power` and the desired
spec, and self-heals a dropped command. **One actor per VM** serializes the
reconciler and any verb so the two never double-drive the same machine (§11.3).

Built: forward-only and idempotent, and the one actor per VM. The reconciler
sweeps every desired record every 30 seconds and runs a pass on demand; a pass
re-reads the host, takes the **one** step that closes the gap, and leaves the
rest to the next pass, so a pass killed half-way leaves nothing to unwind. A VM
that cannot converge backs off per VM, doubling to a five-minute cap, so a broken
image costs the host one attempt an interval rather than a core.

***Checkpointed* steps are NOT BUILT.** No ported verb writes a checkpoint (§11.5),
so a verb interrupted part-way is not re-entered where it stopped: the VM is
converged by the reconciler's ordinary forward pass instead. That is safe for
every verb ported so far, all of which are idempotent from the top. It stops
being safe for the first verb that makes a non-idempotent choice — an address, an
LV, a host slot — which is precisely the boundary §11.5 draws.

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

**Built: the scan, and the property re-attach exists to protect.** Startup runs
the §3.4 host scan before it opens a listener, ingests what reads coherently, and
serves from that. Crucially, Boat **never launches or relaunches a Firecracker on
startup** — the VMs are not its children, its unit is deliberately not ordered
`Before=firecracker-vm@.service`, and a restart under live guests leaves every one
of them running. That is the guarantee auto-update needs, and it holds today.

**NOT BUILT: the re-attach itself, and the journal replay.** `internal/fcattach`
finds a live Firecracker by *talking to* its per-UUID API socket — the only honest
liveness test, since a socket inode outlives the process that bound it — and it
**has no caller**. So no observed record carries a Firecracker PID or an attached
socket, and nothing can drive a running guest through its API across a daemon
restart; observation after a restart is systemd's `ActiveState` plus the on-disk
markers alone. Likewise the reconciler asks the journal for unfinished operations
before its first sweep, but **nothing records a decision** (§11.5), so that list
is always empty, and an operation a crash left `Running` in the store stays
`Running` — there is no way to list the operations bucket to find it. Both are
WO-2 wiring, and **§5's hard gate is not yet satisfied**.

**Fail closed on an empty fence store.** A Boat that lost bbolt refuses to boot
any UUID until it re-registers and re-pulls desired state and epochs (§11.1).
This one is enforced, on both boot paths — the start verb and the reconciler's
pass — and it is the part of the fence that works.

### 3.4 Re-adoption and the host scan

The enumerators already exist, inverted, in
[`reset-server.py`](../scripts/reset-server.py) (`_list_vm_directories`,
`_list_units`, `_list_netns`, `_list_atlas_links`, `_list_ndp_proxy`,
`_list_atlas_lvs`). Boat's Go port uses the same enumeration to **ingest**: each
artifact is read, the Firecracker API socket is cross-checked against the unit's
state, and the observed document per VM is reconstructed by the same `Observe`
that steady-state observation uses — so adoption and the sweep can never report
one host two ways. **Every command in the scan is a listing, a stat or a boolean
gate**; there is no create, remove, start or stop anywhere in the package, which
is what makes "a scan never changes the host it is reading" a property rather
than a convention.

That cross-check is socket *presence*, not liveness. It answers the one question
coherence needs — a unit reporting active with no socket describes a process that
is not there — and it is deliberately weaker than the HTTP probe `fcattach`
performs, which is the test that would prove something is alive behind the socket
(§3.3, not wired).

**Ambiguous or partially torn-down artifacts — a crash mid-terminate — go to a
`quarantine` state requiring confirmation, never into the observed set.** A
half-deleted VM ingested as truth is a VM Atlas will try to start. The line drawn
is between **ambiguity and untidiness**: a stopped VM whose namespace outlived it
is untidy — its identity is not in doubt and booting it is safe — so it is
adopted as the VM it is; an active unit with no namespace is ambiguous, and is
quarantined with the evidence. Quarantine hides a UUID from the control plane,
and hiding a healthy VM has its own cost. **A partial scan fails whole**: any
host read that fails fails the scan and the daemon exits rather than serving what
it could not confirm, because "this host holds nothing" and "I could not read
this host" are the same document otherwise, and only one of them is survivable.
The quarantine set is reported in the export as its own array, keyed by whatever
identifier the host retained.

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

**Built: one golden corpus, at the layer that most needed it.** The quoting model
is generated from CPython's own `shlex` — every expectation in
`internal/run/shlex_conformance_test.go` is what `shlex.quote` / `shlex.split`
actually returned — so the Go command builder is held to the Python's rendering
byte for byte. Each ported verb also asserts its rendered command lines against
the Python verb's, in unit tests that need no host.

**The differential phase on a real host is NOT BUILT.** No harness runs the Go
verb beside the Python one on a live machine and diffs the host effects; the
verbs ported so far were cut over on unit-level equivalence plus manual
exercise. The port order above was followed only in part — the adoption scan and
the wake-trap loop landed early, firecracker re-attach is written but unwired
(§3.3), and network apply has not started.

The gate is honoured per module. Skipping it under schedule pressure re-creates
exactly the risk it exists to retire, and the risk has already been taken once —
so the harness is owed before WO-3's network apply, where a rendering difference
stops being a wrong command and becomes a VM off the network.

### 3.6 CLI and daemon

The `boat` CLI talks to the resident daemon over `/run/boat/boat.sock` (0660)
speaking the same HTTP/JSON API (§2.1). Built: `boat vm start|stop|ls|show
<uuid>`, `boat host facts`, `boat version`. *NOT BUILT:* `boat export` and
`boat adopt` — the export is reachable over the socket with `curl` and adoption
runs unconditionally at daemon startup, so neither is missing capability, only
missing spelling. This is also the operator's break-glass path when Atlas is
unreachable.

The socket lives at `/run/boat/boat.sock` and not `/run/boat.sock` for a reason
worth keeping: `/run` is root-owned `0755`, so a non-root daemon cannot create a
socket at its top level, and the alternatives were running as root or granting
`CAP_DAC_OVERRIDE` — both of which hand back exactly the blast radius §12 exists
to remove. `RuntimeDirectory=boat` gives the service user a directory it owns.

### 3.7 The unit set

`boat daemon` supervises the sibling units: it owns their start, stop and
restart, and surfaces each one's liveness in `GET /v1/host`. **It never reaches
into networkd's gossip state** — supervision is lifecycle only, and the ANCP
boundary of §0 stays intact.

**NOT BUILT (WO-3).** There are no sibling units to supervise yet (THE RULE),
there is no `GET|POST /v1/units/{name}`, and `GET /v1/host` reports hostname,
`boat_version`, the daemon's start time and the VM count — no unit liveness. The
export's `units` array is absent for the same reason (§2.5).

The unit template follows
[`atlas-networkd.service`](../scripts/systemd/atlas-networkd.service) for
`StartLimitIntervalSec` / `StartLimitBurst`, `RuntimeDirectory` +
`StateDirectory`, `Restart=on-failure` — and **deliberately not for `Type=notify`
or `WatchdogSec`**. The daemon does not call `sd_notify`, and a unit that claims
`notify` never reaches `active`: systemd waits for a `READY=1` that never
arrives and times the start out. `Type=exec` is the honest maximum today — it
holds the start until the binary has actually been exec'd, so a missing binary or
a bad service user fails the start instead of being reported as success. Same for
the watchdog: without a process patting it, `WatchdogSec` is a timed kill and not
a liveness guarantee. Both become the ANCP shape when the daemon learns
`sd_notify`, and not before.

## §4. Bootstrap, registration, re-adoption

> **NOT BUILT — WO-1b**, except re-adoption, which is §3.4 and is live. There is
> no `boat bootstrap` subcommand and no `POST /v1/bootstrap`; a host is still
> brought to Active by [`bootstrap-server.py`](../scripts/bootstrap-server.py)
> over SSH, and the Boat binary, its service user, its sudoers file and its unit
> are installed by hand from the Boat repo's README. There is no registration
> handshake: `Server.bootstrap()` is unchanged, `install.sh` still writes the
> `atlas` symlink and does not place `boat`, and the daemon's address and token
> come from site config (§2.3, §12) rather than from a host that registered
> itself.

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

> **NOT BUILT — WO-5b**, and hard-gated: nothing here may ship until §3.3's
> Firecracker re-attach has a caller, which it does not. There is no
> `POST /v1/update`, no `Server.boat_version` field, and no rollout driver. A
> host is updated by an operator replacing `/usr/local/bin/boat` and restarting
> the unit — which is safe today only because the daemon never relaunches a
> Firecracker (§3.3), so live guests survive the restart while the daemon comes
> back with no attachment to them.
>
> The desired-versus-running loop is half-built and worth naming: the **running**
> version already comes back in every export and lands on
> `Server.observed_boat_version`, so drift would be visible the moment a desired
> version existed to compare it against.

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

**Of Boat's four rows, one is built.** The park/wake reflex is live: Boat
installs the proxy-NDP entry, the `/128` route out the shared `atlas-park0`
dummy, and the counting SYN rule in the forward chain, and its resident trap
polls those named counters once a second and asks the reconciler for a pass. That
is the whole of Boat's networking today, and it exists because sleep needs it.
Everything else in the table — per-VM netns, veth, tap, NAT44, proxy-NDP and nft
isolation for a *running* VM, `local-ownership.json`, reserved-IP 1:1 NAT,
customer-gateway forwarding — is **NOT BUILT (WO-3)** and remains the
`firecracker-vm@` unit's Python hooks. Boat also does not yet supervise the
`networkd` unit (§3.7); it only inherits the boundary.

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

**Built, on the wire and in the mechanics.** The contract's `GuestIdentity` is
exactly the shape above — addresses, an `authorized_keys_blob`, and `extra_env`
as `{path, content}` pairs — and it rides on the rebuild request, the one verb
whose input is neither desired state nor a host fact. Boat copies every field
across as bytes: nothing in the path parses a key, validates an address, or knows
what a file called `/etc/anything` is for. It injects them into a freshly
laid-down rootfs with host keys regenerated and the hostname derived from the
UUID.

Two things are still owed. There is **no provision verb**, so the `ProvisionSpec`
above exists only as the rebuild half of itself — provisioning is still an SSH
Task (§4). And **Atlas does not yet fill the blob**: `BoatClient.rebuild_virtual_machine`
sends the operation identifier alone, so a rebuild driven from the desk lays down
a rootfs with no authorized keys and no env files. The seam is right and the
caller has not caught up.

### 7.3 Accepted constraint — dark and private-only VMs

`connection_for_guest` requires a public `/128`, which dark VMs
(`public_networking = 0`) lack. **For v1 the service layer supports public VMs
only**; private-only *service* VMs are out of scope. Tenant private-only VMs that
run no Atlas-managed service are unaffected. The future escape hatch is a single
bounded `boat.dial_guest(uuid, port) → socket` primitive — an L4 forward into the
VM's netns, with Boat never receiving a command, a shell, or the guest credential
— and Atlas's SSH layered over it. It is not specified here and not built.

## §8. Cross-host operations are sagas

> **NOT BUILT — WO-4.** [`migration.py`](../atlas/atlas/migration.py) has no Boat
> awareness at all: every phase below still runs as an SSH `run_task`, on a
> `boat_enabled` host as much as on any other. None of the sub-operations exist
> in the contract, and **Repoint does not bump `boot_epoch`**, which is the
> larger half of why the fence refuses nothing (§16.0). No host should carry
> production VMs through a migration until that is closed.

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

**Built: the autonomy, the freeze, and both halves of the resync.** Boat keeps
running with no control plane — no desired change means no action, the trap keeps
waking VMs, and the CLI keeps working. `boat_mirror` freezes the mirror and
flags the host `Unknown` on an unreachable Boat and writes nothing else: it does
not null a capacity total, touch a VM row, mark anything stopped, or evict. Atlas
re-`PUT`s intent before every verb and on `assert_desired_state`, and pulls the
export with `sync_mirror`. *NOT BUILT:* buffering observed transitions across the
blip (nothing replays what happened while Atlas was away — the export is the only
catch-up, and it carries current state rather than the transitions) and the
`/watch` resume. And **placement does not yet read `mirror_status`**: the field is
written and nothing consults it, so "an Unknown host gets no new arrivals" is
recorded but not enforced. That is the one bullet above whose failure is
silent — the mirror looks right and the scheduler ignores it.

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

`GET /v1/ops/{op_id}` is built, and so is the Task row's shape: a Boat-run verb
writes the same `stdout` / `stderr` / `exit_code` / `status`, through the same
`_mark_running` / `_finalize`, so nothing downstream can tell the transports
apart. **The live-progress half is NOT BUILT** (§2.7): the Task row goes
`Pending → Running → terminal` with the whole trace arriving at the end, so
`live_output` and `progress_line` stay empty on the Boat path.

The operator's audit surface stays the Task row plus the fleet-wide *Running
Operations* view, which now also reflects **Boat-reported observed transitions** —
a wake-trap flip, a crash-restart — giving a truthful fleet picture Atlas could
never show before. It reaches Atlas through the five-minute export sweep rather
than as it happens (§2.6), so the fleet picture is truthful and up to five
minutes old.

**Audit parity is a hard requirement, not an aspiration:** every operation writes
an append-only record equivalent to today's immutable `Task` and `SSH Command
Log` rows.

## §11. Correctness invariants

**These six were the gate**, and it held: all six were written here and reviewed
before any Boat code, because every one of them is cheap to build in and
expensive to retrofit. Each is stated as a rule with its failure mode: a rule
whose reason is missing gets deleted by the next person who finds it
inconvenient.

**Writing them first bought less than it looks.** Where each stands today:

| Invariant | Enforced? |
|---|---|
| 11.1 fence epoch | **Partly.** No fence means boot nothing — enforced on both boot paths. The epoch *comparison* refuses nothing (§16.0) |
| 11.2 CAS on contended reservations | **No.** No operation accepts `If-Match`; nothing returns 409 on a moved observed-epoch |
| 11.3 `desired_power` vs `observed_status` | **Yes**, including the precedence rule, though not in the shape the prose below describes — see the rule |
| 11.4 forward lease, union reconciliation | **No.** Boat allocates no address; the `held/ipv6` bucket does not exist |
| 11.5 write-ahead journalling | **No.** The package exists, is tested, and has no caller |
| 11.6 bbolt isolation | **Partly.** The export's short-`View`-then-release is real; the lock-free read and the `progress/` bucket are not |

### 11.1 The fence epoch

**Rule.** A per-UUID monotonic `boot_epoch`. **Atlas is its sole issuer.** It is
stored on the VM row and mirrored into each Boat's `fence` bucket on every
desired `PUT`. **Boat refuses to boot a UUID unless its local epoch ≥ the on-disk
unit's epoch AND desired `server == self`.** An **empty fence store means boot
nothing**. The epoch bumps at exactly one point: migration Repoint (§8).

> **This rule overstates what is built, and §16.0 is the full account — read it
> before trusting the fence.** Enforced: Atlas is the sole issuer (the mirror
> never writes a host-reported epoch back), the epoch may not regress, and a host
> holding no epoch for a UUID boots it on neither path. Not enforced: the epoch
> comparison, which is a tautology because the `PUT` writes the fence and the
> desired record from one document; and `server == self`, which cannot be checked
> at all because **there is no `server` field in the desired document**.

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

> **NOT BUILT — none of it.** No operation in the contract accepts `If-Match`, no
> handler compares an offered epoch against the store's, and the only `409` the
> `PUT` can return is a fence *regression*, which is a different check answering a
> different question. The observed-epoch itself is live and monotonic, so the
> token a CAS would match on already exists and is already carried in the export;
> what is missing is every reader of it. Until then, §1's "the mirror is
> disposable **because** no contended decision is taken from it" holds only
> because Atlas has not yet been taught to take one from it — the guarantee is
> the absence of a caller, not a mechanism. WO-3 needs this for reserved-IP
> attach and WO-4 for migration-target choice; whichever lands first owns it.

**Failure mode.** Drift on an observation field is a display nuisance; drift on a
reservation is corruption. Without CAS, two provisions read the same stale mirror
and both place into the last slot of RAM on one host, or one reserved IP is
DNAT'd to two guests. The mirror is disposable (§1) precisely because no
contended decision is ever *taken* from it — CAS is the mechanism that makes that
sentence true rather than aspirational.

### 11.3 `desired_power` versus `observed_status`

**Rule.** Verbs mutate `desired_power`, and **exactly one actor per VM** drives
that VM — a verb and a reconcile pass are one queue and can never run at once.
**Precedence: an explicit `desired_power = Stopped` outranks the wake trap — a
stopped VM is not woken by traffic.**

> **Built, and the one place to correct the prose is the phrase "verbs never
> touch the host directly."** They do — a verb runs the mechanics itself. What
> makes that safe is that it runs them **inside the turn it takes from that VM's
> actor**, the same turn a reconcile pass must hold, so the two serialize. Every
> handler reaches the host through one function that claims, takes the turn,
> runs, and journals, which is what makes the property structural rather than
> something each new handler has to remember. Atlas separately states desired
> state with a `PUT` before every verb, so the two halves of the rule are both
> real; they just live on opposite sides of the wire.
>
> The precedence rule is a branch taken **before** the reason for the pass is
> ever read: the Stopped half of the planner is not handed the trigger at all, so
> no future reason for requesting a pass can turn a Stopped desire into a start.
> The wake trap holds no policy of its own — it asks the reconciler for a pass by
> UUID and the planner decides — so an unauthenticated SYN cannot reach a boot
> except through this branch. Sleeping is treated as a *resting state of a Running
> desire*, so the periodic sweep leaves a parked VM parked and only a pass asked
> for by name resumes it; otherwise sleep-on-idle would free no RAM at all.
>
> The same precedence is enforced a second time, at the API, for the two verbs
> that would otherwise route around the planner: an explicit `wake` and an
> explicit `resume` are refused while the stored desire is Stopped. A host holding
> no desired record at all is not refused — there is no assertion to outrank, and
> refusing would leave a VM nothing could ever wake. `wake` is additionally
> fenced, because waking is booting; `resume` and `stop` are deliberately not,
> because both act on a guest already resident here and refusing either would
> strand it.

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

> **NOT BUILT — WO-6**, and correctly so: §6.2 keeps allocation in Atlas for v1,
> Boat allocates no address, and there is no `held/ipv6` bucket. The law is
> stated here because it is the **precondition** for moving allocation down, not
> because anything enforces it today. Nothing regresses meanwhile — the v1
> arrangement has one allocator — but note that Boat does not yet report its
> in-use set either, so the "or the host sees it" half has no source even for
> reconciliation.

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

> **NOT BUILT, in the way that is easiest to miss: the package is there and
> nothing calls it.** `internal/journal` records a decision durably before it
> returns, keys it so an operation's own decisions can be read back in order, and
> tells crashed operations from merely slow ones by stamping each with the
> **incarnation** of the daemon run that took it — an exact test where a timeout
> would have been a guess. All of it is tested. **`Record` has no non-test
> caller.** So the reconciler's startup resume reads an empty list every time, no
> verb re-enters at a checkpoint (§3.2), and the invariant enforces nothing.
>
> Two further gaps, both structural rather than oversights. The journal keeps its
> **own bbolt file** beside the store, because `internal/store` exposes no
> decisions bucket and bbolt holds an exclusive lock per file — so "allocation,
> LV create and operation completion commit in one transaction" is **not
> achievable** until the decisions bucket moves into the store. The consequence
> is bounded and is the safe direction: a crash between the two writes leaves a
> decision recorded for an operation whose outcome was not, and a replay reads
> the decision and finishes. And the store **cannot list its own operations**, so
> an operation a crash left `Running` with no decision recorded is invisible to
> every mechanism here; the reconciler's ordinary forward pass converges the VM,
> but the record stays non-terminal. Reserved-IP CAS is WO-3 and unbuilt (§11.2).

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

> **Built: the third clause, which is the one that bites first.** `Snapshot`
> materializes the whole export inside one short read transaction and returns it
> as a value before a byte is written out, so serialising to a slow client can
> never hold the file's page reclamation — a slow reader cannot make a healthy
> host look dead. The observed epoch is read in that same transaction as the
> records it describes, so a CAS token always belongs to state somebody saw. SSE
> readers touch the store only to read the epoch when publishing an event; the
> stream itself is served from an in-memory hub.
>
> **Not built: the other two.** Fence reads take an ordinary bbolt `View`, not an
> in-memory atomic snapshot — bbolt readers do not block behind the writer, so
> the property the rule is after mostly holds by construction, but it holds by
> bbolt's MVCC rather than by anything here, and a rule that is true by accident
> is a rule the next refactor breaks. There is **no `progress/` bucket and no
> writer for one**, which is untested rather than violated: no long operation has
> been ported yet. The first one that is — image sync, hydration, a RAM snapshot
> on a large guest — is where this invariant stops being theoretical, and it must
> land with its bucket rather than after it.

**Failure mode.** bbolt has a single writer. A 900-second image sync that holds
the write lock stalls the heartbeat; Atlas marks a perfectly healthy host
`Unknown`, placement stops sending it VMs, and the operator investigates a
partition that is really a busy disk. An export streamed *under* the lock is
worse still: the stall becomes proportional to the client's read speed, so a slow
Atlas can freeze a healthy host. **A busy host must never be mis-declared
partitioned** — every §9 behaviour keys off Unknown, so a false Unknown is a
false everything.

## §12. Security — no worse than today is the bar

- **Non-root daemon.** Boat runs as a service user under a **pinned NOPASSWD
  sudoers allow-list** written for it, `sudoers.d/boat`, modelled on
  [`sudoers.d/atlas-tunnel`](../scripts/sudoers.d/atlas-tunnel) — enumerated
  `wg`, `nft -f …`, specific `systemctl` and `firecracker` invocations. The
  individual privileged calls need root; the daemon does not. This is the single
  biggest blast-radius reduction the split buys — today Atlas SSHes to the host
  as root. **Built**: the unit runs `User=boat`, and the file enumerates every
  root command with its arguments — no wildcard shell, no `ALL`, no bare
  `systemctl`; the unit-scoped grants name `firecracker-vm@*.service` so they
  reach neither `sshd` nor `boat.service` itself. Two properties are worth
  keeping in the spec because they are easy to erode. **Each verb adds its own
  lines rather than widening an existing one** — widening the one line that can
  halt a guest is how a read-only probe acquires the power to stop VMs, so the
  Firecracker API grants are split per method and body, one alias each for the
  cooperative power-off, the guest-state change and the snapshot. And **every
  grant that could not be pinned tightly is named in the file as a residual
  risk** rather than left to be discovered: an `install(1)` whose source is a
  spool path the daemon controls, and the park rules' address and uplink
  wildcards, which are read from a VM's own `network.env` and so have no shape to
  pin to. A grant whose looseness is argued in place is one the next reviewer can
  re-argue; a grant that is merely loose is one nobody notices. The unit also
  carries **no sandboxing beyond
  `User=`/`Group=`**, deliberately: `NoNewPrivileges=` breaks setuid `sudo`
  outright, and `ProtectSystem=`/`PrivateTmp=` would confine the root children
  `sudo` spawns too. The privilege boundary is the allow-list, and anything added
  to the unit has to be checked against it.
- **Verb allow-list enforced at the API boundary.** **There is no
  arbitrary-command endpoint, ever** — that one holds by construction: the
  generated router serves only the operations `api/openapi.yaml` describes and
  answers anything else 404, and the runner takes parameterized templates rather
  than assembled strings. *Porting `scripts_catalog.allowed_scripts()` into the
  API layer is NOT BUILT and is not needed while the contract is the allow-list;
  it becomes needed at WO-6, when a generic `boat <verb>` surface exists.* The
  ad-hoc surface stays the SSH Console ([04-tasks.md](./04-tasks.md)), which is
  operator-only and fully logged.
- **Short-lived scoped tokens** (or mTLS over the tunnel), minted per host by
  Atlas. **Rotation under partition:** if Boat cannot reach Atlas it serves the
  last valid token until a **hard expiry**, and Atlas re-mints on reconnect.
  There is never an unreachable-and-trusting-a-stale-token-forever window, and
  never a partition that locks the operator out of their own host. **NOT BUILT —
  WO-1b.** Today the token is a **static, non-expiring** per-host value: Boat
  reads it from a file the operator placed, Atlas reads it from site config
  (`atlas_boat_tokens`, or `atlas_boat_token` for a single-host bench). It is
  never logged and never put in an error message on either side, and the tunnel
  is still the transport boundary — but a leaked token is good until someone
  changes it by hand.
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
  `/etc/atlas-networkd/local-ownership.json` — *both NOT BUILT, WO-3; today it
  neither supervises the unit nor writes the file, and the `vm-network-up` /
  `vm-network-down` hooks remain its writer.* What holds already is the
  prohibition: Boat **never touches membership, the ownership table, the
  generation counters, or the wg peer table**, and nothing in it reads or writes
  ANCP state of any kind. Porting
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

| WO | Status | Ships |
|---|---|---|
| **WO-0** | **SHIPPED** | Walking skeleton: a `boat` binary that starts, serves the API on the tunnel and the unix socket, persists to bbolt, and starts/stops one real VM driven from Atlas through `BoatClient`. |
| **WO-1** | **SHIPPED, less re-attach** | Observed state: adoption scan, ~~firecracker re-attach~~, `GET /v1/export`, `/watch` SSE, the `Host State Snapshot` mirror, and the fence store — advisory only, the DB still authoritative. `internal/fcattach` is written and has no caller (§3.3), so the hard gate on WO-5b is **not** satisfied. |
| **WO-1b** | not started | `boat bootstrap`: a bare host brought to Active by the binary itself, with the armed auto-revert registration handshake (§4). |
| **WO-2** | **IN FLIGHT** | Full lifecycle and reflexes: every VM verb through Boat, the per-VM reconciler, the journal, and the wake trap resident in Boat; per-VM authority flips to Boat. Landed: all nine lifecycle verbs end to end, the per-VM reconciler and single actor, the resident wake trap, the guest-identity blob on the wire, and the five-minute mirror sweep. Outstanding: the journal has no caller (§11.5), `observed_authority` is never read so authority has not flipped (§1), Atlas does not fill the identity blob on a rebuild (§7.2), and there is no `/watch` consumer (§2.6). |
| **WO-3** | not started | Unit supervision and host-local network apply: the sibling units re-pointed at `boat <sub>`, `local-ownership.json` written by Boat, reserved-IP NAT and gateway forwarding applied under CAS (§11.2, which WO-3 must build). |
| **WO-4** | not started | Cross-host sagas: migration, warm fan-out and S3 sync driven over Boat RPCs, with Repoint gated on positive source fencing (§8). **Owns closing §16.0.** |
| **WO-5** | not started | `boat networkd`: the ANCP daemon in Go, same binary, own unit, byte-identical wg and nft output — a port, not a redesign (§13). |
| **WO-5b** | not started | Auto-update (§5). Hard-gated on WO-1's firecracker re-attach; cannot ship before it. |
| **WO-6** | not started | Verb-port completion and cutover: the remaining verbs as `boat <verb>`, the venv and durable package retired, public-IPv6 allocation pushed down **only** once §11.4 is proven. SSH break-glass and `connection_for_guest` are **not** deleted. |
| **Track S** | not started | Services de-fusion (§13, last bullet). Parallel, no Boat dependency; one service moved at a time, green each commit. |

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

**What has actually been verified, three work orders in:** the invariant review
happened; Boat's own suite is green under `-race` and covers every package with a
host seam by faking that seam; and the Atlas side has ~1600 lines of unit tests
across `test_boat_client`, `test_boat_lifecycle` and `test_boat_mirror`. **What
has not:** there is **no differential harness** (§3.5), **no partition drill**,
and — most consequentially for this list — **no e2e use case**, so
`run_all_smoke` exercises no Boat path at all and the Boat transport is not
covered against a real droplet by anything but hand. A `boat_lifecycle` use case
under [`atlas/tests/e2e/use_cases/`](../atlas/tests/e2e/use_cases) is owed, and
it is the check that would have caught the class of defect this chapter's audits
keep finding: a component that exists, passes its unit tests, and is wired to
nothing.

## §16. Open

Everything else in this chapter is **decided**, which is a weaker claim than
built — the `NOT BUILT` markers throughout say which decided things have no code
behind them. These are the questions that have no answer yet.

0. **The fence epoch does not yet refuse anything except an empty store, and
   §11.1 currently overstates what is built.** This is the largest gap between
   this chapter and the code, and it is listed first because it reads as
   finished from every direction: the gate is consulted on both boot paths, the
   store refuses a regression, and the error type exists.

   What works: a host holding no epoch for a UUID boots nothing. That is the
   rule that saves a Boat which lost its bbolt file, and it is enforced.

   What does not: the epoch *comparison* is a tautology. `PUT /vms/{uuid}`
   writes the fence and the desired record from one document, so the held epoch
   and the desired epoch are equal by construction and a stale epoch cannot be
   detected. Two things have to land before it means anything, both in Atlas:

   - **Atlas must bump the epoch at a migration's repoint.** Nothing in Atlas
     writes `boot_epoch` today beyond the initial 1, and `migration.py` has no
     Boat awareness at all. §11.1 names repoint as the single bump point; that
     bump does not exist.
   - **Atlas must be able to retract or supersede desired state on a host that
     no longer owns a VM.** There is no `DELETE /vms/{uuid}` and no `server`
     field in the desired document, so a source Boat keeps `{epoch, Running}`
     for an evacuated VM forever and its sweep will start it again the moment it
     observes it stopped.

   Until both exist, split-brain is prevented by phase ordering and
   `desired_power` — which §9 says explicitly is *not* what should prevent it.
   The honest statement of today's guarantee is: **Boat will not boot a VM it
   was never told about; it will boot a VM it was told about and then forgotten
   about.** WO-4 owns closing this, and no host should carry production VMs
   through a migration until it is closed.

1. **Who writes ANCP's bootstrap trust artifacts after the split** (§4). The
   constraint is fixed — they are operator-signed, Atlas holds the key, Boat must
   never hold it, and they must land before `boat networkd` starts — but whether
   Atlas writes them in the same SSH step that lands the binary or `boat
   bootstrap` requests them during registration is not settled.
2. **API version negotiation.** Atlas must speak `[vN-1, vN]` of the Boat API for
   at least one release window, negotiated on connect. The paths are `/v{N}`; how
   a client discovers `N` is unspecified.
3. **The Boat-side equivalent of `sleep-vm`'s wake-trap gate — decided, and it
   currently names the wrong unit.** Boat's sleep is a hard precondition,
   checked before anything touches the VM: it refuses when the wake trap is not
   active, because a slept-but-unparked VM answers nothing until an operator
   clicks Start and fails *silently*, which is strictly worse than leaving the VM
   awake ([32-sleepy-vms.md](./32-sleepy-vms.md)). What it probes is
   `systemctl is-active atlas-wake-trap.service` — the **Python** unit — while
   Boat's own trap runs in-process (THE RULE). On a host with both, the gate
   passes for the wrong reason and two traps poll one set of counters; on a host
   with only Boat, a correct sleep is refused. The precondition is right; the
   thing it asks is one unit out of date, and the fix belongs with WO-3, when the
   Python trap is retired.
4. **Quarantine resolution** (§3.4). A partially-torn-down VM lands in quarantine
   requiring confirmation; the operator surface and the API that clears it are
   not specified. Boat reports the set in every export, keyed by identifier and
   carrying its evidence; Atlas has nowhere to put it — `boat_mirror` reads a
   per-VM `quarantined` flag that the export does not populate and ignores the
   `quarantine` array entirely, so a quarantined artifact set reaches an operator
   only through the archived document.
5. **One number**: the hard expiry on a token served under partition (§12). The
   `Host State Snapshot` retention bound is answered — **20 epochs per host**,
   chosen as a first answer rather than derived, which keeps a 100-host fleet at
   2000 rows and still leaves an operator a run of recent epochs to diff a drift
   against.
6. **Dark-VM service reach** (§7.3) is deferred, not open — the `dial_guest`
   escape hatch is named as a shape, and specifying it is a decision for whenever
   a private-only service VM is actually required.
