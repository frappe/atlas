# Atlas + Boat: splitting the controller from a smart per-host agent

> **How to use this document.** Part I is the shared reference: the rules,
> the source-of-truth model, and the correctness invariants. Part II is a set of
> **work orders**, each written to be executed in one shot by one agent with no
> further decisions required. Read Part I once; then execute one work order.
> Every open decision is closed in Part I §14 — if a work order seems to need a
> decision, it belongs there, not in the work order.
>
> **Repo:** `github.com/frappe/boat`. **Atlas branch:** `feat/boat-split`
> (off `upstream/main` @ `49509c6`). **Spec chapter:** `spec/33-boat.md`.
> Revised 2026-07-27 against post-ANCP, post-sleepy-VMs `upstream/main`.

---

## Context

Atlas today is a smart Frappe/Python controller driving dumb hosts: every host
mutation is one SSH invocation of a staged idempotent Python script whose result
is scraped off an `ATLAS_RESULT=` stdout line (`atlas/atlas/_ssh/runner.py`).
The Frappe DB is declared the source of truth and the host "a rebuildable cache"
(`spec/01-architecture.md`).

**That inversion has already been overturned in production, twice.** ANCP
(`spec/31`) put an authoritative gossip daemon on every host and *deleted* the
controller's networking module. Sleepy VMs (`spec/32`) put a resident wake-trap
daemon on every host that decides, with no DB consult, when a VM comes back to
life. `firecracker-vm@.service` rebuilds netns/routes/nft/disk from an on-disk
`network.env` on reboot; `atlas-pool.service` rebinds the loopback PV. The host
already *decides*. What it lacks is a store, an API, and a name.

We split Atlas into two systems:

1. **Atlas** stays the Frappe/Python **control plane**: cluster-aggregate state,
   placement, cloud/vendor APIs, Central-facing comms, cross-host coordination
   (migration, snapshot sync), service installation.
2. **Boat** is a **native-Go daemon set in `frappe/boat`**, one per host. It owns
   **all** VM operations (lifecycle, snapshots, resize, migration execution) and
   **host/VM networking**, and is the **source of truth for that host's observed
   bare-metal state**. Boat is workload-agnostic — it knows a VM only as a UUID
   plus resource numbers.

The point is not transport speed — the team measured HTTP-vs-SSH and found it
negligible (`spec/04-tasks.md`). The point is **where state and decisions live**.
This resolves `ROADMAP.md` Decision #1 ("where does state live?") — which ANCP
already half-answered by precedent.

---

## THE RULE

**Every host-side service is written in Go, and every one of them is a separate
systemd service invoked through the same `boat` binary.** Multi-call binary
(busybox model): separate units, separate processes, **one build artifact**.

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
`scripts/lib/atlas/_cli.py` is already a multi-call dispatcher; `install.sh`
already symlinks `/usr/local/bin/atlas` to it; and `scripts_catalog.py` already
models a Task as a **verb** run on the host as `atlas <verb> --kebab-flags`,
returning `ATLAS_RESULT=`. `boat` is that CLI's Go successor with the same verb
grammar — so `scripts_catalog.py`, `_variables_to_flags`, and
`task_results.parse_result` survive the port **unchanged** on the Atlas side.

Scope is therefore the **whole host surface**: ~53 `scripts/*.py` verbs, the 20
`scripts/lib/atlas/` modules, the 25 `networkd/` modules, and all 7 units in
`scripts/systemd/`.

Two consequences are load-bearing benefits:

- It deletes the **durable-package staleness** bug class outright — no
  `/var/lib/atlas/bin` module shadowing, no venv, no `sys.path.insert` shims,
  no `STALE_STAGED_PACKAGE_DIRECTORY` purge — and **version skew between host
  components**, because every unit is literally the same build.
- Today the host has **five** invocation styles: inline `python -c "…import…"`
  (`atlas-pool`, `host-mesh`, `gateway`, `atlas-networkd`), durable
  `python /var/lib/atlas/bin/x.py %i` (the `firecracker-vm@` hooks), the `atlas`
  console script (Task verbs over SSH), raw `nft -f` (`atlas-mgmt-firewall`),
  and `jailer-launch.sh`. The rule collapses them to one.

---

## The thesis (one sentence per tier)

- **Boat** — the per-host daemon set that owns everything *mechanical* about a
  host and its microVMs; it *realizes* goal states and *reflexes* to host-local
  events, and knows a VM only as a UUID plus resource numbers; it never learns
  who owns a VM or what runs inside it.
- **Atlas-core** — the regional brain that decides *intent* (where VMs go, which
  addresses they get, what capacity means) and coordinates across hosts; it holds
  fleet truth and hands each Boat goal states, but performs no host mechanics.
- **Atlas-services** — a capability (proxy, gateway, bench-site, TLS) attached to
  a VM *after* it exists, driven by the services module over a plane it owns;
  core VMs and Boats are oblivious to it.

The dividing rule for "smart Boat" vs "only knows the VM exists":

> **Boat may decide any question answerable from (VM UUID) + (host-local facts:
> nft counters, on-disk markers, `host_signature`, free RAM, launcher support).
> Boat may NOT decide any question whose answer would differ based on who owns
> the VM, what runs in it, or what it costs.** Boat owns *realization and
> reflex*; Atlas owns *enrolment and intent*.

Under this rule sleep-on-idle and wake-on-inbound-TCP are **pure mechanics** (the
*reflex* is Boat's; the *enrolment* — `sleep_on_idle`, `idle_timeout_seconds`,
the firewall map — is Atlas policy handed down). Capacity self-report must stop
at **raw numbers**: Boat reports free cores/RAM/pool bytes; the Sleeping-axis
accounting (`api/server_capacity.py`) stays in Atlas, or "Sleeping is
billing-relevant" leaks into Boat.

---

# Part I — Invariants

## §0. What already shipped (read this before contradicting anything)

**ANCP — `spec/31-ancp-network-control-plane.md`, status SHIPPED.**
`scripts/lib/atlas/networkd/` (25 modules on disk; the spec says 19, written
before late additions — 395 host-lib unit tests) is a
decentralized gossip control plane on every host. It **deleted**
`atlas/atlas/host_mesh.py` and the controller's `reconcile_host_mesh` /
`sequenced_migration_cutover` path entirely. Hosts maintain Membership +
Ownership records over gossip + anti-entropy, and program `wg-mesh` themselves.

**Boat must claim ANCP as precedent, not contradict it.** Three concrete
consequences:

1. **The mesh is not in the Atlas↔Boat contract at all.** Any statement of the
   form "Atlas computes mesh peers and Boat applies them" is wrong.
2. **The VM↔network seam already exists and is a file.** ANCP §11.3 defines
   `/etc/atlas-networkd/local-ownership.json`, written atomically
   (`O_TMPFILE` + `rename`) by `vm-network-up.py` / `vm-network-down.py`, read by
   the daemon. There is deliberately **no Frappe fallback**. Boat simply becomes
   that file's writer. Zero rework of shipped ANCP code.
3. **The Taste.md exception is already granted.** ANCP §6 documents, in the spec,
   that a long-running host daemon is an explicit sanctioned deviation from "one
   operation = one script = one Task row" and "no agent runs on the server."
   Boat inherits that precedent instead of re-arguing it.

**Sleepy VMs — `spec/32-sleepy-vms.md`, shipped (PR #126).**
`scripts/atlas-wake-trap.py` + `scripts/lib/atlas/park.py` are a resident daemon
polling nft named counters (`wake_<uuid>`) once a second; the first inbound TCP
SYN to a parked `/128` wakes the VM by removing the `sleeping` marker and
starting the unit. It re-sweeps park state from on-disk markers at boot, DB-free.
This is the purest existing example of the Boat dividing rule, and Boat absorbs
it natively.

**What this leaves for Boat:** VM lifecycle, per-VM networking, observed state,
adoption, crash recovery, cross-host op execution, and supervision of the
sibling units.

## §1. Source-of-truth model: desired vs observed

Atlas remains authoritative for **desired state** (intent). Boat becomes
authoritative for **observed/actual state**. This *refines* `spec/01` principle
#2 rather than reversing it: a lost host is still rebuildable *from desired
state*; Atlas keeps a **disposable read-through mirror** of Boat's observed state
(never authority), maintained via whole-host export (§2.5) + `/watch` stream +
Boat-pushed heartbeat.

| Field / fact | Class | Authority | Reconciliation |
|---|---|---|---|
| `name` (UUID), `title`, `tenant`, `image`, `ssh_public_key` | Desired (identity) | Atlas | Immutable; Boat receives, never mutates |
| `server` (placement) | Desired | Atlas | CAS-gated; only migration cutover repoints (§8) |
| `vcpus`, `cpu_max_cores`, `cpu_mode`, `memory_megabytes`, `disk_gigabytes`, `data_disk_gigabytes` | Desired (spec) | Atlas | Boat applies; reports `observed_*` back |
| **`desired_power`** ∈ {Running, Stopped} | **Desired** | **Atlas** | The ONLY input to Boat's power reconciler (§11.3) |
| **`observed_status`** (Running/Stopped/Paused/Sleeping/Failed) | **Observed** | **Boat** | Replaces today's "status set from Task success" |
| `sleep_on_idle`, `idle_timeout_seconds` | Desired (enrolment) | Atlas | Boat runs the reflex, never chooses the policy |
| `ipv6_address` | Desired-binding | Atlas (v1); Boat later (§6.2) | CAS-gated; union-reconciliation law (§11.4) |
| `mac_address`, `tap_device`, `private_address` | Derived (pure fn of UUID/tenant) | Either (deterministic) | `networking.py:derive_*` — recomputed identically both sides |
| `has_memory_snapshot`, `last_started/stopped`, `last_traffic_at`, `boot_id` | **Observed** | **Boat** | Marker/counter-driven |
| `public_ipv4` (reserved-IP attach) | Desired-binding | Atlas (vendor alloc) → Boat (NAT apply) | CAS-gated on the host's reserved-IP slot (§11.5) |
| LV inventory, real sizes, thin-pool fullness | **Observed** | **Boat** | Feeds placement |
| `vcpus_total`, `memory_megabytes_total`, `pool_disk_gigabytes_total` | **Observed** (host facts) | **Boat** | Live fact, not a bootstrap snapshot |
| Running firecracker PID / API socket | **Observed** | **Boat** | Boat re-attaches on restart (libvirtd pattern) |
| **`boat_version` (running binary)** | **Observed** | **Boat** | Reported in every export (§2.5); drift vs desired is what drives §5 |
| `boat_version` (desired) | Desired | Atlas | `Server.boat_version`; Atlas pushes, staggered (§5) |
| `boot_epoch` (per-UUID fence) | Control | **Atlas issues**, Boat mirrors + enforces | §11.1 |

**The drift guard, reworked.** Today `virtual_machine.py:validate()` freezes
resource fields (`RESIZE_MUTABLE`) because Atlas has no truthful readback. With
Boat owning observed state, drift (`desired ≠ observed`) becomes a **surfaced
state** for the *observation* fields — Atlas's reconcile flags it and Boat's
reconciler drives observed→desired. **But drift is corruption, not a display
nuisance, for contended reservations** (`server`, `ipv6_address`, reserved-IP,
capacity gate): those stay **frozen-except-through-CAS-verbs** (§11.2). Identity
immutability (`IMMUTABLE_AFTER_INSERT`) stays; Boat keys its store on the same
UUID.

## §2. The Atlas ↔ Boat contract

### 2.1 The API is the complete functional surface (C3)

**Every capability Boat has is an endpoint.** Lifecycle, bootstrap (§4),
self-update (§5), sibling-unit supervision, host facts, whole-host export. The
`boat` CLI and the systemd units are **clients of that same surface** — never
alternate paths with powers the API lacks. This is what makes Atlas able to drive
a host completely, and what makes the CLI a truthful break-glass tool rather than
a second implementation.

### 2.2 Transport — HTTP/JSON over the mgmt tunnel, typed by an IDL

- **HTTP/1.1 + JSON on the wire, SSE for streams** — not gRPC. Atlas already
  speaks HTTP/JSON to DO/Scaleway/S3/Central via `requests`; `grpcio` would
  violate the few-deps ethos and become every downstream app's dependency. Boat's
  Atlas-side client is the shape of `digitalocean.py` / `api/central_link.py`.
- **A real IDL**: an OpenAPI 3 document at `api/openapi.yaml` in the Boat repo is
  the source of truth; the typed Go server and the typed Python client are
  **generated from it at build time and checked in** (zero new *runtime* dep).
  This preserves the typing discipline `_task.py:TaskInputs`/`TaskResult` already
  give, across the language boundary. Explicit `/v{N}` path versioning.
- Boat's API listens **only on the mgmt-tunnel address and `/run/boat.sock`**,
  never the public interface — the tunnel is the transport-security boundary,
  exactly as for Central↔Atlas (`spec/21`).

### 2.3 Auth & least privilege

- **Boat runs non-root** as a service user with the **existing pinned NOPASSWD
  sudoers allow-list** (`scripts/sudoers.d/atlas-tunnel` is the template:
  enumerated `wg`, `nft -f …`, specific `systemctl`/`firecracker` calls). The
  individual privileged calls need root, not the daemon.
- **Verb allow-list at the API boundary** — port `scripts_catalog.allowed_scripts()`
  into the API layer. No arbitrary-command endpoint, ever.
- **Short-lived scoped tokens (or mTLS over the tunnel)**, minted per host by
  Atlas, with a defined rotation-under-partition rule (§12).

### 2.4 The operation set

All idempotent, replayable, versioned; every request carries `op_id`.

- **A. Desired-state apply (the durable primitive)** — `PUT /v1/vms/{uuid}` with
  the full desired spec + `boot_epoch`. Boat diffs against its store and runs
  forward to converge. This is how Atlas re-asserts intent on reconnect.
- **B. Lifecycle verbs** — `POST /v1/vms/{uuid}/{start,stop,pause,resume,sleep,
  wake,resize,snapshot,warm-snapshot,rebuild,terminate,reserved-ip}`,
  `POST /v1/images/sync`, migration sub-ops (§8). Each **mutates desired state
  and lets the reconciler act** — verbs never mutate the host directly (§11.3).
- **C. Observed read/watch** — `GET /v1/vms/{uuid}`, `GET /v1/vms`,
  `GET /v1/host`, `GET /v1/export` (§2.5), `GET /v1/watch` (SSE deltas + op
  progress). CAS reads via `If-Match: <observed-epoch>` (§11.2).
- **D. Host control** — `POST /v1/bootstrap` (§4), `POST /v1/update` (§5),
  `GET|POST /v1/units/{name}` (supervision).

### 2.5 Whole-host export and one-shot sync (C4)

`GET /v1/export` returns Boat's **entire** observed state in one document: every
VM's observed doc, host facts, LV/thin-pool inventory, network state,
sibling-unit liveness, every fence epoch, and **the running `boat_version`**.
Atlas ingests it **in one transaction** to rebuild its mirror.

**Atlas lands it in two places** (both, deliberately):
- **Hot fields denormalized onto `Server`** — capacity, unit liveness,
  `observed_boat_version` — because placement queries them on every provision and
  cannot afford a document parse. Reuses the existing capacity fields
  (`vcpus_total`, `memory_megabytes_total`, `pool_disk_gigabytes_total`) that
  **Refresh Capacity** already stamps.
- **The full document archived as a `Host State Snapshot` row**, keyed by host +
  observed-epoch, for debugging, mirror rebuild, and drift forensics. Retention
  is bounded (keep N per host); the mirror stays obviously disposable.

Carrying `boat_version` in the export is what closes the update loop: Atlas
pushes a desired version (§5) and *observes* the running one here, so version
drift is a surfaced state like any other, not a separate bookkeeping channel.

- This **supersedes the periodic `GET /vms` sweep** as the backstop. `/watch` SSE
  remains for low-latency deltas; the export is the truth-restoring primitive.
- It must be a **consistent snapshot**: one short bbolt `db.View`, materialized
  and released *before* streaming — never held under the write lock (§11.6).
- It carries a monotonic **observed-epoch**, so the CAS verbs (§11.2) can be
  `If-Match`-ed against the exact snapshot Atlas ingested.
- Four consumers, all of them existing hard cases: reconnect after partition
  (§9), first adoption (§3.4), rebuilding a lost Atlas mirror, and the
  post-auto-update health check (§5).
- **State the symmetry explicitly:** `PUT` desired is how Atlas re-asserts
  intent; `GET /v1/export` is how Boat re-asserts fact. Those two calls, run
  back to back, fully resynchronize a host from any state.

### 2.6 Push commands, pull truth

Commands push (Atlas→Boat). State is pulled: Atlas maintains its mirror from SSE
`/watch` plus the §2.5 export as backstop. Boat also POSTs a signed heartbeat on
significant transitions (reusing the `satellite_events.py` signed-webhook shape).
Push for liveness, pull for truth.

### 2.7 Boat op-log vs Frappe Task rows

Boat keeps its own append-only op journal (crash-recovery truth). The Frappe
`Task` row stays Atlas's operator-facing audit + replay record; they share an
`op_id`. Atlas creates the Task (Pending), calls Boat with `op_id = task.name`;
Boat streams progress over SSE; Atlas folds each chunk onto
`live_output`/`progress_line` via the existing `task_log` realtime event (the
streaming seam of `spec/22` survives verbatim, sourced from SSE instead of an SSH
log-tail). On completion Boat returns the typed result; Atlas writes
`stdout`/`exit_code`/`status`. Re-POST of an in-flight or completed `op_id`
returns the same result, never double-runs.

## §3. Boat internals

### 3.1 Store — BoltDB (bbolt)
Single-file, pure-Go, zero-CGO, transactional — ideal for a static binary; the
flyd pattern. Buckets: `vms/<uuid>`, `ops/<op_id>` (append-only journal), `host`,
`alloc/ipv6`, `fence/<uuid>`, `held/ipv6` (forward leases, §11.4), `progress/`
(§11.6). On-disk artifacts (`network.env`, LV names, markers) are the ground
truth Boat re-derives from on adoption; bbolt is the fast transactional index.

### 3.2 FSM / reconciler
Every op is a **forward-only state machine** (flyd's "always run forward"):
ordered, idempotent, checkpointed steps — the discipline `migration.py`
(`PHASE_ORDER`, `advance_migration`) already encodes for migration, generalized
to all ops. A background **reconciler** (kubelet syncLoop) drives observed →
`desired_power`/desired-spec and self-heals a dropped command. **One actor
(goroutine) per VM** serializes the reconciler and any verb so they never
double-drive (§11.3).

### 3.3 Crash recovery — re-attach, don't restart
On start Boat (libvirtd's model) scans for running firecracker + API sockets
(deterministic per-UUID via the ported `paths.py:VirtualMachinePaths`),
**re-attaches** via the FC API socket, replays the `ops` journal (non-terminal
ops re-entered at checkpoint), and rebuilds the observed store from the host scan
(§3.4). This daemonizes what the systemd units + `atlas-pool.service` already do
on reboot, under one resident process with a durable journal.

**Fail-closed on an empty fence store**: a Boat that lost bbolt refuses to boot
any UUID until it re-registers and re-pulls desired + epochs. A wiped Boat that
boots everything it finds on disk is the single most dangerous state (§11.1).

**Re-attach is load-bearing far beyond crash recovery** — it is what makes
auto-update (§5) safe, because a binary swap restarts the daemon under live VMs.

### 3.4 Re-adoption / host scan
The enumerators already exist in `scripts/reset-server.py`
(`_list_vm_directories`, `_list_units`, `_list_netns`, `_list_atlas_links`,
`_list_ndp_proxy`, `_list_atlas_lvs`, …). Boat's Go port uses the same
enumeration *inverted*: it **ingests** each artifact, cross-checks FC sockets for
liveness, and reconstructs the observed doc per VM. Ambiguous or partially
torn-down artifacts (a crash mid-terminate) go to a **quarantine** state
requiring Atlas/operator confirmation rather than being ingested as truth.

### 3.5 The native-Go rewrite — de-risked by differential testing
Full native Go, but **not blind**. The Python host surface is the **conformance
oracle**: 20 `scripts/lib/atlas/` modules, 25 `networkd/` modules, ~53 verbs, and
their millisecond unit tests (`test_lvm.py`, `test_park.py`,
`test_networkd_*.py`, …).

- Each Go module is validated against a **golden corpus** captured from the
  Python module (same inputs → byte-identical rendered commands / results). The
  Python `run()` parameterized-quoting model (`_run.py`) is the spec the Go
  command-builder must match.
- A **differential phase runs the Go and Python implementations side by side on a
  real host** (the Go daemon shelling to the Python verb as reference) and
  asserts identical host effects, before that op cuts over to native-only. This
  retires the big-bang risk (LVM CoW ordering, jail nesting, EUI-64, off-link
  routing, wg peer-table rendering) module by module.
- **Port order — restart-sensitivity and hot-path first:** adoption scan, FC
  re-attach, wake-trap loop, network apply; then LVM/rootfs/image-sync; cold
  paths last.

### 3.6 CLI ↔ daemon
`boat` CLI talks to the resident daemon over a **unix socket**
(`/run/boat.sock`, 0660) speaking the same HTTP/JSON API (§2.1). `boat vm start
<uuid>`, `boat vm ls`, `boat host facts`, `boat export`, `boat adopt`. This is
also the operator break-glass path when Atlas is unreachable.

### 3.7 The unit set and the multi-call binary
Boat (`boat daemon`) supervises the sibling units: it owns their
start/stop/restart, surfaces each one's liveness in `GET /v1/host`, and **never
reaches into networkd's gossip state** — supervision is lifecycle only, the ANCP
boundary of §0 stays intact. Because every unit is the same build, a `boat`
upgrade is atomic across the host; there is no partial-version window.

Unit template follows `scripts/systemd/atlas-networkd.service`: `Type=notify`,
`WatchdogSec`, `StartLimitIntervalSec`/`StartLimitBurst`, `RuntimeDirectory` +
`StateDirectory`, `Restart=on-failure`.

## §4. Bootstrap, registration, re-adoption (C1)

**`boat bootstrap` brings a bare host to Active by itself.** Thin pool, network
scaffold, firecracker/jailer install, sudoers, unit installation, then
self-registration to Atlas. This replaces `scripts/bootstrap-server.py` as an
SSH-driven Task.

- **Landing the binary**: reuse the existing SSH `install.sh` /
  `Server.bootstrap()` path to place the signed binary — do NOT invent a new
  channel; SSH stays for bootstrap and break-glass (§12). `install.sh` shrinks to
  "drop the signed binary + install the units"; `Server.bootstrap()` shrinks to
  "drop binary, invoke `boat bootstrap`, await registration".
- **Registration**: on first boot Boat generates its token/keypair and registers
  to Atlas, mirroring the `central_link.py` `provision_tunnel`/`confirm_tunnel`
  **armed auto-revert handshake** so a failed handoff never bricks the host.
- **Re-adoption**: on a host that already has VM state (upgrade, restart), Boat
  runs the §3.4 scan and adopts existing VMs idempotently, re-attaching to live
  firecracker rather than restarting it.
- Bootstrap is idempotent and re-runnable, exactly as `bootstrap-server.py` is
  today ("Safe to re-run on an Active server").

## §5. Self-update (C2)

Boat updates itself. Required shape:

1. **Desired version** lives in Atlas (`Server.boat_version`). **Atlas pushes
   it** — there is no host-side poll. Atlas already knows which hosts are
   `Unknown` or mid-op, so it is the only place the staggered canary rollout
   (step 6) can be driven correctly; a host-side poll would have to reconstruct
   cohorts and jitter locally, which is exactly where fleet-wide simultaneous
   updates come from. An unreachable host simply updates late, on reconnect.
   The **running** version comes back in every export (§2.5), so desired-vs-
   observed drift is visible without a separate channel.
2. **Verify signature + checksum** before anything else, then **atomically
   rename** over `/usr/local/bin/boat`. Keep **N-1 on disk**.
3. **Quiesce first.** Refuse new ops; checkpoint in-flight ones via the journal
   (§3.2). The journal is what makes an interrupted update replayable.
4. **Restart units in a defined order**, and **re-attach to running firecracker
   rather than restart it** (§3.3). This is the crux: THE RULE means one binary
   backs *every* unit, so a swap re-points all of them at once.
   **Auto-update is therefore hard-gated on FC re-attach and cannot ship before
   it.** Sleeping VMs must stay asleep across an update — the `sleeping` marker
   is authoritative, and a restart must not trip the wake path.
5. **Health-check after swap** (`GET /v1/export` round-trip + unit liveness);
   **roll back to N-1** on failure.
6. **Atlas staggers the fleet** — canary first, then waves. A simultaneous
   fleet-wide auto-update is the one failure mode that can brick every host at
   once, and nothing inside a single Boat can prevent it.
7. Extends `spec/23-supply-chain.md`: signed releases, checksum-pinned install,
   reproducible builds, provenance.

## §6. Networking split (post-ANCP)

### 6.1 The tiers

| Concern | Owner | Boat's role |
|---|---|---|
| Private `fdaa::/16` mesh, membership, ownership gossip, `wg-mesh` peer table | **ANCP** (decentralized, §0) | supervises the unit; **writes `local-ownership.json`** |
| Per-VM netns/veth/tap, NAT44, proxy-NDP, `/128` route, per-VM nft isolation | **Boat** | computes + applies |
| Park / wake trap, per-VM firewall | **Boat** (reflex) | applies; enrolment from Atlas |
| Reserved-IP 1:1 NAT (public v4) | **Boat** | applies; CAS on the host slot |
| Customer-gateway host forwarding | Atlas-computed | Boat applies |
| Vendor reserved-IP allocate/assign/release; public IPv6 allocation | **Pure Atlas** | none |
| Central mgmt tunnel | **Pure Atlas** | none (+host) |

Rule: a pure function of *(this VM, this host)* is Boat's; a function of *(fleet,
placement, tenancy)* is Atlas-computed and Boat-applied; a function of *(vendor
account / Central)* is pure Atlas; **and the private-plane mesh is nobody's — it
is gossiped.**

### 6.2 Public IPv6 allocation — stays in Atlas for v1
Today `allocate_ipv6(server)` is **cluster-aware**: a live VM on host B can own an
address in host A's range after a keep-address migration (`spec/24`); filtering
by `server ==` alone double-allocates. The naive "birth-host allocates" model is
**unsound** (reuse-after-terminate collides with a permanent DO forward;
vacate-then-reallocate races a keep-address move). Therefore:

- **v1: allocation stays in Atlas** (unchanged, cluster-aware); Boat only
  *applies* the address it is handed and reports the in-use set as observed.
- **Later (WO-6, gated): push allocation to Boat** only once the
  **union-reconciliation law** and the **forward-lease** (§11.4) are in the spec
  and the fence epoch is live.

Note this is the **public** plane. The private plane is already decentralized by
ANCP and is not part of this question.

## §7. Guest-service plane & where services live

### 7.1 Atlas keeps the direct guest-SSH plane (locked)
Guest-service config (proxy maps, bench/site deploy, gateway in-guest config) is
*guest-plane* work — application config inside a customer OS, definitionally not
bare-metal host state. It stays in Atlas via `connection_for_guest` (SSH to the
guest public v6 with the Atlas key). Boat does **not** mediate guest traffic and
stays workload-agnostic. Routing guest-exec through Boat is rejected — it would
make Boat a generic guest-command proxy and re-entangle service semantics.

### 7.2 Who injects identity — the opaque-blob interface
Boat owns the rootfs and runs identity injection at provision (the ported
`rootfs.py`), but **treats guest identity as opaque bytes**:

- Boat receives a `ProvisionSpec` whose `identity` is `{uuid, ipv6, ipv4_link,
  private_address, authorized_keys_blob, extra_env: [{path, content}]}`.
- Boat computes hostname/machine-id **from the UUID by a fixed rule it owns**
  (naming a host after its UUID is mechanics), regenerates host keys, and writes
  `authorized_keys_blob` + every `extra_env` entry **verbatim, without parsing**.
- Service-semantic fields are **not** named in Boat's schema. `routing_base_url`
  (`spec/18`) arrives as one anonymous `extra_env` entry
  `{path:"/etc/atlas-routing.env", content:"…"}` Boat cannot tell from any other
  guest env file. `host_signature` (warm-restore guard) and reserved-IP NAT are
  guest-agnostic host mechanics — Boat owns them outright.

### 7.3 Accepted constraint: dark/private-only VMs
`connection_for_guest` requires a public v6, which dark VMs
(`public_networking=0`, `validate_dark_vm_has_identity`) lack. **For v1 the
service layer only supports public VMs**; private-only *service* VMs are out of
scope (tenant private-only VMs that run no Atlas-managed service are unaffected).
Future escape hatch: a **single bounded `boat.dial_guest(uuid, port) → socket`**
primitive (an L4 forward into the VM's netns; Boat never gets a command, shell,
or the guest credential) with Atlas's SSH layered over it.

### 7.4 Services stay in Atlas, as an in-app module boundary
The federated second Frappe app (`spec/30`) is abandoned as a *deployment*, but
its seam is right. Runnable in parallel with the Boat split (Track S):

- Introduce an in-app `atlas/services/` module holding the service modules
  (`proxy`, `tcp_proxy`, `customer_gateway`, `bench_routing`, `bench_image`,
  `deploy_site`, `front_door`, `tls/`, `dns/`) + doctypes, plus the `Service` /
  `Service Binding` registry lifted from `spec/30 §3` — **without** the separate
  bench/DB/SSH engine.
- **De-fuse the generic VM controller**: `Virtual Machine` loses `is_proxy`,
  `is_gateway`, `build_mode`, `pilot_credential_id`, and `terminate()` loses its
  five-way service fan-out (`_deprovision_proxy`/`_revoke_tunnels`/…). Those
  become a `Service Binding` per VM and an `on_trash` observer in the services
  module. Dependency is one-directional (services→core, never core→services).

## §8. Cross-host operations as sagas
Atlas is the **saga orchestrator**; each Boat runs a **local idempotent FSM**.
This is a near-verbatim relocation of `migration.py` (already a resumable phase
machine with `reconcile_migrations` as the safety net); the change is that each
phase becomes an RPC to the relevant Boat instead of an SSH `run_task`.

| Atlas phase | RPC target | Boat-local effect |
|---|---|---|
| Export | source-Boat | export base/disk, checkpointed |
| TargetPrepare | target-Boat | receive base, build dm-clone; if keep-address, arm forward tunnel |
| InjectIdentity | target-Boat | identity inject (opaque blob) |
| Cutover | target-Boat (+ source stop) | boot on dm-clone read-through; source fast-stops |
| Hydrate | target-Boat | poll hydration to 100% (long forward-only copy, self-paced) |
| Collapse | target-Boat | swap dm-clone → linear once local |
| Repoint | Atlas | re-point Subdomains/proxy + record new `server` + **bump `boot_epoch`** |
| Cleanup | source-Boat | cleanup source |

**Cutover completion requires positive fencing of the source**, not just target
boot (§11.1): Atlas must not advance to Repoint until it has an acked heartbeat
from the target at the new epoch AND has fenced the source (epoch-bump acked, or
source confirmed `Unknown`). Same shape for warm-snapshot fan-out (each
target-Boat pulls the golden, validates `host_signature`, restores) and S3
snapshot sync (**Atlas presigns**, owns S3 creds; **Boat transfers** bytes via
the presigned URL — Atlas never proxies bytes).

Ownership changes propagate to the private plane **for free**: the VM's `/128`
leaves the source's `local-ownership.json` and appears in the target's, and ANCP
gossips it. Atlas does not sequence the mesh (`sequenced_migration_cutover` is
gone).

## §9. Partition & failure semantics
- **Boat when Atlas is unreachable**: autonomous. Keeps every VM running (no
  desired change ⇒ no action); keeps serving host-local reflexes (wake-traps,
  reboot recovery from `network.env`, pool rebind); ANCP keeps gossiping
  independently; the local `boat` CLI still works (break-glass); buffers observed
  reports to replay on reconnect.
- **Atlas when a Boat is unreachable**: marks the host **`Unknown`, not dead** —
  must not assume VM death (Nomad's UNKNOWN). Placement excludes an Unknown host
  from *new* arrivals but does not evict its VMs. Freezes the stale mirror,
  flagged stale, rather than nulling it.
- **Reconnect**: Atlas re-`PUT`s desired state (idempotent no-ops on match) and
  pulls `GET /v1/export` to rebuild the mirror in one transaction (§2.5); Boat
  replays buffered transitions; `/watch` resumes. Any in-flight op is resumed by
  Boat's FSM; `op_id` dedupe guarantees exactly-once.
- **Split-brain** is prevented by the fence epoch (§11.1), not by phase ordering.

## §10. Observability & audit
The `spec/22` model survives intact, re-sourced: the Frappe `Task` row stays the
live progress carrier (`live_output`, `progress_line`, `stdout`, `status`); Boat's
op streams over SSE and Atlas folds each chunk on via the existing `task_log`
realtime event. Boat's `ops` journal is crash-recovery truth
(`GET /v1/ops/{op_id}`); the operator's audit surface stays the Task row + the
"Running Operations" fleet view, which now also reflects Boat-reported observed
transitions (a wake-trap flip) — a truthful fleet picture Atlas could never show
before. Every op writes an append-only audit record equivalent to today's
immutable `Task` / `SSH Command Log` rows (audit parity is a hard requirement).

## §11. Correctness invariants
Load-bearing; must be in `spec/33-boat.md` **before Boat code is written**.

1. **Fence epoch** — a per-UUID monotonic `boot_epoch`, **Atlas is the sole
   issuer**, stored on the VM row and mirrored into each Boat's `fence/<uuid>` on
   every desired PUT. Boat **refuses to boot** a `<uuid>` unless its local epoch ≥
   the on-disk unit's epoch AND desired `server == self`. Empty fence store ⇒
   **boot nothing**. Epoch bumps at exactly one point: migration Repoint. Makes
   force-reprovision safe by construction.
2. **CAS on contended reservations** — placement, capacity gate, migration-target
   choice, and reserved-IP attach go through `PUT If-Match: <observed-epoch>`;
   Boat returns `409` if its state moved since the mirror epoch.
   `server`/`ipv6_address`/reserved bindings stay frozen-except-through-CAS-verbs.
3. **`desired_power` vs `observed_status` split** — verbs mutate `desired_power`;
   the reconciler is the single per-VM actor that drives observed → desired.
   Precedence: explicit `desired_power=Stopped` **outranks** a wake-trap (a
   stopped VM is not woken by traffic).
4. **Forward-lease + union-reconciliation for public IPv6** — a `/128` is in-use
   if **Atlas-claims-it OR host-sees-it**; free **iff both** agree. A kept address
   is a lease on its source range recorded in `held/ipv6` **before**
   `vm-network-down` runs; not freeable while any host forwards it.
5. **Write-ahead journaling** — the journal records the non-idempotent *decision*
   (which address, which IP, which host slot) **before** the host side effect, so
   a crash-then-retry replays deterministically; allocation + LV create + op
   completion in one bbolt txn; reserved-IP attach is CAS on the host slot.
6. **bbolt isolation** — heartbeat + fence reads are **lock-free** (in-memory
   atomic snapshot, never the write lock); long ops (image sync, hydration, RAM
   snapshot) write only the `progress/` bucket, never the hot `state/` bucket;
   SSE readers and `GET /v1/export` snapshot via a short `db.View` and release
   before streaming. Prevents a busy host being mis-declared partitioned.

## §12. Security model (no-worse-than-today is the bar)
- **Non-root daemon** under the existing pinned sudoers allow-list
  (`scripts/sudoers.d/`) — the single biggest blast-radius reducer.
- **Verb allow-list enforced at the API boundary**; no arbitrary-command endpoint.
- **Short-lived scoped tokens or mTLS over the tunnel**, per host, minted by
  Atlas. **Rotation under partition**: if Boat can't reach Atlas it serves the
  last-valid token until a hard expiry; Atlas re-mints on reconnect — never an
  unreachable-and-trusting-a-stale-token-forever window.
- **Audit parity**: append-only op records equivalent to today's immutable Task /
  SSH Command Log.
- **Supply chain** (a *new* threat vs SSH, and the attack surface auto-update
  creates): signed releases, checksum-pinned install, reproducible builds,
  provenance extending `spec/23-supply-chain.md`.
- **Keep SSH as break-glass**: inbound key-SSH with the fixed verb catalog stays
  as the out-of-band channel to restart or replace a wedged Boat. **WO-6 does NOT
  delete SSH** until a proven equivalent recovery channel exists.

## §13. Repo conventions (C5)

The Boat repo follows Atlas's style and semantics. Seed `CLAUDE.md` +
`llm/Taste.md` in `frappe/boat` stating exactly this:

- **Style**: small functions (~10 lines), files 100–300 lines, packages under ~15
  files, **no abbreviations** (`virtualMachine`, not `vm`, outside five-line
  scopes), clean over clever, reuse over new code, always tests.
- **One operation = one verb = one op record** — the Go analogue of "one operation
  = one script = one Task row". Compose *inside* a verb, never by chaining RPCs
  from the caller.
- **Every verb idempotent** — retry = re-run, no special repair mode.
- **Fail loud at the boundary, never fall back** — the rule that makes Atlas raise
  on an SSH or vendor error rather than degrade.
- **Tests next to the code they cover.**
- **The spec chapter is the source of truth** — `spec/33-boat.md` in Atlas governs
  the contract; the Boat repo's README points at it.

## §14. Decisions closed (so no work order stalls)

| Question | Closed as |
|---|---|
| Boat vs ANCP | Supervise as a sibling unit; networkd → `boat networkd` (WO-5) |
| Repo | `github.com/frappe/boat`; one `cmd/boat` multi-call entry + `internal/<service>/` per service; module `github.com/frappe/boat` |
| Host binary path | `/usr/local/bin/boat`, replacing the `atlas` symlink `install.sh` makes today |
| Verb grammar | identical to `atlas <verb> --kebab-flags` + `ATLAS_RESULT=`, so `scripts_catalog.py` and `_variables_to_flags` are unchanged |
| Per-host enable switch | `Server.boat_enabled` (Check), default 0; `BoatClient` used only when set |
| Per-VM authority reversal | `Virtual Machine.observed_authority` ∈ {DB, Boat}, default DB |
| Spec chapter | `spec/33-boat.md` |
| IDL + codegen | OpenAPI 3 at `api/openapi.yaml`; Go server + Python client generated at build time, **checked in** |
| Task↔op identity | `op_id == Task.name`; re-POST idempotent |
| Listen address | mgmt-tunnel address + `/run/boat.sock`; never public |
| Auth | short-lived per-host token minted by Atlas, `central_link.py` armed-handshake shape |
| Privilege | non-root `boat` user + pinned sudoers, modelled on `scripts/sudoers.d/atlas-tunnel` |
| Fake provider | `BoatClient` honours `is_fake_server()` exactly as `run_task` does — fake hosts never get a Boat call |
| Mirror backstop | `GET /v1/export` (§2.5) replaces the periodic `GET /vms` sweep |
| **Transport scoping** | Boat **shares the Central-managed tunnel**, and must tolerate a re-provision: exponential-backoff reconnect, buffered observed reports, no op loss. The blip is a handled event, not an outage. No second tunnel. |
| **Mirror storage** | **Both**: hot fields (capacity, unit liveness, `observed_boat_version`) denormalized onto `Server` for placement queries; the full export archived as a `Host State Snapshot` row keyed by host + observed-epoch, bounded retention |
| **Go dependencies** | **Pragmatic — argued per dependency in review.** No standing stdlib-only rule. Record each accepted dep and its rationale in the Boat repo's `CLAUDE.md` so the list stays legible for the signed-release/supply-chain sign-off (§12) |
| **Update trigger** | **Atlas pushes**; no host-side poll. Running version reported in the export (§2.5) so drift is observed state |
| `atlas` vs `boat` on the host | Both coexist until WO-6. `boat` installs alongside; the `atlas` symlink and venv are retired only when the verb port completes |

---

# Part II — Work orders

Each work order is written to be executed in one shot. **If a work order appears
to need a decision, that decision belongs in §14, not in the work order.**

Template — every WO fills all nine fields:

```
Ships / Preconditions / Flag / Creates / Modifies / Contract / Does NOT /
Acceptance / Rollback
```

### WO-0 — Walking skeleton

- **Ships**: a `boat` binary that starts, serves the API on the tunnel + unix
  socket, persists to bbolt, and starts/stops one real VM driven from Atlas.
- **Preconditions**: `frappe/boat` empty repo; one bootstrapped dev host.
- **Flag**: `Server.boat_enabled`.
- **Creates** (boat): `CLAUDE.md`, `llm/Taste.md` (§13), `go.mod`, `cmd/boat/`
  (multi-call dispatch), `internal/api/` (generated server), `internal/store/`
  (bbolt), `internal/vm/` (start/stop), `api/openapi.yaml`,
  `systemd/boat.service`, `sudoers.d/boat`.
- **Creates** (atlas): `atlas/atlas/boat_client.py`.
- **Modifies** (atlas): `Server` doctype (+`boat_enabled`);
  `virtual_machine.py:start/stop` to branch on the flag.
- **Contract**: `POST /v1/vms/{uuid}/start|stop` with `op_id`; `GET /v1/vms/{uuid}`.
  `BoatClient` mirrors `run_task`'s signature so the call site is a one-line
  branch, and honours `is_fake_server()`.
- **Does NOT**: touch networking, adoption, other verbs, or ANCP.
- **Acceptance**: on the dev host, `boat vm start <uuid>` and the Atlas desk
  Start button both boot the same VM; a Task row carries streamed output; a
  re-POST of the same `op_id` returns the first result without re-running.
- **Rollback**: clear `boat_enabled`.

### WO-1 — Observed state, adoption, export, fencing

- **Ships**: Boat as the truthful observer of its host, and Atlas mirroring it —
  advisory only, DB still authoritative.
- **Preconditions**: WO-0.
- **Flag**: `boat_enabled`; authority stays DB.
- **Creates** (boat): `internal/adopt/` (invert `reset-server.py` enumerators),
  `internal/fcattach/` (FC API socket re-attach), `internal/export/` (§2.5),
  `internal/watch/` (SSE).
- **Creates** (atlas): `Host State Snapshot` doctype (host + observed-epoch + the
  full export document, bounded retention).
- **Modifies** (atlas): `Virtual Machine` (+`desired_power`, `observed_status`,
  `boot_epoch`, `observed_authority`); `Server` (+`observed_boat_version` and the
  hot denormalized fields, reusing the existing `*_total` capacity fields); a
  mirror-ingest path for `GET /v1/export` writing both landing places.
- **Contract**: `GET /v1/export` — whole-host document + observed-epoch +
  running `boat_version`, from one short `db.View`; `GET /v1/watch` SSE;
  `GET /v1/host`.
- **Does NOT**: flip authority; change any lifecycle verb's behaviour.
- **Acceptance**: kill and restart `boat` under a running VM — the VM keeps
  running and Boat re-attaches (no reboot); `GET /v1/export` round-trips into
  Atlas's mirror and matches reality; an empty fence store refuses to boot
  anything; a half-terminated VM lands in quarantine, not in the observed set.
- **Rollback**: stop ingesting; fields are additive and empty-safe.

### WO-1b — `boat bootstrap` (C1)

- **Ships**: a bare host brought to Active by the binary itself.
- **Preconditions**: WO-1 (adoption, so re-running on a live host is safe).
- **Creates** (boat): `internal/bootstrap/` (port `bootstrap-server.py`),
  `internal/register/` (armed handshake).
- **Modifies** (atlas): `Server.bootstrap()` → drop binary + invoke + await
  registration; `scripts/install.sh` → binary drop + unit install.
- **Contract**: `POST /v1/bootstrap`; registration mirrors
  `central_link.provision_tunnel`/`confirm_tunnel` auto-revert.
- **Does NOT**: remove the SSH channel.
- **Acceptance**: a fresh DO droplet reaches Active with one `boat bootstrap`;
  re-running it on an Active host is a no-op; a deliberately failed registration
  auto-reverts instead of bricking.
- **Rollback**: the existing `bootstrap-server.py` Task path stays until WO-6.

### WO-2 — Full lifecycle + reflexes

- **Ships**: every VM verb through Boat, with the reconciler, the journal, and the
  wake-trap reflex resident in Boat.
- **Preconditions**: WO-1.
- **Flag**: per-VM `observed_authority = Boat`, reversible.
- **Creates** (boat): `internal/reconcile/` (one actor per VM),
  `internal/journal/`, `internal/park/` (port `park.py` + `atlas-wake-trap.py`).
- **Modifies** (atlas): all `virtual_machine.py` lifecycle methods mutate
  `desired_power`/desired-spec instead of calling `run_task`.
- **Contract**: verbs mutate desired state only (§11.3); `desired_power=Stopped`
  outranks a wake-trap.
- **Does NOT**: touch migration, ANCP, or the guest plane.
- **Acceptance**: differential test each verb (Go vs Python) for byte-identical
  host effects; a sleeping VM wakes on an inbound SYN from an off-host vantage;
  a stopped VM does **not** wake on traffic; per-VM authority flips and reverts.
- **Rollback**: flip `observed_authority` back to DB per VM.

### WO-3 — Unit supervision + host-local networking apply

- **Ships**: Boat supervising the unit set, writing `local-ownership.json`, and
  applying reserved-IP NAT + gateway forwarding.
- **Preconditions**: WO-2.
- **Creates** (boat): `internal/units/` (supervision), `internal/netapply/`.
- **Modifies**: `atlas-pool` / `gateway` / `mgmt-firewall` units re-pointed to
  `boat <sub>`; `vm-network-up/down` ownership-cache write moves into Boat.
- **Contract**: `GET|POST /v1/units/{name}`; unit liveness in `GET /v1/host`.
- **Does NOT**: touch ANCP's gossip, membership, or wg peer table.
- **Acceptance**: ANCP still converges with Boat as the cache writer (its own 395
  tests stay green); reserved-IP attach is CAS-gated and returns 409 on a stale
  epoch.
- **Rollback**: re-point units at the Python entry points.

### WO-4 — Cross-host sagas

- **Ships**: migration, warm fan-out, and S3 sync driven over Boat RPCs.
- **Preconditions**: WO-2.
- **Modifies** (atlas): `migration.py` phases → Boat RPCs; `reconcile_migrations`
  unchanged.
- **Contract**: the §8 phase table; **Repoint requires positive source fencing.**
- **Does NOT**: change saga phase persistence or ordering.
- **Acceptance**: a live migration between two DO hosts; a partitioned-migration
  drill proves the fence epoch stops two live copies; S3 sync transfers via a
  presigned URL with Atlas never proxying bytes.
- **Rollback**: per-host flag returns the phase to `run_task`.

### WO-5 — `networkd` in Go

- **Ships**: `boat networkd`, same binary, own unit, byte-identical wg/nft output
  to the Python daemon.
- **Preconditions**: WO-3; the differential harness from WO-2.
- **Creates** (boat): `internal/networkd/` (port the 25 modules).
- **Contract**: wire format, record shapes, and timers **exactly** as
  `spec/31` §7/§13/§14 — this is a port, not a redesign.
- **Does NOT**: change the protocol, the record schema, or any timer default.
- **Acceptance**: the 395 Python networkd tests are mirrored in Go and pass;
  a mixed cluster (Python hosts + Go hosts) converges; wg peer tables are
  byte-identical between implementations on the same input.
- **Rollback**: re-point the unit at the Python module.

### WO-5b — Auto-update (C2)

- **Ships**: Boat updating itself safely under live VMs.
- **Preconditions**: **WO-1 FC re-attach** (hard gate), WO-3 supervision.
- **Creates** (boat): `internal/update/`.
- **Modifies** (atlas): `Server.boat_version` + a staggered rollout driver.
- **Contract**: §5, all seven steps.
- **Does NOT**: update the whole fleet at once; restart firecracker.
- **Acceptance**: an update under a running VM leaves it running and a sleeping
  VM asleep; a deliberately corrupted binary fails the signature check and never
  swaps; a failing health check rolls back to N-1; the fleet driver canaries.
- **Rollback**: pin `boat_version`.

### WO-6 — Verb port completion + cutover

- **Ships**: the remaining ~53 verbs as `boat <verb>`; the venv and durable
  package retired; public-v6 allocation pushed down.
- **Preconditions**: WO-5, WO-5b.
- **Modifies**: `scripts_catalog.py` durable-path helpers; `install.sh`;
  `runner.py` lifecycle path.
- **Does NOT**: delete SSH break-glass; delete `connection_for_guest`.
- **Acceptance**: `run_all_smoke` green with no Python on the host; public-v6
  push-down only after the forward-lease + union law are proven; SSH break-glass
  still restarts a killed Boat.
- **Rollback**: per-host flag.

### Track S — Services de-fusion (parallel, no Boat dependency)

- **Ships**: `atlas/services/` + `Service`/`Service Binding`; `Virtual Machine`
  loses `is_proxy`, `is_gateway`, `build_mode`, `pilot_credential_id`, and
  `terminate()`'s five-way fan-out.
- **Acceptance**: one service moved at a time, green each commit.

---

## Open questions

**All five prior open questions were closed on 2026-07-27** — see the last five
rows of §14. Nothing blocks WO-0.

Remaining risks to watch (not decisions — things to verify as you build):

1. **Native-Go big-bang risk** — mitigated by the §3.5 differential gate, but the
   gate must be honoured per module, not skipped under schedule pressure.
2. **The two-toolchain window** — Go daemon plus Python reference coexist until
   WO-6 completes.
3. **Version skew across two repos** — Atlas must speak `[vN-1, vN]` of the Boat
   API for at least one release window, negotiated on connect.
4. **Dark-VM service reach deferred** (§7.3) — revisit if a private-only service
   VM is ever required.
5. **Shared-tunnel reconnect** — the §14 decision accepts the blip; prove the
   backoff/buffer path in the WO-2 partition drill, not just in review.

## Critical files (reference points for the build)

- `atlas/atlas/_ssh/runner.py:40 run_task()` — the verb/variables/Task-row shape
  `BoatClient` mirrors; `connection_for_guest()` (line 165) **stays**.
- `atlas/atlas/local_task.py:34 run_local_task()` — the existing non-SSH runner;
  the closest analogue to a `BoatClient` call.
- `atlas/atlas/scripts_catalog.py:168 allowed_scripts()` — the verb allow-list to
  enforce at Boat's API boundary.
- `scripts/lib/atlas/_cli.py` — the multi-call dispatcher `boat` replaces.
- `scripts/install.sh:66` — `ln -sfn "${ATLAS_CLI}" /usr/local/bin/atlas`, the
  exact line that becomes the binary drop (`test_install_sh.py` asserts on it).
- `atlas/atlas/api/central_link.py` + `spec/21` — armed auto-revert handshake for
  registration.
- `atlas/atlas/doctype/virtual_machine/virtual_machine.py` — lifecycle methods,
  the freeze/drift guard, and where `desired_power`/`observed_status`/`boot_epoch`
  land; also the `is_proxy`/`is_gateway`/`build_mode` + `terminate()` fan-out.
- `atlas/atlas/networking.py` — `allocate_ipv6` / `address_is_free_on_server` /
  `derive_*`; the union law + forward-lease before allocation moves.
- `atlas/atlas/migration.py` + `spec/24` — the saga template WO-4 re-targets.
- `scripts/reset-server.py` — the enumerators to invert into the adoption scan.
- `scripts/lib/atlas/paths.py:47 VirtualMachinePaths` — deterministic per-UUID
  paths Boat must reproduce identically.
- `scripts/lib/atlas/park.py` + `scripts/atlas-wake-trap.py` — the wake reflex.
- `scripts/lib/atlas/networkd/` + `spec/31` — the WO-5 port source and its spec.
- `scripts/systemd/atlas-networkd.service` — the unit template.
- `scripts/sudoers.d/atlas-tunnel` — the non-root pinned-command model.
- `spec/30-core-service-boundary.md` — superseded as a *deployment*; its
  `Service`/`Service Binding` seam is lifted in-app (Track S).

## Verification

- **Spec-first gate**: §11's six invariants written into `spec/33-boat.md` and
  reviewed before Boat code is written.
- **Per-op differential test on a live DO host**: Go op vs Python reference
  produce byte-identical host effects (LV layout, netns/nft dump,
  `firecracker.json`, identity writes) before that op cuts to native-only.
- **Partition drills (WO-2 and WO-4)**: kill Atlas → Boat keeps VMs running,
  serves wake-traps, self-recovers on reboot; kill a Boat → Atlas marks the host
  Unknown, does not evict, reconciles on reconnect via `GET /v1/export`. A
  partitioned-migration drill proves the fence epoch stops two live copies.
- **Update drill (WO-5b)**: swap the binary under a running VM and a sleeping VM;
  assert both survive, and that a corrupted binary and a failing health check both
  roll back.
- **Reuse the existing e2e harness** (`atlas/tests/e2e/`, one module per use case)
  driving lifecycle through `BoatClient`; `run_all_smoke` against one shared
  bootstrapped host stays green each work order.
- **Security check**: Boat runs non-root; its API rejects any verb outside
  `allowed_scripts()`; token rotation-under-partition and signed-binary install
  are exercised; SSH break-glass restarts a killed Boat.
