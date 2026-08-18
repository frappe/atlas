# Boat — the per-host daemon, and what Atlas keeps

> **Status: PARTLY BUILT.** The spec-first gate held — §11's invariants were
> written and reviewed before any Boat code — and **most of the delivery order
> (§15) is built**: WO-0/WO-1 shipped, WO-1b/WO-3a/WO-3b/WO-5/WO-5b built (WO-5
> dogfooded), with WO-2/WO-4/WO-6 still carrying the gaps §15 lists.
> `boat` is a running daemon with an adoption scan, a whole-host export,
> a per-VM reconciler, a resident wake trap and every lifecycle verb, and Atlas
> drives every real host through it — the `Server.boat_enabled` flag that used
> to gate it is gone, and so is the SSH path it fell back to. `make check` is
> green. The repository is `github.com/frappe/boat`; the contract IDL is
> `api/openapi.yaml` in that repo and **this chapter governs it** — the Boat
> repo's README points here. Per-work-order status, including what WO-2 still
> owes, is in §15.
>
> The chapter carries both the design and what of it exists. **Every claim not
> yet true of the code is marked `NOT BUILT` where it is made, naming the work
> order that owns it**; everything unmarked is in the code. A claim is only ever
> worth the call site that makes it true — a package that is written, tested, and
> called by nothing enforces nothing — so §15 tracks each work order's real state.

## The split

Atlas today is a smart controller driving dumb hosts over SSH verbs, with the
Frappe DB the source of truth and the host "a rebuildable cache"
([04-tasks.md](./04-tasks.md), [01-architecture.md](./01-architecture.md)). That
is no longer what the fleet does: **the host already decides** (§0). What it lacks
is a store, an API, and a name; this chapter gives it all three and draws the line.

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

Two worked consequences: the sleep/wake *reflex* is Boat's, its *enrolment*
(`sleep_on_idle`, the per-VM firewall map) is Atlas policy handed down
([32-sleepy-vms.md](./32-sleepy-vms.md)); and Boat self-reports raw capacity
numbers only — the Sleeping-axis billing accounting stays in Atlas
([`server_capacity.py`](../atlas/atlas/api/server_capacity.py),
[28-placement.md](./28-placement.md)), never leaking "what it costs" into the daemon.

A third tier sits above both and is unaffected: **Atlas-services** — proxy,
gateway, bench-site, TLS — a capability attached to a VM *after* it exists,
driven over a plane Atlas owns (§7). Core VMs and Boats are oblivious to it.

## §0. The precedent — this is the third resident daemon, not the first

Two shipped chapters already overturned "no agent runs on the server," and Boat
claims them as precedent rather than re-arguing it:

- **ANCP** ([31-ancp-network-control-plane.md](./31-ancp-network-control-plane.md),
  shipped) put an authoritative gossip daemon on every host and deleted the
  controller's networking module. Three consequences bind Boat: the mesh is **not
  in the Atlas ↔ Boat contract at all** (no "Atlas computes peers, Boat applies");
  the VM↔network seam is already a file — ANCP §11.3's
  `/etc/atlas-networkd/local-ownership.json`, which Boat simply becomes the writer
  of; and the Taste exception for a resident host daemon (ANCP §6,
  [README](./README.md) principle 5) is already granted and inherited.
- **Sleepy VMs** ([32-sleepy-vms.md](./32-sleepy-vms.md), shipped) put a resident
  wake-trap daemon on every host that wakes a parked `/128` on the first inbound
  SYN with no DB consult — the purest instance of the dividing rule, absorbed
  natively.

Underneath both, `firecracker-vm@.service` rebuilds netns/routes/nft/disk from
`network.env` after a reboot and `atlas-pool.service` rebinds the loopback PV,
neither consulting Atlas. **What that leaves for Boat:** VM lifecycle, per-VM
networking, observed state, adoption, crash recovery, cross-host op execution, and
supervision of the sibling units.

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
at WO-5, `pool` / `mgmt-firewall` / the `firecracker-vm@` hooks at WO-6. WO-3a
supervises those units where they are — as the Python entry points they are
today — because supervision and re-pointing are separable and only the first is
needed before self-update (§5, §3.7).

Two lines are struck rather than deferred. There is no `boat gateway`:
`gateway.service` runs inside the customer gateway *guest* (§7.1), never on a Boat
host. And there is no `boat wake-trap` unit — the reflex runs as a goroutine inside
`boat daemon`, because the "operator's `Stopped` outranks a stranger's SYN" rule
lives in the reconciler's planner (§11.3) and a separate process could only reach
it by making an unauthenticated packet an API client; one build artifact per host
is the rule, and an in-process reflex does not weaken it.

This is **not a new grammar** — `_cli.py` is already a multi-call dispatcher and
`install.sh` already symlinks `/usr/local/bin/atlas`. `boat` takes that symlink's
place **at WO-6**; until then both coexist (`atlas` for un-ported verbs, `boat` for
ported). The scope is the whole host surface: every `scripts/*.py` verb,
`scripts/lib/atlas/` and `networkd/` module, and `scripts/systemd/` unit.

**What it buys** is two bug classes deleted: the **durable-package staleness
class** outright (no `/var/lib/atlas/bin` shadowing, venv, or "re-bootstrap to
refresh the lib" contract, [04-tasks.md](./04-tasks.md)); and **version skew
between a host's components**, collapsing today's five invocation styles to one
build. **What it costs** is the hard part: one binary swap re-points *every* unit
at once, so an update restarts the daemon under live VMs — **which is exactly why
self-update is hard-gated on re-attaching to a running Firecracker rather than
restarting it** (§3.3, §5).

## §1. Source of truth — desired versus observed

Atlas remains authoritative for **desired state** (intent); Boat becomes
authoritative for **observed state** (fact). **This refines
[01-architecture.md](./01-architecture.md) principle #2, it does not reverse it:**
a lost host is still rebuildable *from desired state*. What changes is the reverse
direction — Atlas keeps a **read-through mirror** of observed state that is
**disposable and never authority**. Losing it costs one `GET /v1/export` (§2.5);
nothing is ever rebuilt *from* it, and nothing is decided by it that a CAS verb
(§11.2) does not re-check against the host.

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
unit's `ActiveState`/`SubState`, the on-disk markers, and — for a VM the unit
calls active — the guest's own state read back from the Firecracker API socket.
**Never** from a command having succeeded.

That last source is what produces Paused (a paused guest's unit is still active,
so systemd alone read it as Running). **Atlas's `observed_status` Select still
carries no Paused**, so a Running observation is accepted against a DB status of
either Running or Paused — `NOT BUILT`, WO-3b.

**What Boat reports today.** The export carries a subset of the observed rows. Live:
`observed_status`, `ActiveState`/`SubState`, `has_memory_snapshot`, `sleeping`, every
fence epoch, the quarantine set, and the host facts (`vcpus_total`,
`memory_megabytes_total`/`_free`, `pool_disk_gigabytes_total`, `pool_used_percent`,
`host_signature`, kernel/Firecracker/`boat_version`). **NOT BUILT:** `last_started`,
`last_stopped`, `last_traffic_at`, `boot_id`, the `observed_*` resource numbers,
`public_ipv4` (WO-3b), the running Firecracker PID (§3.3), and the LV inventory (§2.5).
Of the per-VM fields Atlas's mirror lands, `observed_status` is the only one
([`boat_mirror.py`](../atlas/atlas/boat_mirror.py)).

**The drift guard, reworked.** Today `virtual_machine.py` `validate()` freezes the
resource fields (`RESIZE_MUTABLE`) for lack of a truthful readback; with Boat
owning observed state, drift (`desired ≠ observed`) becomes a **surfaced state**
that Boat's reconciler drives toward desired. **But drift is corruption, not a
display nuisance, for contended reservations** (`server`, `ipv6_address`, the
reserved-IP slot, the capacity gate) — those stay
**frozen-except-through-CAS-verbs** (§11.2). Identity immutability
(`IMMUTABLE_AFTER_INSERT`) is unchanged.

`Virtual Machine.observed_authority` ∈ {DB, Boat} (default DB) gates per-VM whether
Boat's observation wins; it **exists but nothing reads it yet** (the mirror is
advisory, recording drift without acting — flipping it to Boat is WO-2's remaining
half). `Server.boat_enabled`, which gated whether Atlas called a Boat at all, has been
**REMOVED**: every host is on Boat, the ten host verbs run as `boat <verb>`
(`scripts_catalog.BOAT_VERBS`), no second transport to fall back to. A Fake-backed
server never gets a Boat call (`BoatClient` honours `is_fake_server()`).

## §2. The Atlas ↔ Boat contract

### 2.1 The API is the complete functional surface

**Every capability Boat has is an endpoint** — lifecycle, bootstrap (§4),
self-update (§5), sibling-unit supervision, host facts, whole-host export. The
`boat` CLI and the systemd units are **clients of that same surface**, never
alternate paths with powers the API lacks: a capability reachable only from the
CLI is one Atlas cannot audit, replay, or recover after a partition.

### 2.2 Transport — HTTP/JSON over the management tunnel, typed by an IDL

- **HTTP/1.1 + JSON on the wire, SSE for streams** — not gRPC, which would violate
  few-dependencies and become every downstream app's dependency; the Atlas-side
  client has the shape of [`digitalocean.py`](../atlas/atlas/digitalocean.py).
- **A real IDL.** `api/openapi.yaml` in the Boat repo is the source of truth: the
  typed **Go server** is generated from it (`oapi-codegen` pinned, `internal/wire`)
  so a drifting handler fails to compile, while the **Python client is
  hand-written** ([`boat_client.py`](../atlas/atlas/boat_client.py)), one method per
  endpoint — *generating it is NOT BUILT*, so the Atlas side's conformance is a
  review property, not a compile-time one. Explicit `/v{N}` path versioning; the
  daemon mounts the router both bare and under `/v1`.
- **Boat listens only on the management-tunnel address and `/run/boat/boat.sock`,
  never on a public interface.** The tunnel is the transport-security boundary, as
  for Central ↔ Atlas ([21-tunnel.md](./21-tunnel.md)). The socket is always bound;
  the tunnel listener (`--listen`) is unset in the shipped unit until an operator
  adds the address (registration is WO-1b). Refusing a TCP listener without a token
  is enforced; *nothing enforces that the address is not a public one*.
- **Boat shares the Central-managed tunnel; there is no second tunnel.** A tunnel
  re-provision is a handled event, not an outage: backoff on reconnect, observed
  reports buffered across the blip, no operation lost.

### 2.3 Auth and least privilege

Summarised here, specified in §12: non-root daemon under a pinned sudoers allow-list;
the verb allow-list at the API boundary; short-lived per-host tokens minted by Atlas.
Unix-socket requests are authenticated by peer credentials instead (the socket is
`0660`, group-owned). `GET /v1/health` is the one unauthenticated operation, reporting
nothing a caller should not see. Built: the non-root daemon and sudoers file, the
peer-credential model, the constant-time bearer check (an unset token matches
nothing), and the exempt `/health`. **Minting is NOT BUILT** (WO-1b): the token is a
static per-host value from site config (`atlas_boat_tokens`, §12).

### 2.4 The operation set

All operations are idempotent, replayable and versioned. Every mutating **verb**
carries an `op_id`, and one arriving without it is refused rather than run — a
verb that cannot be recognised as a replay is a verb a retry boots twice. The
desired-state `PUT` deliberately carries none: it is idempotent by value, so
re-sending the same document is a no-op with nothing to deduplicate.

- **A. Desired-state apply — the durable primitive.** `PUT /v1/vms/{uuid}` with
  the full desired spec and `boot_epoch`, fence first then intent. It **records
  and returns; it starts and stops nothing** — convergence is the reconciler's
  next pass, so a `PUT` alone can wait a sweep interval (Atlas never relies on
  that; it posts the verb straight after). This is how Atlas re-asserts intent on
  reconnect.
- **A′. Desired-state retract.** `DELETE /v1/vms/{uuid}` — the `PUT`'s mirror and
  the only way an assertion is taken back. It touches nothing on the host; what
  ends is this host's authority to act on the UUID (§1). **It does not clear the
  fence epoch**, by design: retraction removes an authority and must not hand back
  permission to boot, or an evacuated source would accept a stale epoch and boot a
  moved VM (§11.1, §16.0). Idempotent. `terminate` performs the same retraction
  inside the verb, since a destroyed VM still desired Running is one the sweep
  restarts forever.
- **B. Lifecycle verbs.** `POST /v1/vms/{uuid}/<verb>` for `start`, `stop`,
  `pause`, `resume`, `sleep`, `wake`, `resize`, `rebuild`, `terminate` and
  `reserved-ip`, plus the migration sub-operations of §8 at
  `POST /v1/vms/{uuid}/migrate/{phase}`. All built.
- **B′. Host verbs.** `POST /v1/host-verbs/{verb}` — the verbs that were `boat
  <verb>` over SSH. One endpoint, not a typed path each: the request is
  `{operation_id, variables}` (the same UPPER_SNAKE dict the SSH runner used) and
  the reply is the usual `Operation` record, journaled by `op_id` and serialized on
  the VM's or one host-wide turn. Unlike a lifecycle verb it needs no prior `PUT`
  and takes no fence (`provision-vm` CREATES the VM). **Served today** for the six
  that reach no ungranted privileged command (`snapshot-vm`, `snapshot-stop-vm`,
  `delete-snapshot-vm`, `regenerate-host-keys-vm`, `firewall-apply`,
  `export-cleanup-source`). **Not yet served:** verbs needing new scoped grants
  proven on a host (`provision-vm`, `sync-image`, `promote-snapshot-image`,
  `warm-snapshot-vm`, the s3 backups) — those stay on SSH
  `boat <verb>`. `bootstrap` (§4) and `reset-server` never move here.
- **C. Observed read and watch.** `GET /v1/vms/{uuid}`, `GET /v1/vms`,
  `GET /v1/host`, `GET /v1/export` (§2.5), `GET /v1/watch` (SSE deltas).
  `PUT /v1/vms/{uuid}` takes the CAS `If-Match: <observed-epoch>` of §11.2.
  *NOT BUILT:* operation progress on the stream (§2.7).
- **D. Host control.** `GET|POST /v1/units/{name}` — sibling-unit supervision
  (§3.7). The POST is the one mutating op with no `op_id`, and the exemption is
  argued: a unit action is idempotent by outcome (`start`/`restart` both converge
  to "running, now"), and a retry storm is bounded by the unit's own
  `StartLimitBurst`, which Boat is deliberately not granted `reset-failed` to
  clear. *NOT BUILT:* `POST /v1/bootstrap` (§4, WO-1b) and `POST /v1/update`
  (§5, WO-5b).
- **E. The journal.** `GET /v1/ops/{op_id}` (§10) and the unauthenticated
  `GET /v1/health` (§2.3). Both built.

**How a verb reaches the host.** Atlas `PUT`s desired state before every Boat-routed
verb, then posts the verb, which says "now". The verb **runs the mechanics itself
inside the turn it takes from that VM's actor**, so a verb and a reconcile pass are
one queue per UUID (§11.3). Verb **names** are the host CLI's (`start-vm`,
[`scripts_catalog.py`](../atlas/atlas/scripts_catalog.py)) but the wire is JSON:
`run_boat_task` maps a verb to its endpoint and the typed operation record replaces
the scraped `ATLAS_RESULT=` line (§2.7). **Almost every verb's whole request is its
`op_id`** — a resize reads its numbers from the store, not the wire; `stop` carries
the two non-desired knobs (`graceful`, `stop_timeout_seconds`) and `rebuild` the
source and guest identity to inject (§7.2). A verb taking from the wire a per-VM
number the store could hold is the shape to refuse in review.

### 2.5 Whole-host export and one-shot sync

`GET /v1/export` returns Boat's **entire** observed state in one document: every
VM's observed doc, host facts, LV and thin-pool inventory, network state,
sibling-unit liveness, the quarantine set, every fence epoch, and the running
`boat_version`. Atlas ingests it **in one transaction** to rebuild its mirror.

**Two of those are NOT BUILT and the document omits them:** the LV inventory
(adoption enumerates it and drops the result — the fix is a store bucket) and
**network state** (WO-3b owns per-VM networking). Omissions are absences, not empty
arrays: an empty `logical_volumes` would falsely assert "no volumes". The two
arrays where **empty is a claim** are sibling-unit liveness (WO-3a — `[]` means
"asked, runs none") and quarantine (below); host facts and the LV inventory are the
opposite, where absent means "not looked at" and the last value stands.

**State the symmetry:** `PUT` desired is how Atlas re-asserts intent;
`GET /v1/export` is how Boat re-asserts fact. Run back to back they resynchronize a
host from any state — the whole recovery story, with four consumers each a hard
case: reconnect after partition (§9), first adoption (§3.4), rebuilding a lost
mirror, and the post-update health check (§5). It supersedes the periodic `GET
/vms` sweep as the backstop; `/watch` SSE remains for low-latency deltas.

Three properties are load-bearing:

- A **consistent snapshot**: one short bbolt `db.View`, materialized and released
  *before* streaming, never under the write lock (§11.6).
- A monotonic **observed-epoch**, read in the same transaction as the records it
  describes, so §11.2's CAS verbs can `If-Match` the exact snapshot Atlas ingested.
  The epoch is live and Atlas orders ingests by it — *the `If-Match` half is NOT
  BUILT* (§11.2).
- The running **`boat_version`**, closing the update loop: Atlas pushes a desired
  version (§5) and observes the running one, so version drift is ordinary observed
  state.

**Atlas lands the export in two places:** hot fields denormalized onto `Server`
(capacity, unit liveness, the quarantine set, `observed_boat_version`) because
placement queries them on every provision, reusing the capacity fields **Refresh
Capacity** already stamps ([28-placement.md](./28-placement.md)); and the full
document archived as a **`Host State Snapshot`** row keyed by host and epoch, for
debugging, mirror rebuild and drift forensics (retention bounded — 20 epochs per
host — so the mirror stays obviously disposable, each row carrying its ingest-time
drift list). `observed_quarantined` is the one field whose **absence is a claim**:
Boat omits the array when there is nothing to report, so absent means *none*; a
half-terminated VM reported as merely absent would be indistinguishable from a
cleanly deleted one, so its `quarantined` drift row **suppresses** the `absent` row.

**Freshness and epochs.** An ingest at an epoch already held re-lands nothing but
re-stamps freshness, because the epoch only moves on an observed *change* and a
quiet host reports the same number forever. **Zero is a legitimate epoch**, so
absent and zero differ. A *lower* epoch is normally a late poll to be dropped — but
a Boat that lost bbolt counts again from below, so the **fences** disambiguate: a
regression with fences intact is a no-op, one from a host that forgot what it was
told is adopted and lands `Unknown`. `Fresh` is not the reward for answering:
`mirror_status` goes `Unknown` both when the daemon did not answer (§9's freeze) and
when it answered holding no fence for a VM Atlas placed there (§11.1's most
dangerous state — the host will boot nothing). Both stop placement filling the host,
neither evicts, and both clear themselves — the next successful export, or the next
`PUT` of desired state.

### 2.6 Push commands, pull truth

Commands push (Atlas → Boat); state is pulled. The design is **push for liveness,
pull for truth** — a lost pushed update has no self-healing path, a pulled one
re-converges on the next sweep. Designed: a `/watch` SSE stream plus a signed HMAC
heartbeat on significant transitions, with the §2.5 export as backstop.

**Only the pull half exists.** Boat serves `/watch` and publishes on every
observation, but **Atlas has no SSE consumer** and the **signed heartbeat is NOT
BUILT**. What is built is the right half first — `boat_mirror.sweep_mirrors` runs
every five minutes and enqueues one `GET /v1/export` ingest per host — so the
mirror's freshness bound is the sweep interval: a host that becomes unreachable is
flagged `Unknown` within five minutes, not seconds.

### 2.7 A Boat operation and a Frappe Task row

Boat keeps its own operation journal, crash-recovery truth: one record per `op_id`,
written on claim and rewritten exactly once on completion, **first completion
wins** (never removed, never overwritten). The Frappe `Task` row stays Atlas's
operator-facing audit and replay record, and **they share one identifier:
`op_id == Task.name`.**

The designed sequence: Atlas creates the Task (`Pending`), calls Boat with
`op_id = task.name`, Boat streams progress over SSE and Atlas folds each chunk onto
`live_output` / `progress_line` through the existing `task_log` event
([22-observability.md](./22-observability.md)), and on completion Boat returns the
typed result. **The streaming middle is NOT BUILT.** Today the verb is one bounded
request: Boat runs it to a terminal record before answering, and `run_boat_task`
folds `output`→`stdout` and `error`→`stderr` in one `_finalize` — the same row
shape `run_task` writes, so nothing downstream can tell which transport ran; a
non-terminal status coming back is treated as a protocol surprise and raised.

**Boat returns a typed result.** `Operation` carries one optional free-form `result`
object (`additionalProperties: true`) holding the same payload an SSH script's
`ATLAS_RESULT=` line would — populated today by `sleep-vm` alone (`memory_snapshot`,
plus `reason` / `memory_snapshot_bytes`), and serving `snapshot-vm` /
`warm-snapshot-vm` when those move onto Boat. **Absent is not false:** a verb with
nothing to report — or one that FAILED — omits it, since a failed verb's half-result
is not a value to act on. It rides in the journal, so a replayed `op_id` answers with
the first attempt's result.

The reading is done **in the transport, not at the call site**
(`boat_client.OPERATION_RESULT_FIELD`): a present `result` becomes the same
`ATLAS_RESULT=` line an SSH script would have written, so `task_results.parse_result`
reads one Task the same way either way. The caller stays tolerant regardless
(`parse_optional_result`) — a `sleep` that cannot learn whether RAM was dumped still
marks the row `Sleeping`, because the VM is parked either way. (Insisting on the line
was silent in every direction: the VM parked, the Task committed `Success`, the row
stayed `Running`, and the idle sweeper re-slept it every minute forever.)

**Replay never double-runs.** Re-POSTing an in-flight or completed `op_id` returns
the recorded operation and runs nothing; an `op_id` recorded against a *different*
verb or VM is a `409`. The claim is a single store transaction, so two concurrent
posts of one identifier can never both come back claimed.

## §3. Boat internals

### 3.1 Store — bbolt

Single file, pure Go, zero CGO, transactional. Records are **indented JSON**
deliberately: on a wedged host an operator has only `strings` and a hex dump, and
every key is one they can also search for in Atlas.

Buckets, as built: `virtual-machines`, `operations`, `desired`, `fence`,
`quarantine` (latest-wins per scan, so a resolved quarantine stops being reported),
and `meta` (observed epoch plus the incarnation counter that tells a crashed
operation from a slow one). `decisions` holds §11.5's write-ahead records; both it
and the counter live here, not a separate file, because a decision and the operation
it justifies must commit together. *NOT BUILT:* `alloc/ipv6` and `held/ipv6`
(forward leases, §11.4 — WO-6) and `progress/` (§11.6, no writer yet).

**On-disk artifacts — `network.env`, LV names, markers — are the ground truth Boat
re-derives on adoption; bbolt is the fast transactional index, not a second truth.**

### 3.2 Reconciler and forward-only state machines

Every operation is a **forward-only** state machine — ordered, idempotent,
checkpointed steps, always run forward, never unwound — generalizing the discipline
[`migration.py`](../atlas/atlas/migration.py) already encodes (`PHASE_ORDER`). A
background **reconciler** drives observed toward `desired_power` and spec, and
**one actor per VM** serializes the reconciler and any verb (§11.3).

Built: forward-only, idempotent, and the one actor per VM. The reconciler sweeps
every desired record every 30 seconds and runs a pass on demand; a pass takes the
**one** step that closes the gap and leaves the rest to the next, so a pass killed
half-way leaves nothing to unwind. A VM that cannot converge backs off per VM,
doubling to a five-minute cap.

***Re-entry* at a checkpoint is NOT BUILT.** An interrupted verb is not resumed
where it stopped: the reconciler's ordinary forward pass converges the VM and the
interrupted record is closed as a failure naming the restart. That is safe for eight
of the nine verbs (idempotent from the top); `rebuild` writes its source down ahead
of the destructive step (§11.5) so the decision survives. It stops being safe-enough
at the first verb that allocates — the boundary §11.5 draws, arriving with provision
and migration.

### 3.3 Crash recovery — re-attach, do not restart

On start Boat scans for running firecracker processes and their per-UUID API
sockets (ported from [`paths.py`](../scripts/lib/atlas/paths.py)),
**re-attaches**, replays the `ops` journal, and rebuilds the observed store from the
host scan (§3.4). **Re-attach is load-bearing beyond crash recovery: it is what
makes auto-update safe**, because a binary swap restarts the daemon under live VMs
(§5); a Boat that restarts firecracker to regain control cannot be upgraded without
an outage.

**Built: the scan and the property re-attach protects.** Startup runs the §3.4 scan
before opening a listener and serves from it. Crucially Boat **never launches or
relaunches a Firecracker on startup** — the VMs are not its children, its unit is
not ordered `Before=firecracker-vm@.service`, and a restart under live guests leaves
every one running. That is the guarantee auto-update needs, and it holds today.

**Built: the re-attach.** `internal/fcattach` finds a live Firecracker by *talking
to* its per-UUID socket — the only honest liveness test, since a socket inode
outlives the process that bound it — with two callers: the adoption scan's coherence
cross-check, and `Observe` confirming a VM systemd calls active really has a
Firecracker behind it. The reply carries the guest's own state, which is where
`Paused` comes from. The probe runs only for VMs whose status depends on it (an
active unit with no sleeping marker), and **a socket that does not answer yields
`Unknown`, never `Stopped`** — a stopped, sleeping and mid-launch VM look identical
from the socket, so the cross-check must not become a claim the host cannot support.

**NOT BUILT: driving a running guest through its API across a restart.** Boat can
say *that* a Firecracker is alive and report its pid, but does not yet hold or
re-establish an API conversation with one (a pause- or snapshot-across-upgrade would
need it). **§5's hard gate — a binary swap leaves every guest running — is
satisfied**, because that is about what Boat does *not* do on startup.

The journal replay is live: an operation is stamped in flight when claimed, so a
crash between claim and terminal record leaves a record the next incarnation finds
(§11.5). **Fail closed on an empty fence store:** a Boat that lost bbolt refuses to
boot any UUID until it re-registers and re-pulls desired state and epochs (§11.1) —
enforced on both boot paths, the part of the fence that works.

### 3.4 Re-adoption and the host scan

The enumerators already exist, inverted, in
[`reset-server.py`](../scripts/reset-server.py). Boat's Go port uses the same
enumeration to **ingest**: each artifact is read, the Firecracker socket is
cross-checked against the unit's state, and the per-VM observed document is
reconstructed by the same `Observe` steady-state uses — so adoption and the sweep
can never report one host two ways. **Every command in the scan is a listing, a stat
or a boolean gate** — no create, remove, start or stop anywhere — which makes "a
scan never changes the host it reads" a property, not a convention.

That cross-check is a real liveness probe: it asks the socket rather than whether
the socket exists (a segfaulted Firecracker leaves its inode behind, and a `stat` is
happy with it). The distinction it holds: a probe that **cannot be made** fails the
whole scan; a probe that **was made and got no answer** is data.

**Ambiguous or partially torn-down artifacts — a crash mid-terminate — go to
`quarantine`, never into the observed set**, because a half-deleted VM ingested as
truth is one Atlas will try to start. The line is between **ambiguity and
untidiness**: a stopped VM whose namespace outlived it is untidy (identity not in
doubt, safe to boot) and adopted; an active unit with no namespace is ambiguous and
quarantined with the evidence. **A partial scan fails whole** — the daemon exits
rather than serve what it could not confirm, since "holds nothing" and "could not
read" are otherwise the same document. The quarantine set is its own export array,
keyed by whatever identifier the host retained.

### 3.5 The native-Go rewrite is gated by differential testing

Full native Go, but not blind: the Python host surface (`scripts/lib/atlas/`, the
`networkd/` modules, the host verbs, their unit tests) is the **conformance
oracle**. Each Go module is validated against a **golden corpus** captured from its
Python counterpart (byte-identical rendered commands), the
[`_run.py`](../scripts/lib/atlas/_run.py) quoting model being the spec the Go builder
must match; and a **differential phase runs both side by side on a real host**
before an operation cuts over to native-only, retiring the big-bang risk (LVM CoW
ordering, EUI-64, wg peer-table rendering) one module at a time. **Port order is
restart-sensitivity and hot path first.**

**Built: one golden corpus, at the layer that most needed it** — the quoting model
generated from CPython's own `shlex` (`internal/run/shlex_conformance_test.go`), plus
per-verb rendered-command assertions that need no host. **The real-host differential
phase is NOT BUILT**: verbs ported so far were cut over on unit-level equivalence
plus manual exercise. The gate is owed before WO-3b's network apply, where a
rendering difference stops being a wrong command and becomes a VM off the network —
skipping it re-creates exactly the risk it exists to retire.

### 3.6 CLI and daemon

The `boat` CLI talks to the resident daemon over `/run/boat/boat.sock` (0660)
speaking the same HTTP/JSON API (§2.1) — also the operator's break-glass path when
Atlas is unreachable. Built: `boat vm start|stop|ls|show <uuid>`, `boat host facts`,
`boat version`. *NOT BUILT:* `boat export` and `boat adopt` — the export is reachable
over the socket with `curl` and adoption runs at startup, so neither is missing
capability, only spelling. The socket sits under `/run/boat/` (not `/run/`) so a
non-root daemon can own it via `RuntimeDirectory=boat` rather than running as root or
taking `CAP_DAC_OVERRIDE` — the blast radius §12 exists to remove.

### 3.7 The unit set

`boat daemon` supervises the sibling units — it owns their start and restart and
surfaces each one's liveness in `GET /v1/host` — but **never reaches into networkd's
gossip state**: supervision is lifecycle only, and §0's ANCP boundary stays intact.
**BUILT (WO-3a)** over the units as they exist today: `GET /v1/units/{name}` reports
liveness, `POST /v1/units/{name}` acts, and `GET /v1/host` and the export carry the
set (§2.5). Three decisions are worth the chapter:

- **The verb set is `start` and `restart`, no `stop`.** "Be running" and "be running
  afresh" are the only states a control plane wants a sibling unit in, and `restart`
  covers every legitimate stop-then-start. A `stop` would be a verb with teeth and no
  driver — its only power is to strand every sleeping VM by stopping the counter
  watcher. `reset-failed` is out for the adjacent reason: a rate limit the
  rate-limited thing can reset is not one. Either need is SSH break-glass (§12).
- **The supervised set is a closed list of literal names**, enforced in Go and pinned
  in `sudoers.d/boat` one grant per unit per action, no wildcard: `atlas-pool`,
  `atlas-networkd`, `atlas-wake-trap`, `atlas-mgmt-firewall`. A UUID cannot be spelled
  with four literals, so `firecracker-vm@` instances, `sshd` and `boat.service` are
  unreachable by construction. `gateway.service` (guest-plane, §7.1) and the pre-ANCP
  `host-mesh.service` are excluded on purpose.
- **A unit systemd reports `not-found` is omitted, not reported inactive**, since
  `boat_mirror._units_down` reads any non-`active` unit as down and a host not running
  a unit would otherwise flag permanently degraded. The liveness read is `systemctl
  show` — a system-bus property read needing no grant; only `start`/`restart` are
  privileged.

The unit template follows
[`atlas-networkd.service`](../scripts/systemd/atlas-networkd.service) but
**deliberately not `Type=notify` or `WatchdogSec`**: the daemon does not call
`sd_notify`, so a `notify` unit would never reach `active`, and an unpatted
`WatchdogSec` is a timed kill, not a liveness guarantee. `Type=exec` is the honest
maximum until the daemon learns `sd_notify`.

## §4. Bootstrap, registration, re-adoption

> **PARTLY BUILT — WO-1b.** Re-adoption (§3.4) is live, and `boat bootstrap` is the
> host-prep Task: `Server.bootstrap()` runs `boat bootstrap --firecracker-version …
> --architecture …` (the flags `bootstrap-server.py` took, printing the same
> `ATLAS_RESULT=` line, the `.py` kept as the differential's oracle) and also deploys
> Boat itself — ships the binary, `sudoers.d/boat` and both units from the
> operator-staged checkout named by `atlas_boat_distribution`, creates the `boat`
> user, validates the allow-list with `visudo -cf` before installing, and starts
> `boat.service` once the host is VM-ready. **Still NOT built:** `POST /v1/bootstrap`
> and the registration handshake — the daemon's address and token still come from
> site config (§2.3, §12), nothing writes `/etc/boat/token`, so a freshly
> bootstrapped daemon serves its local socket only.

**`boat bootstrap` brings a bare host to Active by itself** — thin pool, network
scaffold, firecracker/jailer install, sudoers, unit installation, then
self-registration — replacing [`bootstrap-server.py`](../scripts/bootstrap-server.py)
as an SSH-driven Task, idempotent and safe to re-run on an Active server.

- **Landing the binary reuses the existing SSH path** (`Server.bootstrap()` /
  [`install.sh`](../scripts/install.sh) place the signed binary; no new channel).
  SSH stays, for bootstrap and break-glass (§12).
- **Registration mirrors the armed auto-revert handshake** of
  [`central_link.py`](../atlas/atlas/api/central_link.py) ([21-tunnel.md](./21-tunnel.md)):
  Boat generates its token/keypair and registers, and a failed handoff reverts
  rather than bricking the host.
- **Re-adoption**: on a host that already has VM state, Boat runs the §3.4 scan and
  adopts idempotently, re-attaching to live firecracker.

**One constraint comes from shipped ANCP and is not Boat's to relax.** The ANCP
bootstrap trust artifacts (spec/31 §8, §19.4–5 — signing keys, `seed.json`, the
introduction certificate) are signed with the **operator provision key Atlas holds
and Boat must never hold** (a Boat-generated certificate is self-signed and rejected
by every host). They are Atlas-written, ride the binary's channel, and must exist
before `boat networkd` starts. *Which side of the handshake writes them is open —
§16.*

## §5. Self-update

> **PARTLY BUILT — WO-5b.** The Boat-side self-update is built (§15):
> signed-release verification, atomic install keeping N-1, the seven-step Apply with
> rollback, and `POST /v1/update` spawning a detached `boat update-apply` in its own
> systemd scope. **NOT BUILT on the Atlas side:** no desired `Server.boat_version`
> field and no staggered rollout driver, so a host is still updated by an operator.
> §3.3's re-attach gate is satisfied. **Not yet exercised under a live guest, and
> nothing ships the allow-list**, so the sudoers-change half of the drill is
> uncovered. The **running** version already comes back in every export and lands on
> `Server.observed_boat_version`, so drift is visible the moment a desired version
> exists to compare against.

Boat updates itself; the required shape, all seven steps: (1) **desired version
lives in Atlas** (`Server.boat_version`) and **Atlas pushes it** — no host-side poll,
because only Atlas knows which hosts are Unknown or mid-operation and can stagger
correctly; (2) **verify signature and checksum first**, then **atomically rename**
over `/usr/local/bin/boat`, keeping N-1; (3) **quiesce** — refuse new operations,
checkpoint in-flight ones into the journal (§3.2); (4) **restart the units in order
and re-attach to running firecracker rather than restart it** (§3.3), with **sleeping
VMs staying asleep** across the swap ([32-sleepy-vms.md](./32-sleepy-vms.md)); (5)
**health-check** (a `GET /v1/export` plus unit liveness) and **roll back to N-1** on
failure; (6) **Atlas staggers the fleet**, canary then waves, since a simultaneous
fleet-wide auto-update is the one failure mode that can brick every host at once; (7)
it extends [23-supply-chain.md](./23-supply-chain.md) — signed releases,
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

**Boat's network apply is built (WO-3b); the runtime cutover is not.** The
park/wake reflex has been live longest: Boat installs the proxy-NDP entry, the
`/128` route out the shared `atlas-park0` dummy, and the counting SYN rule in the
forward chain, and its resident trap polls those named counters once a second and
asks the reconciler for a pass — it exists because sleep needs it. WO-3b then built
the rest of the table — per-VM netns, veth, tap, NAT44, proxy-NDP and nft isolation
for a *running* VM, `local-ownership.json`, reserved-IP 1:1 NAT, customer-gateway
forwarding — in Go, held byte-identical to the Python on a real host (the
differential caught a duplicate nft rule the Python added on every restart), and
Atlas routes the reserved-IP attach through Boat. What remains (WO-6) is repointing
the running-VM units off the `firecracker-vm@` unit's Python hooks onto `boat <sub>`. Boat now supervises the `networkd` unit's
lifecycle and reports its liveness (§3.7), and reads none of its state: the mesh,
its membership and its wg peer table stay ANCP's, exactly as §0 requires.

### 6.2 Public IPv6 allocation stays in Atlas for v1

`allocate_ipv6(server)` is **cluster-aware**: after a keep-address migration
([24-vm-migration.md](./24-vm-migration.md)) a live VM on host B can own an address
out of host A's range, so a "birth-host allocates" model is **unsound**
(reuse-after-terminate collides with a permanent vendor forward). **Accepted v1
constraint: allocation stays in Atlas, cluster-aware**; Boat only *applies* the
address it is handed and reports the in-use set. Pushing allocation down is gated on
§11.4's union-reconciliation law and forward-lease and on the fence being enforced
(§15, WO-6). This is the **public** plane; the private plane is already
decentralized by ANCP.

## §7. The guest-service plane stays in Atlas

### 7.1 Atlas keeps the direct guest-SSH plane

Guest-service configuration — proxy maps, bench and site deploy, in-guest gateway
config — is *guest-plane* work, not bare-metal host state, and stays in Atlas over
`connection_for_guest` ([04-tasks.md](./04-tasks.md)): SSH to the guest's public
`/128`. **Boat does not mediate guest traffic and stays workload-agnostic** —
routing guest-exec through it would make it a generic guest-command proxy and
re-entangle service semantics with the daemon that must not know them.

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
- **Service-semantic fields are not named in Boat's schema:** `routing_base_url`
  ([18-bench-self-routing.md](./18-bench-self-routing.md)) arrives as one anonymous
  `extra_env` entry Boat cannot tell from any other, whereas `host_signature` and
  reserved-IP NAT are guest-agnostic host mechanics Boat owns outright.

**Built, on the wire and in the mechanics.** The contract's `GuestIdentity` is
exactly that shape and rides on the rebuild request — the one verb whose input is
neither desired state nor host fact. Boat copies every field as bytes (nothing parses
a key, validates an address, or knows what `/etc/anything` is for) into a freshly
laid-down rootfs, with the disk's **SSH host keys preserved** — rotating them every
rebuild would break every client's `known_hosts`; a keyless disk is the one exception
(a self-heal, not a rotation). `BoatClient.rebuild_virtual_machine` fills the blob
from the same `_rebuild_variables` the SSH path uses, so either transport lays down
the same guest; a Task variable the mapping does not name **raises** rather than being
silently dropped. **No provision verb exists yet**, so the `ProvisionSpec` lives only
as the rebuild half of itself — provisioning is still an SSH Task (§4).

### 7.3 Accepted constraint — dark and private-only VMs

`connection_for_guest` requires a public `/128`, which dark VMs
(`public_networking = 0`) lack. **For v1 the service layer supports public VMs
only**; private-only *service* VMs are out of scope. Tenant private-only VMs that
run no Atlas-managed service are unaffected. The future escape hatch is a single
bounded `boat.dial_guest(uuid, port) → socket` primitive — an L4 forward into the
VM's netns, with Boat never receiving a command, a shell, or the guest credential
— and Atlas's SSH layered over it. It is not specified here and not built.

## §8. Cross-host operations are sagas

> **BUILT, NOT DOGFOODED — WO-4.** Each mutating phase below is a
> `POST /v1/vms/{uuid}/migrate/{phase}` on the relevant Boat:
> [`migration.py`](../atlas/atlas/core/migration.py)'s `_run_phase_task` routes the
> saga through `run_boat_migration_phase` (only the cutover *boot* and
> `collapse_forward`'s re-provision stay on `run_task` — they are lifecycle ops,
> not saga phases), and **Repoint bumps `boot_epoch`** (`migration.py:958`), so the
> fence is armed (§16.0). What is not closed: **no live two-host migration has been
> dogfooded** — a real host surfaced an `nbd-client -N ''` defect (fails qemu-nbd
> negotiation, ungrantable in sudoers) — so no host should carry production VMs
> through a migration until WO-4 is exercised end to end.

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
desired change means no action), serves host-local reflexes (wake traps, reboot
recovery from `network.env`, pool rebind), keeps ANCP gossiping, keeps the local
`boat` CLI working as break-glass, and buffers observed reports for reconnect.

**Atlas when a Boat is unreachable — the host is `Unknown`, not dead**, an
easy-to-get-wrong rule: Atlas must not assume the VMs died (an unreachable daemon is
evidence about the daemon, not firecracker); placement excludes an Unknown host from
new arrivals **but does not evict** (evicting turns a management-plane blip into a
fleet-wide outage, and — without the fence — two live copies the moment the host
returns); and the stale mirror **freezes, never nulled** (a nulled mirror reads as
"this host has no VMs", which placement would act on).

**Reconnect** is the §2.5 symmetry: Atlas re-`PUT`s desired state and pulls
`GET /v1/export` to rebuild the mirror in one transaction, Boat replays its buffered
transitions, `/watch` resumes; any in-flight operation is resumed by Boat's state
machine, `op_id` dedupe making that exactly-once (§2.7).

**Built: the autonomy, the freeze, and both halves of the resync.** Boat keeps
running with no control plane; `boat_mirror` freezes the mirror and flags the host
`Unknown` and writes nothing else — no nulled capacity total, no VM row touched,
nothing marked stopped, no eviction. Atlas re-`PUT`s intent before every verb and
pulls the export with `sync_mirror`. **Placement reads `mirror_status`:** the
candidate set is `placement.placement_candidates()` (Active *and* not `Unknown`),
feeding every placement path; the two arrivals that **pin their own host** — a clone
(host-local thin snapshot) and a migration (operator-named target) — apply the same
gate through `placement.assert_visible` and refuse instead. When every Active host
reads `Unknown`, placement raises **`HostNotVisibleError`, not `NoCapacityError`** —
the region is blind, not full, the only signal that stops Central retrying against a
region with room. It is an *arrivals* gate and nothing more: no eviction, no write
to `status`, capacity accounting unchanged, a resize on a VM already there still
allowed. Only the literal `Unknown` excludes — an empty `mirror_status` (never
mirrored) does not, since "I have not looked" is not "I have lost sight of it" — and
the next successful export writes `Fresh` with no operator (§2.5). *NOT BUILT:*
buffering observed transitions across the blip, and the `/watch` resume.

**Split-brain is prevented by the fence epoch (§11.1), not by phase ordering.**
Ordering is a property of a saga that completes; the fence is a property that
holds when one does not.

## §10. Observability and audit

The [22-observability.md](./22-observability.md) model survives intact, merely
re-sourced. The Frappe `Task` row stays the live-progress carrier and Boat's `ops`
journal is crash-recovery truth at `GET /v1/ops/{op_id}` (built). A Boat-run verb
writes the same `stdout`/`stderr`/`exit_code`/`status` through the same
`_mark_running`/`_finalize`, so nothing downstream tells the transports apart.
**The live-progress half is NOT BUILT** (§2.7): the Task row goes `Pending → Running
→ terminal` with the whole trace at the end, so `live_output`/`progress_line` stay
empty on the Boat path. The audit surface stays the Task row plus the *Running
Operations* view, now also reflecting **Boat-reported observed transitions** (a
wake-trap flip, a crash-restart) — truthful, and up to five minutes old via the
export sweep (§2.6). **Audit parity is a hard requirement:** every operation writes
an append-only record equivalent to today's immutable `Task` / `SSH Command Log`
rows.

## §11. Correctness invariants

**These six were the gate:** all six were written and reviewed here before any Boat
code (cheap to build in, expensive to retrofit), each stated as a rule with its
failure mode — a rule whose reason is missing gets deleted by the next person who
finds it inconvenient. **Writing them first bought less than it looks.** Where each
stands today:

| Invariant | Enforced? |
|---|---|
| 11.1 fence epoch | **Yes**, pending a live migration drill. No fence means boot nothing; a superseded epoch is now refused — the repoint bump (`migration.py:958`) and the desired `server` field both landed (§16.0) |
| 11.2 CAS on contended reservations | **Yes, as a mechanism; no contended caller yet.** `PUT /v1/vms/{uuid}` takes `If-Match: <observed-epoch>` and answers 409 `stale-observation`. Atlas sends no such header yet |
| 11.3 `desired_power` vs `observed_status` | **Yes**, including the precedence rule, though not in the shape the prose below describes — see the rule |
| 11.4 forward lease, union reconciliation | **No.** Boat allocates no address; the `held/ipv6` bucket does not exist |
| 11.5 write-ahead journalling | **Partly.** Every operation is stamped in flight in its claim's own transaction, and rebuild records its source ahead of the destructive step. No verb re-enters at a checkpoint |
| 11.6 bbolt isolation | **Partly.** The export's short-`View`-then-release is real; the lock-free read and the `progress/` bucket are not |

### 11.1 The fence epoch

**Rule.** A per-UUID monotonic `boot_epoch`. **Atlas is its sole issuer.** It is
stored on the VM row and mirrored into each Boat's `fence` bucket on every
desired `PUT`. **Boat refuses to boot a UUID unless its local epoch ≥ the on-disk
unit's epoch AND desired `server == self`.** An **empty fence store means boot
nothing**. The epoch bumps at exactly one point: migration Repoint (§8).

> **§16.0 is the full account.** Enforced: Atlas is the sole issuer (the mirror
> never writes a host-reported epoch back), the epoch may not regress, and a host
> holding no epoch for a UUID boots it on neither path. The epoch *comparison* and
> `server == self` now bite too — the repoint bump (`migration.py:958`) and the
> desired `server` field both landed (§16.0); what remains is a live migration
> drill (WO-4).
>
> **Atlas also reports the disagreement.** The export's `fence_epochs` map is read
> against the epoch Atlas issued for each VM it placed there, and every difference
> is a `fence` drift row on the epoch's `Host State Snapshot`. Only VMs Atlas has
> fenced are compared; an export with no `fence_epochs` key is silence, the empty
> map `{}` the claim. The **absent** case has teeth — the host will boot nothing —
> so it puts `mirror_status` at `Unknown` and takes the host out of arrivals (§2.5,
> §9).

**Failure mode.** Without it, a partitioned migration produces two live copies of
one VM — the reconnecting source boots the VM whose disk the target owns, two
writers on one disk and two hosts answering NDP for one `/128` (ANCP blackholes the
conflict, spec/31 §18, but only after the corruption). The same failure needs no
migration: a Boat that lost bbolt and boots everything on disk is the single most
dangerous state the system can reach, which "empty fence store means boot nothing"
forbids. The epoch also makes force-reprovision safe by construction.

### 11.2 CAS on contended reservations

**Rule.** Placement, the capacity gate, migration-target choice and reserved-IP
attach all go through `PUT` with `If-Match: <observed-epoch>`. **Boat returns
`409` if its state moved since the epoch the mirror was built from.** `server`,
`ipv6_address` and the reserved bindings stay frozen except through those CAS
verbs.

> **BUILT as a mechanism (WO-3a), with no contended caller yet.**
> `PUT /v1/vms/{uuid}` accepts `If-Match: <observed-epoch>` and answers `409
> reason: stale-observation`; omitting the header is ungated (the common case,
> since Atlas re-asserts intent on every reconnect and a precondition there would
> fail resync exactly when the mirror is furthest behind). The PUT's other `409`
> carries `reason: fence-regression` — the two share a code but one must never be
> retried and the other must be retried against a fresh export.
>
> **The token is whole-host but the COMPARISON is per-resource — a correction to
> the rule above.** A whole-host comparison is unusable: every observation bumps the
> epoch, so on a forty-VM host the offered epoch is hundreds of bumps stale by
> construction and *every* CAS would fail (a precondition that always fails is one
> somebody removes, taking the real protection with it). So each observed record is
> stamped with the epoch it was written at, and `If-Match` asks only whether THAT
> record moved; an epoch *newer* than any this host issued is also refused. A
> HOST-WIDE contended resource (last slot of RAM, thin-pool bytes, the reserved-IP
> slot) is **not** covered by widening this back to the host — it gets its own
> stamped record and the same primitive, one more caller.
>
> **Reserved-IP attach is still NOT BUILT (WO-3b)**, so §1's "mirror disposable
> because no contended decision is taken from it" still holds by the absence of a
> caller — but the mechanism now exists and is proven, so the next caller (WO-4's
> migration-target choice) inherits it rather than inventing it.

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

> **Built. The one prose correction:** verbs *do* touch the host — a verb runs the
> mechanics itself, but **inside the turn it takes from that VM's actor** (the same
> turn a reconcile pass holds), so the two serialize. Every handler reaches the host
> through one claim-take-turn-run-journal function, making the property structural.
> Atlas separately `PUT`s desired state before every verb, so both halves of the
> rule are real, on opposite sides of the wire.
>
> The precedence rule is a branch taken **before** the reason for the pass is read:
> the Stopped half of the planner is never handed the trigger, so no future reason
> can turn a Stopped desire into a start, and an unauthenticated SYN reaches a boot
> only through it. Sleeping is a *resting state of a Running desire*, so the sweep
> leaves a parked VM parked and only a pass asked for by name resumes it. The same
> precedence is enforced a second time at the API: explicit `wake` and `resume` are
> refused while the stored desire is Stopped (a host holding no desired record is
> not refused — nothing to outrank). `wake` is additionally fenced (waking is
> booting); `resume` and `stop` are not, since both act on a resident guest.

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

**Failure mode.** Ownership of a public `/128` is cluster-wide (§6.2), so any
single-sided view double-allocates: a host still forwarding a kept address is
invisible to an Atlas-only view; an allocated-but-not-yet-applied address looks free
to a host-only view; and without the lease recorded *before* teardown, a crash
between "stop forwarding" and "release" strands it in neither book. The result is
always two VMs on one public address — which is why §6.2 gates allocation-in-Boat
on this law.

### 11.5 Write-ahead journaling of the decision

**Rule.** The journal records the **non-idempotent decision** — which address,
which reserved IP, which host slot — **before** the host side effect, so a crash
then a retry replays deterministically. Allocation, LV create and operation
completion commit in one bbolt transaction. Reserved-IP attach is CAS on the
host's reserved-IP slot.

**Built, in one file.** `internal/store` holds the decisions bucket and the
incarnation counter — no second bbolt file, no `journal.db` (`internal/journal`
reads it) — because the in-flight stamp has to be atomic with the claim, and two
files would open the crash window the invariant exists to close. `ClaimOperation`
writes the incarnation inside the claim's own transaction, so `Unfinished`
(non-terminal *and* claimed by an earlier incarnation) distinguishes a crashed
operation from a merely slow one. An interrupted operation is closed as **Failure**
naming the restart after the VM is driven forward — Failure because Boat cannot know
the verb finished, and the safe direction since every verb is idempotent.

**Of the nine verbs, exactly one makes a choice its own retry could not repeat:**
`rebuild` picks the volume the new root is snapshotted from and then `lvremove`s the
old root, so it records that source before it acts; the other eight are idempotent by
construction. **`sleep` had to be MADE idempotent** — replaying it on an
already-asleep VM `rm -rf`'d the snapshot it was about to re-take and reported
`Success` (the VM cold-booted on its next wake); it now branches on the sleeping
marker first and re-asserts what a sleep leaves behind without touching the snapshot,
because a verb whose replay is the designed recovery has to converge, not refuse. The
decisions this invariant was really written for (which address, host slot, LV) belong
to **provision and migration**, which are NOT BUILT — inventing decisions to justify
the machinery would have been worse than the gap.

> **NOT BUILT: re-entry at a checkpoint.** `Decisions(id)` is written, readable, and
> consumed by nothing — and an Atlas retry mints a **new Task name**, so it cannot
> reach the previous operation's decisions at all. Until a verb re-enters,
> `rebuild-source` is forensics with correct write-ahead ordering (the plumbing is
> proven for WO-4/WO-6, the checkpoint semantics are not). Reserved-IP CAS is WO-3b:
> the primitive is built (§11.2), the reserved-IP slot it compares against is not.

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

> **Built: the third clause, which bites first.** `Snapshot` materializes the whole
> export inside one short read transaction and returns it as a value before a byte is
> written, so serialising to a slow client can never hold the file's page reclamation
> — a slow reader cannot make a healthy host look dead. The observed epoch is read in
> that same transaction, so a CAS token always belongs to state somebody saw; SSE
> readers touch the store only to read the epoch, the stream itself served from an
> in-memory hub. **Not built: the other two.** Fence reads take an ordinary bbolt
> `View`, so the property holds by bbolt's MVCC rather than anything here (true by
> accident is a rule the next refactor breaks); and there is **no `progress/` bucket
> and no writer**, untested rather than violated since no long operation has been
> ported — the first (image sync, hydration, a large RAM snapshot) must land with its
> bucket, not after.

**Failure mode.** bbolt has a single writer. A 900-second image sync holding the
write lock stalls the heartbeat, so Atlas marks a healthy host `Unknown` and
placement stops sending it VMs; an export streamed *under* the lock is worse, the
stall scaling with the client's read speed. **A busy host must never be
mis-declared partitioned** — every §9 behaviour keys off Unknown, so a false Unknown
is a false everything.

## §12. Security — no worse than today is the bar

- **Non-root daemon.** Boat runs as a service user under a **pinned NOPASSWD sudoers
  allow-list** (`sudoers.d/boat`, modelled on
  [`sudoers.d/atlas-tunnel`](../scripts/sudoers.d/atlas-tunnel)): enumerated `wg`,
  `nft -f …`, specific `systemctl` and `firecracker` invocations — the individual
  calls need root, the daemon does not. This is the biggest blast-radius reduction
  the split buys (today Atlas SSHes as root). **Built**: `User=boat`, every root
  command enumerated with its arguments (no wildcard shell, no `ALL`, no bare
  `systemctl`), the unit-scoped grants naming `firecracker-vm@*.service` so they
  reach neither `sshd` nor `boat.service`. Two properties are easy to erode and worth
  keeping: **each verb adds its own lines rather than widening an existing one** (the
  Firecracker API grants are split per method — power-off, guest-state, snapshot — so
  a read-only probe cannot acquire the power to stop VMs); and **every grant that
  could not be pinned tightly is named in the file as a residual risk** (the
  spool-path `install(1)`, the park rules' address/uplink wildcards read from a VM's
  own `network.env`). The unit carries **no sandboxing beyond `User=`/`Group=`**:
  `NoNewPrivileges=` breaks setuid `sudo`, and `ProtectSystem=`/`PrivateTmp=` would
  confine the root children `sudo` spawns — the privilege boundary is the allow-list.
- **Verb allow-list enforced at the API boundary.** **There is no arbitrary-command
  endpoint, ever** — the generated router serves only what `api/openapi.yaml` describes
  and 404s anything else, and the runner takes parameterized templates, not assembled
  strings. *Porting `scripts_catalog.allowed_scripts()` into the API layer is NOT BUILT
  and unneeded while the contract is the allow-list; it becomes needed at WO-6's generic
  `boat <verb>` surface.* The ad-hoc surface stays the SSH Console
  ([04-tasks.md](./04-tasks.md)), operator-only and fully logged.
- **Short-lived scoped tokens** (or mTLS), minted per host by Atlas, with **rotation
  under partition**: Boat serves the last valid token until a hard expiry and Atlas
  re-mints on reconnect, so there is never a trust-a-stale-token-forever window nor a
  partition that locks an operator out of their host. **NOT BUILT — WO-1b:** today the
  token is a **static, non-expiring** per-host value (Boat reads a file the operator
  placed, Atlas reads site config `atlas_boat_tokens`). Never logged, never in an error
  message, and the tunnel is still the transport boundary — but a leaked token is good
  until changed by hand.
- **Audit parity** — §10. Append-only operation records equivalent to today's
  immutable `Task` / `SSH Command Log` rows.
- **Supply chain is a NEW threat, created by self-update:** a self-updating binary
  fetches and executes code on its own, where SSH scripts were fetched per Task. Signed
  releases, checksum-pinned install, reproducible builds and provenance are therefore
  mandatory (extending [23-supply-chain.md](./23-supply-chain.md), the signature check
  first in §5), and every accepted Go dependency is argued in review with the standard
  library as the default.
- **SSH is retained as break-glass.** Inbound key SSH with the fixed verb catalog stays
  as the out-of-band channel to restart or replace a wedged Boat. **The verb-port
  cutover does not delete it** until a proven equivalent recovery channel exists (§15,
  WO-6).

## §13. What is explicitly not Boat's

- **The ANCP gossip plane.** Boat supervises the `networkd` unit and writes
  `/etc/atlas-networkd/local-ownership.json` — *both built: unit supervision
  (WO-3a/WO-5) and the `local-ownership.json` write (WO-3b); repointing the
  running-VM `vm-network-up`/`vm-network-down` apply onto `boat <sub>` is WO-6.*
  What holds already is the
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
  `is_proxy`, `is_gateway`, `build_mode`, `pilot_credential_id` and `terminate()`'s
  service fan-out into an `atlas/services/` module with a `Service` / `Service
  Binding` registry — runs in parallel with no Boat dependency (§15, Track S). It
  lifts [30-core-service-boundary.md](./30-core-service-boundary.md)'s seam **in-app**
  (that chapter's federated answer is superseded, its boundary is not); services
  depend on core, never core on services.

**On transport.** [04-tasks.md § Why SSH, not HTTP](./04-tasks.md) found the two
transports statistically indistinguishable and chose not to switch for latency;
**that conclusion stands** — Boat is not a latency change, the transport moves
because **the state** moves. The Task model (a typed verb, `--kebab-case` flags in,
one typed result out, one audited row) survives unchanged (§2.4, §2.7); only delivery
changes. The ten `scripts_catalog.BOAT_VERBS` are delivered as `boat <verb>` on every
host; the rest still run `atlas <verb>` on the venv, the `.py` files staying as the
differential's oracle.

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

Work orders, one line each — enough to place any commit. They were gated behind
`Server.boat_enabled` (and, from WO-2, per-VM `observed_authority`) and rolled
back by clearing a flag; `boat_enabled` is now removed, so the rollback for
anything below is a revert rather than a switch.

| WO | Status | Ships |
|---|---|---|
| **WO-0** | **SHIPPED** | Walking skeleton: a `boat` binary that starts, serves the API on the tunnel and the unix socket, persists to bbolt, and starts/stops one real VM driven from Atlas through `BoatClient`. |
| **WO-1** | **SHIPPED** | Observed state: adoption scan, firecracker re-attach, `GET /v1/export`, `/watch` SSE, the `Host State Snapshot` mirror, and the fence store — advisory only, the DB still authoritative. `internal/fcattach` now has callers in both the scan and `Observe` (§3.3), so **WO-5b's hard gate is satisfied**: a binary swap leaves every guest running, and Boat can confirm one is alive rather than inferring it from systemd. |
| **WO-1b** | **built** | `boat bootstrap`: a bare host brought to VM-ready by the binary itself. Dogfooded from scratch on atlas-host-1 and idempotently over a pre-existing pool on host-2 — Firecracker, jailer, thin pool, nft scaffold, `atlas-park0`. The armed auto-revert registration handshake (§4) is NOT built; a host is still registered by the controller. |
| **WO-2** | **IN FLIGHT** | Full lifecycle and reflexes: every VM verb through Boat, the per-VM reconciler, the journal, and the wake trap resident in Boat; per-VM authority flips to Boat. Landed: all nine lifecycle verbs end to end, the per-VM reconciler and single actor, the resident wake trap, the guest-identity blob on the wire, and the five-minute mirror sweep. Outstanding: `observed_authority` is never read so authority has not flipped (§1), no verb re-enters at a checkpoint (§11.5), and there is no `/watch` consumer (§2.6). |
| **WO-3a** | **built** | Sibling-unit supervision — `GET|POST /v1/units/{name}`, unit liveness in `GET /v1/host` and in the export (§3.7) — and the CAS primitive of §11.2: `If-Match` on the desired-state PUT, compared per-resource, answering `409 stale-observation`. |
| **WO-3b** | **built** | Host-local network apply: `vm-network-up`/`down`, the private-plane isolation, the public-ingress firewall, WireGuard tunnels, `local-ownership.json` and the reserved-IP 1:1 NAT — each held byte-identical to the Python on a real host, which is how the differential found a duplicate nft rule the Python added on every restart. Atlas routes the reserved-IP attach through Boat. Re-pointing the supervised units at `boat <sub>` is still WO-5/WO-6. |
| **WO-4** | **code-complete, NOT dogfooded** | Cross-host sagas: `internal/migration` holds thirteen host-side phase functions behind `POST /v1/vms/{uuid}/migrate/{phase}`, `boot_epoch` bumps at repoint, and the `server == self` placement gate is wired end to end. Deploying it to a host found a real defect the tests could not: `nbd-client -N ''` both fails qemu-nbd negotiation AND is ungrantable in sudoers. `migration.py` routes each saga phase over Boat via `run_boat_migration_phase` (only the cutover boot + collapse re-provision stay on `run_task`); what remains is that **no live two-host migration has been dogfooded**, so §16.0 is not fully closed. |
| **WO-5** | **built and dogfooded** | `boat networkd`: the ANCP core on memberlist, same binary, own unit. Proven on two hosts — host-2 seed-joined host-1, both learned the other by TOFU and rendered it as a wg peer through gossip → syncconf. |
| **WO-5b** | **built** | Auto-update (§5): signed-release verification, atomic install keeping N-1, the seven-step Apply with rollback, and `POST /v1/update` spawning a detached `boat update-apply` in its own systemd scope so the daemon restart cannot SIGTERM it mid-swap. Not exercised under a live guest, and **nothing ships the allow-list**, so the half of its drill that covers a sudoers change is still uncovered. |
| **WO-6** | **IN FLIGHT** | Verb-port completion and cutover. Landed: `internal/{snapshot,backup,image,thinpool,hostkeys,cert,mgmtfirewall,reset}` and `snapshot.RestoreVM`, all reachable as `boat <verb>` taking the Python's flags and printing its `ATLAS_RESULT=` line; Atlas routes the ten host verbs of `scripts_catalog.BOAT_VERBS` at them; `Server.boat_enabled` deleted. Outstanding: the venv and durable package are NOT retired — `firewall-apply`, the tunnel verbs, `poll-vm-traffic`, `probe-woken-vms` and `export-cleanup-source` have no Boat verb, and the `.py` files of the ported ones stay as the differential's oracle. Public-IPv6 push-down still gated on §11.4. SSH break-glass and `connection_for_guest` are **not** deleted. |
| **Track S** | not started | Services de-fusion (§13, last bullet). Parallel, no Boat dependency; one service moved at a time, green each commit. |

Verification is spec'd with the work: the §11 invariants reviewed before any Boat
code; a per-operation differential on a live host before cutover (§3.5); partition
drills at WO-2/WO-4 (kill Atlas and Boat keeps VMs running and self-recovers; kill a
Boat and Atlas marks it Unknown without evicting; a partitioned-migration drill proves
the fence stops two live copies); an update drill at WO-5b proving a corrupted binary
and a failing health check both roll back; and `run_all_smoke`
([README § Testing](./README.md)) green at every work order.

**What has actually been verified:** the invariant review happened; Boat's own suite
is green under `-race`, faking every host seam; and the Atlas side has ~1600 lines of
unit tests (`test_boat_client`, `test_boat_lifecycle`, `test_boat_mirror`). **What
has not:** **no differential harness** (§3.5), **no partition drill**, and — most
consequentially — **no e2e use case**, so `run_all_smoke` exercises no Boat path and
the transport is covered against a real droplet only by hand. A `boat_lifecycle` use
case ([`atlas/tests/e2e/use_cases/`](../atlas/tests/e2e/use_cases)) is owed — the
check that would catch this chapter's recurring defect: a component that exists,
passes unit tests, and is wired to nothing.

## §16. Open

Everything else in this chapter is **decided**, which is a weaker claim than
built — the `NOT BUILT` markers throughout say which decided things have no code
behind them. These are the questions that have no answer yet.

0. **The fence epoch now refuses a stale epoch, not just an empty store.** For a
   while this was the largest gap between this chapter and the code and §11.1
   overstated what was built; both prerequisites have since landed, so the gate now
   does what §11.1 says. What is left is dogfooding, not code.

   What works: a host holding no epoch for a UUID boots nothing. That is the
   rule that saves a Boat which lost its bbolt file, and it is enforced.

   What used to not: the epoch *comparison* was a tautology. `PUT /vms/{uuid}`
   writes the fence and the desired record from one document, so the held epoch
   and the desired epoch were equal by construction and a stale epoch could not be
   detected. Two things had to land to break that tautology, and **both now have**:

   - **Atlas bumps the epoch at a migration's repoint** (`migration.py:958`) — the
     single bump point §11.1 names — and `migration.py` now routes the saga over
     Boat (`run_boat_migration_phase`). A superseded source's stale epoch is
     detectable. **Closed.**
   - ~~**Atlas must be able to retract or supersede desired state on a host that
     no longer owns a VM.**~~ **Closed.** `DELETE /v1/vms/{uuid}` retracts an
     assertion (§2.4 A′), and `terminate` retracts its own before it touches the
     host. The retraction keeps the fence epoch: dropping it would leave the host
     holding **no** epoch, which is the state any fresh `PUT` satisfies —
     including a stale one — so a retraction that cleared the fence would hand
     back exactly the boot the fence exists to refuse. The desired document now
     carries `server` (`boat_client.desired_state`), so the `server == self` gate
     is checkable — a host boots a VM only when the record names it (§11.1).

     **A retracted VM is refused on both paths** — an absent desired record is now a
     refusal in its own right (`fence.ErrNoAuthority`), the rule the reconciler always
     applied and the API verbs did not, closing the window where `allowedToBoot`
     compared a retracted UUID's epoch to itself and let `start`/`wake` boot an
     evacuated source. `terminate` made this urgent: it destroyed a VM and left
     `{epoch, Running}` behind, so every sweep ran `systemctl start` on an
     `lvremove`d root forever.

   With the bump in place, superseding is detectable too. The honest statement of
   today's guarantee is: **Boat will not boot a VM it was never told about, will
   not boot one it has been told to forget, and — now that repoint bumps the
   epoch — will not boot one it was told about and then merely superseded.** What
   remains is not code but proof: no live two-host migration has been dogfooded
   (the `nbd-client -N ''` defect, WO-4), so no host should carry production VMs
   through a migration until that runs end to end.

1. **Who writes ANCP's bootstrap trust artifacts after the split** (§4). The
   constraint is fixed (operator-signed, Atlas-held key, landing before `boat
   networkd` starts); whether Atlas writes them in the binary's SSH step or `boat
   bootstrap` requests them at registration is unsettled.
2. **API version negotiation.** Atlas must speak `[vN-1, vN]` of the Boat API for
   at least one release window, negotiated on connect. The paths are `/v{N}`; how
   a client discovers `N` is unspecified.
3. ~~**The Boat-side equivalent of `sleep-vm`'s wake-trap gate.**~~ **Closed.** Boat's
   sleep is a hard precondition that refuses when the wake trap is not running (a
   slept-but-unparked VM answers nothing and fails *silently*,
   [32-sleepy-vms.md](./32-sleepy-vms.md)). It now asks Boat's own in-process trap
   loop directly rather than probing the Python `atlas-wake-trap.service`, which would
   have answered `is-active` yes for a reflex nobody was running — the exact silent
   failure the gate exists to prevent.
4. **Quarantine resolution** (§3.4). Reporting is no longer the gap — Boat sends the
   set in every export and `boat_mirror` lands it as `Server.observed_quarantined`
   and per-epoch `quarantined` drift rows (§2.5). What an operator cannot yet do is
   *resolve* one: there is no verb that confirms an artifact set for deletion or
   re-adoption, so the only exit is the `boat` CLI on the host. What that verb asserts,
   and who may assert it, is the open part.
5. **One number**: the hard expiry on a token served under partition (§12). The
   `Host State Snapshot` retention bound is answered — **20 epochs per host** (a
   first answer, not derived), keeping a 100-host fleet at 2000 rows.
6. **Dark-VM service reach** (§7.3) is deferred, not open — the `dial_guest`
   escape hatch is named as a shape, and specifying it is a decision for whenever
   a private-only service VM is actually required.
