# Self-service subdomain routing (bench-admin sites)

A bench VM is a long-lived box where the owner spins up **arbitrary sites** from
inside the guest — the bench-admin UI (`admin/`) or the `bench new-site` CLI. This
chapter makes those guest-created sites routable through the regional proxy, with
**no operator action**, as long as the site is named inside the regional wildcard
(`<label>.<region>.frappe.dev`). Dropping a site stops routing it. Uniqueness
(one subdomain → one VM, fleet-wide) is enforced and surfaced to the bench user at
create time.

> **Relationship to self-serve (14-self-serve.md).** That chapter is the
> **one-site-per-VM, Atlas-driven** flow: Atlas clones the golden, deploys *one*
> site, and inserts *one* [`Subdomain`](./02-doctypes.md#subdomain) itself
> (`Site.auto_provision` step 5). This chapter is the **many-sites-per-VM,
> guest-driven** generalization: Atlas did not run `bench new-site`, so no
> `Subdomain` row exists for the new site. The whole job here is to get a
> `Subdomain` row inserted (and removed) for sites Atlas never created — reusing
> the entire `Subdomain` → proxy engine that already exists.

> **Code is on the spec.** This chapter describes the **push-only** model, and
> [`bench_routing.py`](../atlas/atlas/bench_routing.py) +
> [`atlas-route-client.py`](../bench/atlas-route-client.py) implement exactly it:
> the four whitelisted endpoints (`register` / `deregister` / `check_label` /
> `list`), caller resolution by source `/128`, the per-VM cap, the `Subdomain
> Denylist` + `Bench Routing Audit` DocTypes, and the typed guest client — with **no
> pull, no scheduler entry, and no sweeper**. The old push-triggers-pull hybrid
> (`reconcile_bench_sites`, `route_hint`, `_list_guest_sites`, the hourly scheduler
> entry) was deleted in the convergence. Unit-green on `atlas.tests.local`
> (test_bench_routing, test_bench_routing_guest); the IPv6-origin host facts are
> proven by the self-serve e2e and the `bench_self_routing` manual verifier.

## The shape (one-way push: the guest tells, Atlas writes)

Everything downstream of a `Subdomain` row already works and is proven: its
`after_insert` enqueues a deduplicated regional proxy reconcile, its `on_trash`
deconverges, and `subdomain` is `unique:1` (DB-enforced fleet-wide uniqueness).
See [12-proxy.md](./12-proxy.md) and
[`subdomain.py`](../atlas/atlas/doctype/subdomain/subdomain.py). **No proxy code
changes.** The only new code *creates and removes the row* for a guest-created
site, now also lets the guest **list** its own rows, **arbitrates** every call,
and **audits** every call.

The communication is **one-way, VM → Atlas**, over the guest's own egress. The
guest *tells* the controller what changed; the controller never reads the guest
back. There is **no scheduled SSH pull** — no controller-initiated SSH into the
guest at all:

```
PUSH (the guest's word, VM → Atlas; the controller never SSHes back):
  • register(label)     BEFORE `bench new-site` → the authoritative INSERT that
                                                   RESERVES the name (active=1) — the
                                                   real block-at-create gate
  • deregister(label)   AFTER `bench drop-site`, OR if `bench new-site` FAILS
                                                 → DELETE the caller's own Subdomain
                                                   (idempotent — also the rollback)
  • check_label(label)  OPTIONAL, before register → read-only advisory availability
                                                   answer (UX nicety, never the gate)
  • list()              ON DEMAND                → read-only; the caller VM's own
                                                   Subdomain rows, to find + clear strays

  All FOUR carry NO VM-identifying argument — the controller resolves the calling
  VM from the request source address (Caller resolution). The guest can only ever
  speak as its own box. check_label and list() are read-only; the controller stays
  the sole writer of the fleet-wide-unique Subdomain table.
```

**`register` runs BEFORE `bench new-site`, not after.** Reserving the name first is
what makes the create un-blockable: the authoritative insert atomically claims the
fleet-wide `unique` key, so no one can grab the label out from under a create that's
already underway. If `register` is declined (`taken`/`reserved`/`at_limit`/`invalid`),
the guest **never starts** `bench new-site` — block-at-create by ordering, no orphan.
If `bench new-site` then **fails**, the guest `deregister`s to release the
reservation (the rollback). `check_label` survives only as an *optional, advisory*
pre-flight for early UX feedback — it is no longer the gate, because an advisory
"ok" followed by a create is exactly the window an attacker could block; `register`
closes that window by writing first.

`register`/`deregister` **carry the routing state**: a `register` is the only thing
that ever creates the row, a `deregister` the only thing that removes it. `check_label`
and `list` write nothing. The controller still **arbitrates** every write — it owns
the fleet-wide `unique` key, the per-VM cap (Component G), and the brand denylist
(Component H), so a guest's word is *accepted only if it passes the controller's
rules*. Absent a pull, the guest's message is the **trigger and the content** of
every change; `list` lets the guest *audit* the result without being able to *write*
it. **Every call** — read or write, accepted or rejected — is recorded in the MyISAM
audit log (Component I).

> **What we gave up by dropping the pull, and why it's acceptable here.** The
> earlier design used a scheduled SSH pull as the source of truth, with the guest
> only *triggering* an early re-list. The pull made routing correct even when a
> message was lost — and made teardown independent of guest liveness. Going one-way
> trades that for simplicity and for surviving SSH-key loss (a guest with no inbound
> SSH can still register/deregister/list over its own egress). The two risks the
> pull was guarding are bounded as follows:
>
> - **Hijack of another VM's name** — *closed*, by Caller resolution (a guest writes
>   only routes for its own source `/128`) + the DB unique key (an owned name can't
>   be taken) + the denylist (branded names can't be grabbed). The guest is a
>   *constrained* writer, not a free one.
> - **A lost/withheld `deregister`** — *bounded, accepted residual*. A site dropped on
>   a still-alive VM whose `deregister` never lands stays routed. Because the bench
>   nginx emits a per-site `server_name <fqdn>` vhost with **no `default_server`
>   catch-all** ([`deploy-site.py`](../bench/deploy-site.py): "on-disk name == the
>   Host, so no `default_server` is needed"), the stale route serves a **404, not a
>   co-resident tenant's site** — a dead link, not a cross-site exposure. It is
>   cleaned when the VM is terminated (Component F deletes *all* its rows, no guest
>   cooperation) — **and the owner can self-clear it sooner**: `list()` surfaces the
>   stray (a route with no matching on-disk site), and a `deregister()` per stray
>   removes it without waiting for terminate. We **accept** the dead-link window
>   rather than add a TTL/heartbeat or a scheduled sweeper; it is documented, not
>   hidden.

## Component A — `register` / `deregister` (the guest writes, the controller arbitrates)

`atlas/atlas/bench_routing.py`, both `@frappe.whitelist(allow_guest=True)` +
`@rate_limit`. Each resolves the calling VM from the request source address
(*Caller resolution*) → region (*Component E*), runs its rules, writes if they
pass, and records the outcome in the audit log (*Component I*) on **every** path:

```
register(label)   -> {"status": "ok" | "taken" | "reserved" | "at_limit" | "invalid"}
deregister(label) -> {"status": "ok"}
```

**`register(label)`** — the authoritative insert, run **before** `bench new-site`. It
reserves the name: this is the real block-at-create gate, not `check_label`. It runs
the **same** Contract-A rules `Site` enforces, in the same order, before
writing: `validate_label` (shape) → `validate_reserved` + the brand denylist
(Component H) → the fleet-wide availability (`is_taken` + an existing `Subdomain`) →
the per-VM cap (Component G). On a pass, insert `Subdomain(subdomain=label,
virtual_machine=<resolved vm>, active=1)`; the row's `after_insert` reconciles
the proxy fleet (no extra push). A `DuplicateEntryError` (two benches racing the same
label) maps to `taken` — the DB unique key is the **atomic arbiter**, and reserving
*first* is what makes the subsequent create un-blockable: the name is already claimed
before any work begins. `taken`/`reserved`/`at_limit`/`invalid` insert nothing and
tell the guest why; the guest then never starts `bench new-site` (no orphan, no
rollback). `register` admits exactly one label and never evicts. It is idempotent on
an already-owned label — a retried `register` for the caller's own row is a clean
`ok`, so a re-run after a transient failure is safe.

**`deregister(label)`** — the teardown signal, fired on **two** paths: after a
deliberate `bench drop-site`, *and* as the **rollback** when `bench new-site` fails
after a successful `register` (the reservation is released so a failed create leaves
no stale route). Resolve the VM, find its `Subdomain(subdomain=label,
virtual_machine=<vm>)`, and **delete** it (its `on_trash` deconverges the proxy).
Scoped to the caller's own VM by Caller resolution, so a guest can never deregister
another VM's route. Idempotent: an absent row is a clean `ok` (a double
`bench drop-site`, a replayed POST, a `list()`-driven stray clear, or a rollback for a
`register` that itself failed). It asserts only *its own* teardown — a guest cannot
deregister a label it doesn't own (the row's `virtual_machine` must match the
resolved VM, else no-op).

Both are **arbitrated, not trusted**: the guest supplies a label and an intent, but
every rule that protects the fleet (uniqueness, reserved, denylist, cap, own-VM
scoping) is applied controller-side. The guest's word can create/remove only what
the rules allow, only for itself. **Both update the proxy immediately** through the
existing `Subdomain` hooks — `register`'s `after_insert` and `deregister`'s `on_trash`
each enqueue the regional reconcile inline, so a route appears or disappears the
moment the write lands, with no second push and no pull to wait on.

> **A rejected write is still arbitrated by a trustworthy source — *if* the edge
> holds.** Both endpoints resolve the VM from `frappe.local.request_ip` (*Caller
> resolution*), which is only the real peer `/128` behind a trusted edge that
> overwrites `X-Forwarded-For`. **That production edge is not yet built and is a
> hard prerequisite** (see *Caller resolution*); below it, a forged XFF lets a guest
> register/deregister *another* VM's routes — a hijack, not a nuisance. The audit
> log (Component I) is how such an attempt is detected after the fact.

## Component B — `check_label` (the optional advisory pre-flight)

`atlas/atlas/bench_routing.py`, `@frappe.whitelist(allow_guest=True)` +
`@rate_limit`. **Read-only**, and **no longer the gate** — `register` is
(*Component A*). `check_label` is an *optional* courtesy the guest may call to give
the user early "that name's taken" feedback before committing to a `register`:

```
check_label(label) -> {"status": "ok" | "taken" | "reserved" | "at_limit" | "invalid",
                        "suffix": "<region domain>"}
```

It runs the same checks `register` will (`validate_label`, `validate_reserved` +
denylist, `is_taken`, the per-VM cap against the **source-resolved** VM) and returns
the active region's domain (*Component E*) so the guest can build the FQDN without
carrying it. It carries **no VM-identifying argument** — the cap check is always
against the caller's own VM (*Caller resolution*). It writes nothing but **is
audited** (status `ok`/`taken`/…, *Component I*).

`check_label` is **advisory and fail-open**, and that is *why* it can't be the gate:
a wrong/stale "ok" here, acted on by starting a create and only then registering,
is exactly the window an attacker could use to grab the name first. The
authoritative, race-free decision is `register`'s atomic insert (*Component A*) —
`check_label` only saves the user a doomed create when the answer is already "taken".
A malformed label is returned as a clean `{"status": "invalid", "reason": "<message>"}`
(not a 417) so the guest hook can surface the operator's message verbatim without
parsing an HTTP error body.

## Component C — `list` (the guest reads its OWN routes to find strays)

`atlas/atlas/bench_routing.py`, `@frappe.whitelist(allow_guest=True)` +
`@rate_limit`. **Read-only**, takes **no argument** — the VM is the source
address (*Caller resolution*), never a parameter:

```
list() -> {"domains": [{"label":  "<label>",
                        "fqdn":   "<label>.<region domain>",   # built controller-side
                        "active": true | false}, ...]}     # [] for a VM with no rows
```

Returns **all** the `Subdomain` rows where `virtual_machine ==` the source-resolved
VM. `fqdn` is reconstructed controller-side as `f"{label}.{region_domain}"`
(region/domain from the active Root Domain, *Component E*; never echoed from a guest
suffix); `active` reflects the row's flag (in this push-only model `register` always
inserts `active=1` and `deregister` deletes, so a route is either active or gone —
the field is surfaced for completeness and forward-compat, not because this chapter
deactivates rows). `list` is read-only: it writes nothing and **does not touch the
cap** (Component G counts on a *write*; enumerating consumes nothing). It is audited
like every call (*Component I*).

**Purpose — the guest's self-service stray finder.** The owner enumerates its
own routes and compares them against its on-disk `sites/`. A `Subdomain` with
**no matching on-disk site** is a *stray*: a lost `deregister` (Component D's
best-effort POST never landed), or a site dropped while the controller was
unreachable. The guest then calls `deregister(label)` on each stray. This is
the on-demand complement to the accepted-residual dead-link window (*The shape*):
because there is no scheduled sweeper, `list` + `deregister` is how a stray on a
still-running VM gets cleared before the VM is terminated (Component F).

**Why read-only, and why the guest still drives each delete.** The controller
stays the **sole writer**: `list` asserts nothing and writes nothing, so the guest
gets a *view*, never a lever. The guest issues a **per-stray** `deregister`, each one
arbitrated and individually audited (own-VM scoping, idempotent). We **deliberately
do not** expose a converge-style "here is my whole set, delete the rest" endpoint:
that would re-introduce a guest-driven reconcile (the exact pull-shaped coupling
this chapter removed) and let one malformed guest set **mass-delete** its own routes
in a single call. Per-stray `deregister` keeps every delete an explicit, individual,
separately-audited act.

A source matching no VM / a Terminated VM / a proxy is a **clean reject** — the same
Caller-resolution gate the write endpoints use; such a caller can't legitimately own
bench sites. The reject is a `frappe.throw` (no listing), distinct from the typed
`{"status": …}` results `check_label`/`register` return; an empty inventory is the
typed `{"domains": []}`, never a throw.

> **`list` is read-scoped only behind the trusted edge — not unconditionally.**
> `list` returns only `virtual_machine == <source-resolved VM>` rows, so behind a
> trusted edge a guest can list **only its own box**. But Caller resolution is by
> `frappe.local.request_ip`, so under a **broken edge** (the documented hard
> prerequisite, *Caller resolution*) a forged `X-Forwarded-For` resolves `list()` to
> a *victim* VM and leaks that VM's route inventory — `list` is a read-hijack below a
> broken edge, the same trust dependency as the writes, never harmless.

> **What `list` closes, and what it does not.** `list` + `deregister` lets the
> *owner* clear a stray proactively, shrinking the dead-link window without an
> operator. But it is still **guest-initiated**: a VM that never calls `list`
> (or has lost egress) still depends on **terminate** (Component F) for cleanup —
> that is the only controller-side teardown, and it is total (every row for the VM
> goes when the VM goes). `list` is a **convenience, not a new safety guarantee**.

## Caller resolution (the VM is the source address, never a parameter)

All four endpoints derive *which VM is calling* from the request's **public IPv6
source `/128`**, matched against `Virtual Machine.ipv6_address` — never from a
request parameter. A guest is root in its own VM and can read any value we inject
(`/etc/atlas-vm-uuid`, any secret), so a guest-supplied `vm_uuid` could name
*another* VM. Resolving from the source address means a guest can only ever speak
**as the box its packets come from** — it cannot register/deregister/list/probe
another tenant's VM. This scoping is what makes a *writing* one-way push tolerable:
the guest is a writer, but only of its own routes.

- No injected secret is involved. A secret written into the guest authenticates
  "the VM" to a tenant who *is* root in that VM, so it identifies nothing the source
  address doesn't already (and a shared per-region secret identifies only the
  region) — the source `/128` is the one VM-identifying fact the tenant cannot forge
  *if* it is read from a trusted hop (below). `/etc/atlas-routing.env` carries
  **only** the base URL, no token (see *Identity*).
- A spoofed/non-matching source is a clean reject (`frappe.throw`): no VM resolves,
  so no write happens (and `list` returns no inventory). Resolution to a Terminated
  VM or a proxy is likewise rejected — those can't legitimately own bench sites. The
  rejected attempt is still **audited** with the source `/128` that tried (*Component
  I*) — a non-resolving source is exactly the forensic signal worth keeping.
- **The resolver filters Terminated/proxy in the query and fails closed on a duplicate
  `/128`.** `ipv6_address` is **not** a unique column, and `allocate_ipv6` can recycle a
  Terminated VM's `/128` onto a fresh Running VM (the reuse guard is deferred,
  [09-roadmap.md](./09-roadmap.md)). So resolution selects only `status != Terminated`
  **and** `is_proxy = 0` rows — a stale Terminated row carrying the recycled address can
  never *shadow* the live owner — and if two *live* non-proxy VMs ever share a `/128` it
  resolves **neither** (a write under either would be wrong), rather than trusting an
  arbitrary first row. This is a logic backstop *behind* the trust root, not a substitute
  for it: the edge + host anti-spoof (below) are still what make the source `/128` honest.

**Where the source address comes from (the trust root — and a real gap).** The
guest does **not** reach the controller's Frappe worker directly; it traverses a hop
(ngrok in local dev; an edge/LB in production). So `request.remote_addr` at the
worker is the *hop's* address, not the VM's `/128` — useless for resolution. The VM's
real `/128` survives only in **`X-Forwarded-For`**, which Frappe exposes as
`frappe.local.request_ip`. **Two traps make `request_ip` untrustworthy by default —
and here it gates a *write*, so this is load-bearing, not cosmetic:**

1. Frappe's [`set_request_ip`](../../frappe/frappe/auth.py) trusts `X-Forwarded-For`
   **unconditionally** and takes its **leftmost** comma value — it never checks that a
   trusted hop set it. A guest can send `X-Forwarded-For: <victim-/128>`; because the
   worker reads the leftmost value, the guest's forged entry **wins over** anything
   the real edge appended. Used naively, `request_ip` is attacker-settable — and in
   the one-way model that means a guest could **register/deregister/list another VM's
   routes**. This is the single most dangerous failure mode in the design.
2. ngrok (and many proxies) **append** to XFF rather than overwrite it, so the genuine
   client IP sits *after* the guest's forged one — the leftmost-wins parse picks the
   forgery.

**Therefore caller resolution requires a trusted edge that *strips any
client-supplied `X-Forwarded-For` and sets it to the real peer `/128`*** before the
request reaches the worker (and the worker must read the edge-guaranteed value, not a
raw leftmost-XFF). The edge is the trust root of the whole feature.

> **The rate limiter shares the trust root's fate.** `@rate_limit(ip_based=True)`
> keys on the **same** `frappe.local.request_ip` caller resolution reads
> ([`rate_limiter.py`](../../frappe/frappe/rate_limiter.py)). So below a broken edge a
> forged XFF defeats **both**: per-VM scoping (a guest acts as a victim) *and* the
> rate limit (a guest spreads its quota across forged source IPs). The edge isn't one
> of two independent controls — it is the single hinge both swing on. Verify it
> before trusting either.

- **Production edge: not yet built.** The controller's base URL is just
  `frappe.utils.get_url()`; nothing today sits in front of the controller that
  overwrites XFF (the spec/12 regional proxy fronts *tenant* traffic, not the Atlas
  controller). Standing up / configuring that edge is a **hard prerequisite** of this
  model — without it, the write endpoints are spoofable and `list` is a read-hijack.
- **Local dev: ngrok, with the append trap.** ngrok must be configured so the worker
  keys off the value ngrok sets (its real-client header), not a guest-prepended XFF —
  otherwise dev "works" while being trivially spoofable, hiding the prod gap.
- **Still load-bearing below the edge:** even with a trusted edge, the routed-tap host
  must not let VM-Y emit packets with VM-X's v6 source (anti-spoofing / RPF), or the
  edge faithfully records a spoofed peer. Both the **edge XFF-overwrite** and the
  **host anti-spoof** must be **verified**, not assumed — together they are the trust
  root of caller resolution, and because resolution now gates a write, a failure is a
  hijack, not a nuisance.

## Component D — the bench-cli hook (guest side, thin, strictly typed)

A small stdlib-only client committed in the `bench/` tree as
[`bench/atlas-route-client.py`](../bench/atlas-route-client.py), installed into the
golden by [`bench/build.sh`](../bench/build.sh) at `/usr/local/bin/atlas-route` (so
it is present on every clone). It reads only the Atlas base URL from
`/etc/atlas-routing.env` (see *Identity*) and POSTs the whitelisted endpoints with
**no VM-identifying argument** — the controller resolves the calling VM from the
source address. It is wired at the two choke points **both** the admin UI and the CLI
flow through — bench-cli's `new_site` and `drop_site` — so a site created either way
is covered (hooking the admin Flask callbacks alone would miss CLI-created sites).

### The POST must go over IPv6 (the origin is the validation)

Caller resolution matches the request's **source `/128`** against
`Virtual Machine.ipv6_address` — so the client **must** reach the controller over
**IPv6**, or the controller has no VM-identifying address to resolve. Two reasons
this is a hard requirement, not a preference:

- **Origin validation depends on it.** The whole security model is "the VM is the
  box its v6 packets come from." A POST over IPv4 arrives NAT'd (the host's shared
  v4, or a proxy's) — there is no per-VM v4 `/128` to key on, so the controller
  cannot tell which VM called. The client forces the connection to the v6 address
  family (resolve the base-URL host to its `AAAA`, connect over v6; fail loudly if
  the controller has no v6 address rather than silently falling back to v4).
- **Dev reachability.** In developer mode the controller's IPv4 may not be routable
  from inside the guest at all (the v6 path is the one that works), so v4 isn't even
  a degraded fallback — it's a dead connection.

The client therefore pins the request to IPv6 (an `AF_INET6`-only connector) and
treats "no v6 route to the controller" as a transport error on the same fail-open /
best-effort rules below — never as a v4 retry.

### Strictly-typed results and errors (so bench consumes them cleanly)

The client exposes a **typed** Python surface, not bare dicts/exit codes, so the
bench-cli wiring matches on classes the type checker knows. Every call returns one of
a small closed set of result types, and every failure is a typed exception:

```
# Results (one per logical outcome — bench matches on the type, not a string):
@dataclass(frozen=True) class Available:    suffix: str               # check_label ok
@dataclass(frozen=True) class Registered:   label: str; fqdn: str     # register ok
@dataclass(frozen=True) class Deregistered: label: str                # deregister ok
@dataclass(frozen=True) class Listing:       domains: list[Route]      # list ok
@dataclass(frozen=True) class Route:        label: str; fqdn: str; active: bool

# A declined write/check is a typed value too (NOT an exception — it's an expected
# business outcome the caller branches on):
@dataclass(frozen=True) class Declined:
    reason: Reason            # enum: TAKEN | RESERVED | AT_LIMIT | INVALID
    message: str              # the operator-facing text, verbatim from the controller

# Failures are typed exceptions (the caller decides fatal vs best-effort):
class RoutingError(Exception): ...                 # base
class NotConfigured(RoutingError): ...             # /etc/atlas-routing.env absent — no-op signal
class TransportError(RoutingError): ...            # unreachable / no v6 route / timeout / bad JSON
```

`Reason` is a closed enum mirroring the controller's status strings exactly
(`taken`/`reserved`/`at_limit`/`invalid`), so a new status can't slip through as an
untyped string — an unknown status from the wire is a `TransportError`, not a silent
pass. `bench` imports these types and branches on `isinstance` (or pattern-matches),
so the consumer never parses a status string or an exit code by hand.

### Subcommands (register reserves FIRST; deregister is also the rollback)

- `atlas-route register <label>` — **before** `bench new-site` runs. POSTs
  `register(label)` and returns `Registered` on success or `Declined` on
  `taken`/`reserved`/`at_limit`/`invalid`. On `Declined` the bench-cli command
  **stops** — the local site is never created, so there is **no orphan**
  (block-at-create by ordering). This is the authoritative reservation; reserving
  before the create is what makes the create un-blockable. Idempotent on the caller's
  own label, so a retry after a transient `TransportError` is safe.
- `atlas-route deregister <label>` — fired on **two** paths: after `bench drop-site`,
  **and as the rollback when `bench new-site` fails** after a successful `register`.
  POSTs `deregister(label)`, best-effort, returns `Deregistered`. A lost `deregister`
  on the drop path leaves a stale (404-serving) route until the owner clears it via
  `list` (below) or the VM is terminated (the accepted residual, *The shape*).
- `atlas-route check-label <label>` — **optional**, before `register`, for early UX
  feedback only. Returns `Available` or `Declined`. It is *not* the gate (`register`
  is); a `Declined` here just spares the user a doomed `register`. A name that isn't
  `<label>.<region domain>` (the suffix the result carries) is the user's choice to
  run local-only and is simply never registered; the wrapper skips it.
- `atlas-route list` — **on demand** (a maintenance subcommand, not in the create/drop
  hot path). POSTs `list()`, returns `Listing`, compares the returned `Route` labels
  against the bench's on-disk `sites/` directory, and for every routed label with **no
  matching on-disk site** (a stray) prints it and issues a `deregister(label)`. The
  owner's self-service reconcile — entirely guest-initiated, per-stray, never a bulk
  converge.

The client **raises `NotConfigured` → no-ops cleanly** (routing skipped) when
`/etc/atlas-routing.env` is absent — so an ordinary (non-Atlas) bench is unaffected.
The bench-cli `new_site`/`drop_site` integration is a thin call into `atlas-route`,
gated on the binary being present; that wiring lives in the bench-cli repo (the moving
dependency `build.sh` pins), and the contract it depends on is this client's **typed
surface** — the result/exception classes above, not stringly-typed status codes.

## Component E — region (controller-resolved, not VM-asserted)

No VM — site or proxy — carries a `region` field; Atlas is single-region and the
one stored region is `Atlas Settings.region`. We **don't** make a VM-carried region
a source of truth (it would drift and misroute). Region is resolved
**controller-side**:

- `register`/`deregister`/`check_label`/`list` build the FQDN from the single active
  [Root Domain](./02-doctypes.md#root-domain)'s domain suffix
  (`active_root_domain().domain`, [`placement.py`](../atlas/atlas/placement.py)) the
  same way [`Site`](../atlas/atlas/doctype/site/site.py) does. Single-region today;
  resolution stays controller-side, never parsed from a guest FQDN.
- `check_label` returns the region **domain** so the guest can name its site
  correctly; `list` returns each row's **FQDN** built from it; the guest never
  *claims* a region.

The label-to-FQDN reconstruct rule still applies on every `register`: the controller
validates `label` and stores it; the served FQDN is `f"{label}.{region_domain}"`,
built controller-side, never from a guest-supplied suffix.

## Component F — controller-side teardown (terminate deletes everything)

The **only** controller-side teardown is `VirtualMachine.terminate()`, and it is
total — so no scheduled sweeper is needed:

**`VirtualMachine.terminate()`** calls `_delete_subdomains()` beside
`_detach_reserved_ip()` / `_delete_snapshots()`
([`virtual_machine.py`](../atlas/atlas/doctype/virtual_machine/virtual_machine.py)):
delete every `Subdomain` where `virtual_machine == self.name`. **Already built.** When
a VM dies, *all* its routes die with it, with no guest cooperation — each delete's
`on_trash` deconverges the proxy. Because terminate removes the rows that point at a
`/128` *before* that address can be recycled (`allocate_ipv6` only re-hands an address
a terminated VM has released), there is **no surviving row to drift onto a new
tenant** — which is exactly why the old address-drift sweeper is gone.

> **Why no sweeper.** The earlier design ran an hourly `sweep_stale_subdomains` to
> catch (a) rows of VMs killed out-of-band and (b) a route whose VM's address drifted
> onto a recycled `/128`. Case (b) is closed structurally: `terminate()` deletes a
> VM's rows as part of the same teardown that releases its address, so a row never
> outlives its VM's `/128`. Case (a) — a VM removed *without* going through
> `terminate()` — is an Atlas-internal invariant to uphold (every VM removal goes
> through `terminate()`), not a routing concern to paper over with a periodic scan.
> The remaining residual — a stray route on a *still-running* VM whose `deregister`
> was lost — is **not** a leak (it 404s, no `default_server`, *The shape*) and is
> cleared by the owner via `list` + `deregister` (Component C) or by eventual
> terminate. We delete correctly on terminate instead of scanning to repair; the
> `allocate_ipv6` reuse guard ([09-roadmap.md](./09-roadmap.md)) is the belt-and-
> suspenders follow-up.

## Component G — the per-VM subdomain cap (namespace-exhaustion control)

A bench owner can `bench new-site` arbitrarily many sites; without a ceiling one
tenant occupies an unbounded slice of the region's namespace (and bloats the proxy
map). The DB unique key blocks hijacking an *owned* name and Component H blocks
*branded* names; the cap blocks *bulk* squatting of unowned names and bounds blast
radius per VM.

**The cap is a simple memory tier — start at 20, double a step at a time as the VM
gets bigger.** No floor/multiply formula; just a small lookup keyed on
`memory_megabytes`, so a `resize()` re-prices it for free:

```
cap(vm):
   ≤  8 GB → 20      # the base — every size in sizes.py today sits here
     16 GB → 40
     32 GB → 80
     ≥ 64 GB → 160
```

20 is the **base**, not a pinch: every size in [`sizes.py`](../atlas/atlas/sizes.py)
today (≤ 8 GB) gets 20. Each tier up doubles — bigger VMs serve more sites. Adding a
size means adding one row to the table, nothing to recompute.

**Enforced authoritatively in `register`** (and mirrored advisorily in `check_label`):
count the resolved VM's `Subdomain` rows; at or above `cap(vm)`, `register` returns
`at_limit` and inserts nothing. Because each `register` admits exactly one label and
never evicts an existing route, the cap is a simple ceiling: the sites already routed
stay routed, and the (N+1)th create is refused at write time. (There is no
set-convergence step — each label arrives as its own `register`.)

## Component H — the brand/keyword denylist (a DocType, editable live)

`RESERVED_SUBDOMAINS` blocks structural labels (`www`, `api`, …) and is frozen in
code. The **brand denylist** is the complement the unique key and cap don't cover: a
tenant grabbing `paypal`/`stripe`/`login`/`account`/… under the valid wildcard TLS
cert — phishing-as-a-service on a name no other VM holds yet. Unlike the structural
reserved set, the brand list **changes over time** (a new payment brand, an abused
keyword the operator spots in the audit log), so it lives in a **DocType**, not a
code constant or a settings textarea:

```
Subdomain Denylist  (engine: InnoDB; one row per blocked label)
  label    Data   autoname: field:label, unique:1 — the blocked label (lowercased)
  reason   Data   why it's blocked (operator note: "payment brand", "auth keyword", …)
  enabled  Check  default 1 — flip off to lift a block without losing the row/reason
```

An operator adds a row to block a name and the next `register`/`check_label` honors it
**immediately** — no deploy, no migrate. Enforcement is in the same `validate_reserved`
seam, so both `check_label` and `register` reject a denylisted label (told at create
time; never written). The check is a single indexed `exists("Subdomain Denylist",
{"label": <lowercased>, "enabled": 1})`, cheap enough to run inline on every call.
Seeded at install with the obvious payment/auth/brand terms; the operator curates it
from there — often straight from a hijack-attempt row in the audit log (*Component I*).

## Component I — the request audit log (MyISAM)

Every call to the four endpoints (`check_label`, `register`, `deregister`, `list`)
writes one row to a new append-only DocType, **`Bench Routing Audit`**, with
`"engine": "MyISAM"`. It is the forensic backbone of the trust-root story: when the
edge / host anti-spoof is the load-bearing risk (*Caller resolution*), this log is
**how you detect a hijack attempt** — a `register` whose source `/128` resolved to
VM-X while a forged `X-Forwarded-For` in the forwarded-header chain named VM-Y leaves
a row with *both* facts side by side.

**Why MyISAM, when every other Atlas DocType is InnoDB.** An audit log is
append-only, write-heavy, and **never participates in the controller's transactional
writes** — and that is the point, not a performance tweak. A MyISAM insert is **not
rolled back** when the surrounding request transaction rolls back. A *rejected*
`register` calls `frappe.throw` (or returns a non-`ok` status after a `is_taken`
check), and a non-resolving source `frappe.throw`s in caller resolution — both unwind
the request transaction; on InnoDB the audit insert would unwind with it and we would
**lose the record of exactly the attempts most worth auditing** — the rejected /
hijack-attempt ones. MyISAM's auto-committed table survives the throw, so the reject
is recorded.

> **The MyISAM insert survives the caller's rollback — that is the whole reason.**
> The endpoint writes the audit row via the helper, and a later `frappe.throw` rolls
> back its *own* transaction; the MyISAM row persists because the engine auto-commits
> per statement and ignores the InnoDB transaction. So persistence rides **MyISAM's
> auto-commit alone** — the helper does **not** call `frappe.db.commit()`, because an
> explicit commit would also flush any partial transactional work done before the
> throw (defeating the rollback the reject relies on). One mechanism, not "commit
> and/or MyISAM": the table engine *is* the durability. The honest cost: MyISAM gives
> **no crash-safe recovery and no FK integrity** — acceptable for an append-only
> forensic log that references nothing transactionally and must outlive the rows it
> describes. (Verify at migrate that the table is created `ENGINE=MyISAM` and not
> silently coerced to InnoDB by the deployment's MariaDB config — the whole argument
> rests on the engine actually being MyISAM.)

```
Bench Routing Audit  (engine: MyISAM, append-only, sole writer = _audit())
  endpoint     Data   check_label | register | deregister | list
  label        Data   the label argument; BLANK for list()
  status       Data   ok | taken | reserved | at_limit | invalid | unresolved
                      (the SAME values an endpoint returns/throws — no synthetic codes;
                       "unresolved" = caller resolution found no VM, i.e. a spoof attempt)
  business_reject Check  1 = a rules decline (taken/reserved/at_limit/invalid) or an
                         unresolved source; 0 = a clean ok. (A @rate_limit throttle is
                         NOT a row here — see the note below.)
  vm           Data   resolved VM name — a Data SNAPSHOT, not a Link: an audit row must
                      survive the VM's deletion (a Link would dangle/cascade), and a
                      spoof attempt resolves to NO vm at all (blank vm + a source_ip)
  source_ip    Data   the /128 caller resolution KEYED ON (frappe.local.request_ip) —
                      the exact value the trust decision used; recorded even when it
                      resolved to no VM, so the spoofer's /128 is captured
  fwd_headers  Long Text  the forwarded-header chain (incl. the raw X-Forwarded-For)
                      stored VERBATIM — guest-controlled bytes
  request_body Long Text  the raw POST body, guest-controlled, stored VERBATIM
  creation     (built-in)  timestamp — Frappe's own; no extra field
```

`vm` is **`Data`, not `Link`** deliberately: a `Link` would either dangle or
cascade-delete when the VM is terminated, destroying the record precisely when it
matters, and a spoof attempt resolves to *no* VM — there is nothing to link. The
snapshot keeps the row self-contained and immortal.

> **`source_ip` ≠ `fwd_headers`, and the difference *is* the hijack signal.**
> `source_ip` is the single value caller resolution acted on (`frappe.local.request_ip`,
> the leftmost-XFF the worker trusted); `fwd_headers` is the *whole* forwarded chain
> verbatim. Behind a correct edge they agree. When they **disagree** — `source_ip` is
> a clean edge-supplied peer but `fwd_headers` shows a guest-prepended
> `X-Forwarded-For: <some-other-/128>` — that is a guest *attempting* the leftmost-XFF
> forgery, recorded in full. Storing both is what turns "the edge is load-bearing"
> into "and here is the log that proves whether it held."

**Where it's written.** A single helper `_audit(endpoint, label, status, *,
business_reject, vm, source_ip, fwd_headers, request_body)` is called on **every path
of every endpoint, including the reject/throw paths** (audit-before-throw). Reads
(`check_label`, `list`) audit with `status=ok` (or `invalid`/`unresolved`) and
`business_reject` set accordingly; writes audit their `ok`/`taken`/`reserved`/
`at_limit`/`invalid` outcome. The non-transactional MyISAM table is what makes the
reject rows land despite the surrounding rollback (the blockquote above).

> **A `@rate_limit` throttle is *not* in this table — and that's honest.** The
> `@rate_limit` decorator raises *before* the endpoint body runs, so `_audit()` (which
> runs inside the body) never executes for a throttled request. The audit log records
> **business decisions** (resolved/declined/listed), not transport throttling; a
> throttle surfaces as Frappe's own 429 + rate-limiter logs, not a `Bench Routing
> Audit` row. We do **not** claim the table distinguishes a throttle from a decline —
> it simply never sees the throttle. (A future enhancement could audit throttles from
> the decorator seam; out of scope for v1.)

**Retention.** The table grows **unbounded** — one row per request, forever, and it
stores guest-controlled `fwd_headers`/`request_body` verbatim (a size/PII caution for
any future export). A prune (a deferred sweep or a fixed retention window) is
**wanted but out of scope for v1**; named here, not built.

## Identity injected into the guest

The **only** thing routing injects is the Atlas base URL, to `/etc/atlas-routing.env`
(`0644 root:root`) — the guest needs somewhere to POST and nothing else. It carries
**no VM UUID and no token**: caller resolution is by source address, so the guest
never sends a VM-identifying value, and there is no secret to ride MMDS (which is
unauthenticated plain HTTP any tenant SSRF can read).

- **Cold provision** — [`rootfs.inject_identity`](../scripts/lib/atlas/rootfs.py)
  writes `/etc/atlas-routing.env` (the base URL) while the rootfs is mounted,
  alongside `authorized_keys` and the network env. The base URL rides a new optional
  `routing_base_url` field on `Identity`, threaded from a `ROUTING_BASE_URL` Task var
  ([`provision-vm.py`](../scripts/provision-vm.py)) the controller sets to
  `frappe.utils.get_url()` — **the FQDN of the trusted edge**, so the guest's POSTs
  traverse the hop that overwrites XFF (*Caller resolution*).
- **Warm clone** — the disk is never mounted, so the base URL rides MMDS:
  `_mmds_metadata` adds `routing_base_url`, and the in-guest
  [`atlas-warm-freshen.py`](../bench/atlas-warm-freshen.py) writes the env file when
  it adopts a clone's identity.

> **`/etc/atlas-vm-uuid` is not a routing dependency.** Caller resolution is by source
> address, so routing needs neither a cold-path UUID injection nor a `vm_uuid` field
> in the MMDS payload. `/etc/atlas-vm-uuid` remains only the warm-freshen
> adopted-identity marker (`warm.sh` writes it on the golden, the freshen unit adopts
> it) — untouched by this chapter.

## Why this is simple, and where the risk lives

- **Simple** — one-way push reuses the whole `Subdomain` → proxy engine (hooks,
  `proxy.py`, the unique key). The new code is two write endpoints + two read
  endpoints (`check_label`, `list`) + the audit log + the denylist DocType + a thin
  typed guest client. **No SSH pull job, no scheduled sweeper, no TTL/heartbeat
  machinery, no token lifecycle, no MMDS secret.** Teardown is one place —
  `terminate()` — and the guest can self-clear strays via `list`. A guest with no
  inbound SSH still routes its sites.
- **Where the risk concentrates** — the trust root is **Caller resolution**, because
  it gates a *write* (and a same-fate read in `list`, and the rate limiter). If the
  edge fails to overwrite XFF (or the host lets a VM spoof another's v6 source), a
  guest can register/deregister/list another VM's routes — a hijack, not a nuisance.
  The IPv6-only client (Component D) is load-bearing here: a v4 POST has no per-VM
  source to resolve. This is the one property that must be verified on a host before
  this ships; everything else degrades gracefully. The audit log (Component I) is how
  a failure is *detected*, not prevented — the edge is the prevention.
- **Accepted residual** — a lost `deregister` on a still-running VM leaves a
  404-serving stale route until the owner clears it (`list` + `deregister`) or the VM
  is terminated. No default_server means it can't serve a co-resident tenant's site;
  terminate deletes every row before its `/128` can be recycled, so the route never
  drifts onto a new tenant. We document it rather than add a sweeper or heartbeat.
- **Debuggable** — every write is a `Subdomain` row change with its own proxy
  reconcile, and every call is a `Bench Routing Audit` row; a routing failure is "did
  the `register` POST arrive and pass the rules" (one audit entry), and the
  controller's own state is the whole truth.

## Deferred (out of scope for v1)

- A per-region shared secret on the endpoints (Caller resolution by source address +
  rate-limit are the v1 controls).
- The `allocate_ipv6` reuse guard (v1 relies on `terminate()` deleting a VM's rows
  before its `/128` is released — belt-and-suspenders only).
- A TTL + guest keepalive heartbeat to expire stale routes on a still-running VM
  (v1 accepts the 404-serving dead-link window, narrowable by the owner via
  `list` + `deregister` — *The shape* / Component C).
- A scheduled sweeper (v1 has none — `terminate()` is the only controller-side
  teardown, and it is total; *Component F*).
- A "management access lost" / liveness signal per VM (one-way push has no pull whose
  failure would surface key loss; revisit if operators need it).
- Auditing `@rate_limit` throttles (the v1 audit log records business decisions, not
  transport throttles — Component I).
- A retention prune for `Bench Routing Audit` (it grows unbounded in v1 — Component I).
- Multi-region cross-region suffix hardening (single-region today; the
  reconstruct-and-compare rule is specified now so it's correct when a second region
  lands).

## Testing

- **Unit (milliseconds):**
  - `check_label` status mapping (ok/taken/reserved/invalid/at_limit) over
    `subdomain_label`'s rules + denylist + cap; the suffix matches the active Root
    Domain.
  - **Caller resolution:** all four endpoints resolve the VM from the edge-supplied
    source `/128` (`frappe.local.request_ip`) against `Virtual Machine.ipv6_address`,
    take **no** `vm_uuid` parameter (a body param is ignored), and reject a source
    that matches no VM / a Terminated VM / a proxy with **no write** (and, for `list`,
    no inventory). The leftmost-XFF forgery (a guest-supplied `X-Forwarded-For`) must
    NOT resolve to the named victim under the trusted-edge contract — the regression
    test for the one-way model's worst failure. (The edge XFF-overwrite and host
    anti-spoof are e2e/host facts; the unit boundary is "given this `request_ip`, the
    right VM or a reject.")
  - `register` (the reserve-first gate): passes the full rule chain in order and
    inserts `active=1` on ok; returns `taken` on an owned label and on a
    `DuplicateEntryError` race (the atomic-reservation regression — two benches racing
    the same label, one wins the unique key, the other gets `taken`); `reserved`
    /denylist; `at_limit` at cap; `invalid` on a bad label; the inserted row's
    `virtual_machine` is the **source-resolved** VM, never a param. Idempotent
    re-register of an already-owned label is a clean `ok` (the retry-after-transient
    case).
  - `deregister` (drop **and** create-failure rollback): deletes only the caller's own
    `Subdomain` for the label (a row owned by another VM is a no-op), is idempotent on
    an absent row (a rollback for a `register` that itself failed, a replayed POST, a
    double drop), and fires the proxy reconcile via `on_trash`.
  - `list`: returns only the caller VM's own rows, each with a controller-built `fqdn`;
    an empty inventory is `{"domains": []}`; a non-resolving / Terminated / proxy
    source is a clean `throw` (no inventory); it writes nothing and does not affect the
    cap.
  - **Component G (cap):** the tier lookup (≤8 GB → 20; 16 GB → 40; 32 GB → 80; a
    resize re-prices it); `register` admits up to cap then `at_limit`-refuses, never
    evicts an existing row.
  - **Component H (denylist DocType):** a `Subdomain Denylist` row with `enabled=1`
    is rejected by both `check_label` and `register` in the `validate_reserved` seam;
    flipping `enabled=0` lifts the block immediately (no migrate); a row added at
    runtime is honored on the next call.
  - **Component I (audit):** every endpoint writes one `Bench Routing Audit` row on
    **both** the ok and the reject path; the doctype is `engine: MyISAM`; the row
    survives a request rollback (a rejected `register` that `throw`s still leaves its
    audit row — the InnoDB-would-lose-it regression); a non-resolving source records
    `vm` blank + the spoofing `source_ip`; `source_ip` is the value resolution used
    and `fwd_headers` holds the raw XFF chain (the two can differ); the helper does
    **not** call `frappe.db.commit()`.
  - **Component F:** `VirtualMachine.terminate()` deletes **every** `Subdomain` for
    the VM (each `on_trash` reconciles the proxy); there is **no** sweeper to test
    (assert the scheduler carries no `sweep_stale_subdomains`/`reconcile_*` entry).
  - The guest client's **typed** contract: `register` returns `Registered` on ok and
    `Declined(reason=…)` on `taken`/`reserved`/`invalid`/`at_limit` (the caller aborts
    the create on `Declined`, before `bench new-site` runs); a missing
    `/etc/atlas-routing.env` raises `NotConfigured` (the no-op signal); an
    unreachable controller / no-v6-route raises `TransportError`; an unknown wire
    status is a `TransportError`, never a silent pass; `deregister` is best-effort
    non-fatal and is the create-failure rollback; `list` returns `Listing`, diffs
    against on-disk `sites/`, and deregisters each stray (per-stray, never bulk).
  - **IPv6-only transport (Component D):** the client connects over `AF_INET6` only;
    given a controller host with no `AAAA` (or no v6 route) it raises `TransportError`
    rather than falling back to IPv4 — the unit boundary is "the connector refuses v4",
    the actual v6 reachability is a host fact.
  - The cold-path identity injection writes `/etc/atlas-routing.env` (base URL only,
    no UUID) pointing at the trusted-edge FQDN; the warm MMDS payload + freshen carry
    the same.
- **Host facts (e2e):** rides along in the self-serve use case
  ([`self_serve_site.py`](../atlas/tests/e2e/use_cases/self_serve_site.py)) — on a
  real bench VM: `register <label>` **then** `bench new-site <label>.<region>.frappe.dev`
  → assert the reservation exists **and the proxy's live map serves it** (read the map
  back, not just the DB row); a forced `bench new-site` failure → assert the client's
  `deregister` rollback left **no** stale `Subdomain`; `bench drop-site` then
  `deregister` → assert it **drops from the live map**; `list` from inside the guest
  returns that VM's routes and a manufactured stray is cleared by the client's
  per-stray `deregister`; a direct `VirtualMachine.terminate` leaves no `Subdomain`.
  **IPv6 origin (the trust root):** the in-guest POST actually traverses IPv6, so a
  `register` from inside the real guest, through the trusted edge, resolves to *that*
  VM by its v6 source `/128` even when the guest sends a forged `X-Forwarded-For` (and
  the audit row records the divergence); the routed-tap host prevents a second VM from
  emitting that source; and a forced v4 attempt fails to resolve (no per-VM v4). Only a
  host run can prove these, and the feature is not safe to ship until they pass.
