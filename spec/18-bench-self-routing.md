# Self-service subdomain routing (bench-admin sites)

A bench VM is a long-lived box where the owner spins up **arbitrary sites** from
inside the guest — the bench-admin UI (`admin/`) or `bench new-site`. This chapter
makes those guest-created sites routable through the regional proxy with **no operator
action**, as long as the site is named inside the regional wildcard
(`<label>.<region>.frappe.dev`). Dropping a site stops routing it. Uniqueness (one
subdomain → one VM, fleet-wide) is enforced and surfaced to the bench user at create
time.

> **vs. 14-self-serve.md.** That chapter is the one-site-per-VM, Atlas-driven flow (Atlas
> clones the golden and inserts the one [`Subdomain`](./02-doctypes.md#subdomain) itself).
> This is the many-sites-per-VM, guest-driven generalization: Atlas never ran `bench
> new-site`, so the job is to get a `Subdomain` row inserted/removed for sites Atlas never
> created — reusing the whole `Subdomain` → proxy engine.

The model is **push-only, one-way (VM → Atlas)** over the guest's own egress: the
guest *tells* the controller what changed; the controller never SSHes back, and there
is no scheduled pull or sweeper. The guest binary is `bench-domain-provider`
(Component D), the language-agnostic plug-in [pilot](../../references/pilot) discovers
on `PATH` and drives by exit-code + stdout JSON.

## The shape (one-way push: the guest tells, Atlas writes)

Everything downstream of a `Subdomain` row already works: its `after_insert` enqueues a
deduplicated regional proxy reconcile, its `on_trash` deconverges, and `subdomain` is
`unique:1` (DB-enforced fleet-wide uniqueness) — see [12-proxy.md](./12-proxy.md) and
[`subdomain.py`](../atlas/atlas/doctype/subdomain/subdomain.py). **No proxy code
changes.** The new code only creates/removes the row for a guest-created site, lets the
guest **list** its own rows, and **arbitrates** + **audits** every call.

Four verbs, all carrying **no VM-identifying argument** — the controller resolves the
calling VM from the request source address (*Caller resolution*), so a guest can only
ever speak as its own box:

- **`register(label)`** — run **BEFORE** `bench new-site`: the authoritative INSERT that
  reserves the name (`active=1`), atomically claiming the fleet-wide `unique` key so it can't
  be grabbed out from under a create already underway — the block-at-create gate (Component A).
  A decline → the guest never starts `bench new-site` (no orphan); a create failure → the
  guest `deregister`s to release the reservation.
- **`deregister(label)`** — after `bench drop-site`, **or** the rollback when `bench new-site`
  fails: DELETEs the caller's own `Subdomain`, idempotent (Component A).
- **`check_label(label)`** — OPTIONAL read-only advisory availability answer; a UX nicety,
  never the gate (Component B).
- **`list()`** — read-only; the caller VM's own `Subdomain` rows, to find and clear strays
  (Component C).

`register`/`deregister` carry the routing state (the only creator / the only remover of
the row); `check_label`/`list` write nothing, so the controller stays the **sole
writer** of the fleet-wide-unique table. Every write is **arbitrated, not trusted** — the
controller owns the `unique` key, the per-VM cap (Component G), and the brand denylist
(Component H), so a guest's word is accepted only if it passes the rules. **Every call**,
read or write, accepted or rejected, is recorded in the MyISAM audit log (Component I).

> **What dropping the pull costs.** An earlier design used a scheduled SSH pull as the
> source of truth. One-way trades that for simplicity and for surviving SSH-key loss (a
> guest with no inbound SSH still registers/deregisters/lists over its egress). The two
> risks the pull guarded:
> - **Hijack of another VM's name** — *closed* by Caller resolution (a guest writes only
>   its own source `/128`'s routes) + the DB unique key + the denylist.
> - **A lost/withheld `deregister`** — *bounded, accepted residual*. A site dropped on a
>   still-alive VM whose `deregister` never lands stays routed, but the bench nginx emits a
>   per-site `server_name <fqdn>` vhost with **no `default_server` catch-all**
>   ([`deploy-site.py`](../bench/deploy-site.py)), so the stale route serves a **404, not a
>   co-resident tenant's site** — a dead link, not a cross-site exposure. Cleared by terminate
>   (Component F) or sooner by the owner (`list` + `deregister`); we accept the window rather
>   than add a TTL/heartbeat/sweeper.

## Component A — `register` / `deregister` (the guest writes, the controller arbitrates)

`atlas/atlas/bench_routing.py`, both `@frappe.whitelist(allow_guest=True)` +
`@rate_limit`. Each resolves the calling VM from the request source (*Caller
resolution*) → region (*Component E*), runs its rules, writes if they pass, and audits
the outcome (*Component I*) on **every** path:

```
register(label)   -> {"status": "ok" | "taken" | "reserved" | "at_limit" | "invalid"}
deregister(label) -> {"status": "ok"}
```

**`register(label)`** runs the **same** Contract-A rules `Site` enforces, in order,
before writing: `validate_label` (shape) → `validate_reserved` + brand denylist
(Component H) → fleet-wide availability (`is_taken` + an existing `Subdomain`) → per-VM
cap (Component G). On a pass it inserts `Subdomain(subdomain=label,
virtual_machine=<resolved vm>, active=1)`; the row's `after_insert` reconciles the proxy.
A `DuplicateEntryError` (two benches racing the same label) maps to `taken` — the DB
unique key is the **atomic arbiter**, and reserving *first* is what makes the subsequent
create un-blockable. `taken`/`reserved`/`at_limit`/`invalid` insert nothing and tell the
guest why. `register` admits exactly one label, never evicts, and is **idempotent** on an
already-owned label (a retry after a transient failure is a clean `ok`).

**`deregister(label)`** resolves the VM, finds its `Subdomain(subdomain=label,
virtual_machine=<vm>)`, and **deletes** it (its `on_trash` deconverges the proxy). Scoped
to the caller's own VM, so a guest can never deregister another VM's route (the row's
`virtual_machine` must match, else no-op). **Idempotent**: an absent row is a clean `ok`
(a double drop, a replayed POST, a `list`-driven stray clear, or a rollback for a
`register` that itself failed).

Both apply every fleet-protecting rule (uniqueness, reserved, denylist, cap, own-VM
scoping) controller-side, and both update the proxy immediately through the existing
`Subdomain` hooks — no second push, no pull to wait on.

> **Only trustworthy if the edge holds.** Both resolve the VM from `frappe.local.request_ip`
> — the real peer `/128` only behind the trusted edge (*Caller resolution*), which is **not
> yet built, a hard prerequisite**. Below it a forged XFF is a route hijack; the audit log
> (Component I) detects the attempt after the fact.

## Component B — `check_label` (the optional advisory pre-flight)

`atlas/atlas/bench_routing.py`, `@frappe.whitelist(allow_guest=True)` + `@rate_limit`.
**Read-only** and **not the gate** (`register` is):

```
check_label(label) -> {"status": "ok" | "taken" | "reserved" | "at_limit" | "invalid",
                        "suffix": "<region domain>"}
```

It runs the same checks `register` will (`validate_label`, `validate_reserved` + denylist,
`is_taken`, the per-VM cap against the **source-resolved** VM) and returns the active region's
domain (*Component E*) so the guest can build the FQDN without carrying it. It takes **no
VM-identifying argument**, writes nothing, but **is audited**. Fail-open, hence not the gate
(a stale "ok" acted on by a create is the race window `register`'s atomic insert closes). A
malformed label returns a typed `{"status": "invalid", "reason": "<message>"}` (not a 417) so
the guest hook can surface the operator's message verbatim.

## Component C — `list` (the guest reads its OWN routes to find strays)

`atlas/atlas/bench_routing.py`, `@frappe.whitelist(allow_guest=True)` + `@rate_limit`.
**Read-only**, takes **no argument** — the VM is the source address (*Caller
resolution*):

```
list() -> {"domains": [{"label":  "<label>",
                        "fqdn":   "<label>.<region domain>",   # built controller-side
                        "active": true | false}, ...]}     # [] for a VM with no rows
```

Returns **all** `Subdomain` rows where `virtual_machine ==` the source-resolved VM.
`fqdn` is reconstructed controller-side as `f"{label}.{region_domain}"` (from the active
Root Domain, *Component E*; never echoed from a guest suffix). `list` writes nothing and
**does not touch the cap** (Component G counts on a *write*). It is audited like every
call.

**Purpose — the guest's stray finder.** The owner enumerates its own routes and compares
them against its on-disk `sites/`. A `Subdomain` with **no matching on-disk site** is a
*stray* (a lost `deregister`, or a drop while the controller was unreachable); the guest
then `deregister`s each. Because there is no sweeper, `list` + `deregister` is how a stray
on a still-running VM is cleared before terminate (Component F). The controller stays the
**sole writer** — `list` is a *view*, never a lever — and each `deregister` is
**per-stray**, individually arbitrated and audited. We deliberately do **not** expose a
converge-style "here is my whole set, delete the rest" endpoint: that would re-introduce
guest-driven reconcile (the pull-shaped coupling this chapter removed) and let one
malformed set **mass-delete** its own routes in a single call.

A source matching no VM / a Terminated VM / a proxy is a **clean reject** (a
`frappe.throw`, no listing) — the same Caller-resolution gate the writes use; an empty
inventory is the typed `{"domains": []}`, never a throw. That gate cuts both ways: under a
broken edge a forged `X-Forwarded-For` resolves `list()` to a *victim* and leaks its
inventory (a read-hijack, the same trust dependency as the writes). And `list` is
guest-initiated — a VM that never calls it still depends on **terminate** (Component F),
the only controller-side teardown, for cleanup.

## Caller resolution (the VM is the source address, never a parameter)

All four endpoints derive *which VM is calling* from the request's **public IPv6 source
`/128`**, matched against `Virtual Machine.ipv6_address` — never from a request parameter. A
guest is root in its own VM and could forge any injected `vm_uuid`/secret to name *another*
VM; the source address it cannot forge, so it can only ever speak **as the box its packets
come from**. That is what makes a *writing* one-way push tolerable: a writer, but only of its
own routes.

- No injected secret is involved: a secret written into a guest authenticates "the VM" to
  a tenant who *is* root in it, so it identifies nothing the source `/128` doesn't already.
  `/etc/atlas-routing.env` carries **only** the base URL, no token (*Identity*).
- A spoofed/non-matching source, or resolution to a Terminated VM or a proxy, is a clean
  reject (`frappe.throw`): no write happens (and `list` returns no inventory). The rejected
  attempt is still **audited** with the source `/128` that tried — a non-resolving source
  is exactly the forensic signal worth keeping.
- **The resolver filters Terminated/proxy in the query and fails closed on a duplicate
  `/128`.** `ipv6_address` is **not** unique, and `allocate_ipv6` can recycle a Terminated
  VM's `/128` onto a fresh Running VM (the reuse guard is deferred,
  [09-roadmap.md](./09-roadmap.md)). So resolution selects only `status != Terminated`
  **and** `is_proxy = 0` — a stale Terminated row can never *shadow* the live owner — and
  if two *live* non-proxy VMs ever share a `/128` it resolves **neither** (a write under
  either would be wrong). A logic backstop *behind* the trust root, not a substitute for it.

**The trust root — and a real gap.** The guest doesn't reach the Frappe worker directly; it
traverses a hop (ngrok in dev, an edge/LB in prod), so `request.remote_addr` is the *hop's*
address and the VM's real `/128` survives only in **`X-Forwarded-For`**
(`frappe.local.request_ip`). Frappe's [`set_request_ip`](../../frappe/frappe/auth.py) trusts
XFF **unconditionally** and takes its **leftmost** value, never checking that a trusted hop
set it — and many proxies (ngrok) **append** rather than overwrite, so a guest sending
`X-Forwarded-For: <victim-/128>` puts its forgery leftmost, *ahead* of the genuine appended
IP, and **wins**. Used naively `request_ip` is attacker-settable — and here it gates a
*write*, so a guest could register/deregister/list another VM's routes. This is the single
most dangerous failure mode in the design.

**Therefore caller resolution requires a trusted edge that strips any client-supplied
`X-Forwarded-For` and sets it to the real peer `/128`** before the request reaches the
worker. The edge is the trust root of the whole feature.

- **Production edge: not yet built.** The base URL is just `frappe.utils.get_url()`;
  nothing today overwrites XFF in front of the controller (the spec/12 regional proxy
  fronts *tenant* traffic, not the Atlas controller). Standing up that edge is a **hard
  prerequisite** — without it the writes are spoofable and `list` is a read-hijack.
- **Local dev: ngrok, with the append trap** — configure ngrok so the worker keys off
  ngrok's real-client header, not a guest-prepended XFF, else dev "works" while trivially
  spoofable, hiding the prod gap.
- **Host anti-spoof is also load-bearing:** even with a trusted edge, the routed-tap host
  must not let VM-Y emit packets with VM-X's v6 source (RPF), or the edge faithfully records
  a spoofed peer. Both the **edge XFF-overwrite** and the **host anti-spoof** must be
  **verified**, not assumed — a failure is a hijack, not a nuisance.

> **The rate limiter shares the trust root's fate.** `@rate_limit(ip_based=True)` keys on the
> **same** `frappe.local.request_ip`
> ([`rate_limiter.py`](../../frappe/frappe/rate_limiter.py)), so a forged XFF below a broken
> edge defeats **both** per-VM scoping and the rate limit — the edge is the single hinge both
> swing on.

## Component D — the `bench-domain-provider` plug-in (guest side, process I/O)

A small stdlib-only binary committed at
[`bench/bench-domain-provider.py`](../bench/bench-domain-provider.py), installed by
[`bench/build.sh`](../bench/build.sh) at `/usr/local/bin/bench-domain-provider` (present on
every clone). It is the plug-in [pilot](../../references/pilot) looks up on `PATH` and drives
by **exit code + stdout JSON** (documented at
[pilot's `docs/domain-provider.md`](../../references/pilot/bench-cli/docs/domain-provider.md)) —
the boundary is process I/O, not a typed surface pilot imports. It reads only the Atlas base
URL from `/etc/atlas-routing.env` (*Identity*) and POSTs with **no VM-identifying argument**.
pilot wires it at the choke points both the admin UI and the CLI flow through (`new_site`,
`drop_site`, the Add/Remove-Domain admin path). The binary **no-ops cleanly** (`register`
exits 0, query verbs print blank) when `/etc/atlas-routing.env` is absent, so a non-Atlas
bench is unaffected.

### The POST must go over IPv6

Caller resolution matches the request's **source `/128`**, so the binary **must** reach the
controller over **IPv6** or there is no VM-identifying address to resolve (a v4 POST arrives
NAT'd). The binary pins the connection to an `AF_INET6`-only connector (resolve the host's
`AAAA`, connect over v6; fail loudly if there is no v6 route rather than silently falling
back to v4) — "no v6 route" is a transport error (register → fail-closed; query verbs →
fail-soft), never a v4 retry.

### The verbs

The controller receives exactly the bare `label` it already arbitrates: for a wildcard
subdomain the binary **peels the region wildcard suffix** off the full FQDN
(`app.<region>.frappe.dev` → `app`) before POSTing. The suffix is the `wildcard-domains`
answer (below), the same thing pilot's `matches_wildcard` gates on; a name not under the
wildcard peels to nothing and is **declined**. Exit-code convention (pilot only checks zero
vs non-zero): `0` ok / fail-soft no-op; `1` transport/config failure; `2` declined; `64`
usage error.

- **`register <domain>`** — before `bench new-site`. Peels to the label and POSTs
  `register(label)` (a custom, non-peeling FQDN routes to `register_custom_domain` —
  Component L). **Exit 0** = route reserved (the row appears on register, not after create);
  **exit 2** = declined (`taken`/`reserved`/`at_limit`/`invalid`, or a non-wildcard /
  multi-label name Phase 1 doesn't route) — pilot stops, no orphan; **exit 1** = transport
  failure → pilot aborts (**FAIL-CLOSED**: if the route wasn't provisioned, the site
  shouldn't exist). Absent config → exit 0 (not an Atlas bench). Idempotent on the caller's
  own label.
- **`deregister <domain>`** — after `bench drop-site`, **and** as the rollback when
  `bench new-site` fails. Peels and POSTs `deregister(label)`, **always exit 0** (a non-zero
  would throw on an otherwise-successful drop). Best-effort: a lost `deregister` leaves a
  404-serving route until terminate (the accepted residual). The guest binary has **no
  `list` verb**, so guest-side stray clearing has no equivalent — pilot drives `deregister`
  itself on drop, and total teardown is still `terminate()` (Component F).
- **`generate-dns-records <site> <domain>`** — pre-flight, read-only, **advisory**: the DNS
  records the user adds at *their own* provider so a custom name reaches their site (**Atlas
  writes to no zone**). A wildcard subdomain we route needs none (`{}`, exit 0); a custom
  domain gets the recipe from `dns_records(domain, site)`: **CNAME → the caller's own regional
  site FQDN** (a name *reserved* to this VM, so no other tenant can claim the route), **A +
  AAAA → the proxy fleet** (the apex fallback where a CNAME is illegal), **CAA → the active
  issuer** (omitted for a Self-Managed issuer). The controller verifies the caller **owns**
  `site` before advising a CNAME. Fail-open (the real gate is `register`).
- **`wildcard-domains`** — host-level: the wildcard pattern(s) sites here may be named
  under, `["*.<active region domain>"]`. Fail-soft (blank + exit 0). pilot constrains site
  names to these.
- **`proxy-servers`** — host-level: the regional edge proxies' public IPs that front this
  bench. Fail-soft. When non-empty, pilot locks its nginx to exactly these
  (`allow … ; deny all;`), trusts their `X-Forwarded-For`, and forwards it upstream
  untouched — see *Trust root* below.

### Trust root — `proxy-servers` closes the gap caller resolution flagged

Caller resolution is sound only if the bench reads the real client IP from a trusted edge
(*Caller resolution*). `proxy-servers` is how the bench learns which IPs to trust: pilot locks
nginx to exactly those (`allow … ; deny all;` + `set_real_ip_from` + forward XFF untouched),
so the forged-XFF hole is closeable in the field.

## Component E — region (controller-resolved, not VM-asserted)

No VM — site or proxy — carries a `region` field; Atlas is single-region and the one stored
region is `Atlas Settings.region`. A VM-carried region would drift and misroute, so region
is resolved **controller-side**: every endpoint builds the FQDN from the single active
[Root Domain](./02-doctypes.md#root-domain)'s domain suffix (`active_root_domain().domain`,
[`placement.py`](../atlas/atlas/placement.py)) the same way
[`Site`](../atlas/atlas/doctype/site/site.py) does. `check_label` returns the region
**domain** so the guest can name its site; `list` returns each row's **FQDN** built from it.
The served FQDN is always `f"{label}.{region_domain}"`, built controller-side, never parsed
from a guest-supplied suffix.

## Component F — controller-side teardown (terminate deletes everything)

The **only** controller-side teardown is `VirtualMachine.terminate()`, and it is total, so
no sweeper is needed. It calls `_delete_subdomains()` beside `_detach_reserved_ip()` /
`_delete_snapshots()`
([`virtual_machine.py`](../atlas/atlas/doctype/virtual_machine/virtual_machine.py)): delete
every `Subdomain` where `virtual_machine == self.name`. **Already built.** When a VM dies,
*all* its routes die with it, no guest cooperation, each delete's `on_trash` deconverging
the proxy. Because terminate removes the rows pointing at a `/128` *before* that address can
be recycled (`allocate_ipv6` only re-hands an address a terminated VM released), **no
surviving row can drift onto a new tenant** — which is why the old address-drift sweeper is
gone.

> **Why no sweeper.** The earlier hourly `sweep_stale_subdomains` caught (a) rows of VMs
> killed out-of-band and (b) a route whose address drifted onto a recycled `/128`. Case (b)
> is closed structurally (above); case (a) is an Atlas-internal invariant to uphold — every
> VM removal goes through `terminate()` — not a routing concern to scan for. The
> still-running-VM residual 404s (*The shape*) and is cleared by `list` + `deregister` or
> terminate; the `allocate_ipv6` reuse guard ([09-roadmap.md](./09-roadmap.md)) is the
> belt-and-suspenders follow-up.

## Component G — the per-VM subdomain cap (namespace-exhaustion control)

A bench owner can `bench new-site` arbitrarily many sites; without a ceiling one tenant
occupies an unbounded slice of the region's namespace (and bloats the proxy map). The unique
key blocks hijacking an *owned* name and Component H blocks *branded* names; the cap blocks
*bulk* squatting of unowned names.

The cap is a **memory tier** — a small lookup keyed on `memory_megabytes`, so a `resize()`
re-prices it for free:

```
cap(vm):
   ≤  8 GB → 20      # the base — every size in sizes.py today sits here
     16 GB → 40
     32 GB → 80
     ≥ 64 GB → 160
```

**Enforced authoritatively in `register`** (mirrored advisorily in `check_label`): count the
resolved VM's `Subdomain` rows; at or above `cap(vm)`, `register` returns `at_limit` and
inserts nothing. Because each `register` admits exactly one label and never evicts, the cap
is a simple ceiling — sites already routed stay routed, the (N+1)th create is refused at
write time. Adding a size is one more table row, [`sizes.py`](../atlas/atlas/sizes.py).

## Component H — the brand/keyword denylist (a DocType, editable live)

`RESERVED_SUBDOMAINS` blocks structural labels (`www`, `api`, …) and is frozen in code. The
**brand denylist** is the complement: a tenant grabbing
`paypal`/`stripe`/`login`/`account`/… under the valid wildcard TLS cert —
phishing-as-a-service on a name no other VM holds yet. Because the brand list **changes over
time** (a new payment brand, an abused keyword spotted in the audit log), it lives in a
**DocType**, not a code constant:

```
Subdomain Denylist  (engine: InnoDB; one row per blocked label)
  label    Data   autoname: field:label, unique:1 — the blocked label (lowercased)
  reason   Data   operator note ("payment brand", "auth keyword", …)
  enabled  Check  default 1 — flip off to lift a block without losing the row/reason
```

An operator adds a row and the next `register`/`check_label` honors it **immediately** — no
deploy, no migrate. Enforcement is in the same `validate_reserved` seam (both `check_label`
and `register` reject a denylisted label), a single indexed `exists("Subdomain Denylist",
{"label": <lowercased>, "enabled": 1})` run inline. Seeded at install with the obvious
payment/auth/brand terms; the operator curates from there, often straight from a
hijack-attempt row in the audit log (*Component I*).

## Component I — the request audit log (MyISAM)

Every call to every endpoint — the four per-site (`check_label`, `register`, `deregister`,
`list`) **and** the two host-level queries (`wildcard_domains`, `proxy_servers`, which carry
no VM and audit with a blank `vm` + the asking source) — writes one row to an append-only
DocType, **`Bench Routing Audit`** (`"engine": "MyISAM"`). It is the forensic backbone of
the trust-root story: this log is **how a hijack attempt is detected** — a `register` whose
source `/128` resolved to VM-X while a forged `X-Forwarded-For` named VM-Y leaves a row with
*both* facts side by side.

**Why MyISAM, when every other Atlas DocType is InnoDB.** A MyISAM insert is **not rolled
back** when the surrounding request transaction rolls back — and that is the point. A
*rejected* `register` (or a non-resolving source) `frappe.throw`s, unwinding the request
transaction; on InnoDB the audit insert would unwind with it and we would **lose the record
of exactly the attempts most worth auditing**. Persistence rides **MyISAM's auto-commit
alone** — the helper does **not** call `frappe.db.commit()`, which would also flush partial
transactional work done before the throw (defeating the reject's rollback). The honest cost:
no crash-safe recovery, no FK integrity — acceptable for an append-only log. (Verify at
migrate that the table is created `ENGINE=MyISAM` and not coerced to InnoDB by the
deployment's MariaDB config — the whole argument rests on it.)

```
Bench Routing Audit  (engine: MyISAM, append-only, sole writer = _audit())
  endpoint     Data   check_label | register | deregister | list |
                      wildcard_domains | proxy_servers
  label        Data   the label argument; BLANK for list() and the host queries
  status       Data   ok | taken | reserved | at_limit | invalid | unresolved
                      (the SAME values an endpoint returns/throws; "unresolved" =
                       caller resolution found no VM, i.e. a spoof attempt)
  business_reject Check 1 = a rules decline or an unresolved source; 0 = a clean ok.
                       (A @rate_limit throttle is NOT a row here — see below.)
  vm           Data   resolved VM name — a Data SNAPSHOT, not a Link: the row must
                      survive the VM's deletion (a Link would dangle/cascade), and a
                      spoof resolves to NO vm (blank vm + a source_ip)
  source_ip    Data   the /128 caller resolution KEYED ON (frappe.local.request_ip) —
                      the exact value the trust decision used; recorded even when it
                      resolved to no VM
  fwd_headers  Long Text  the forwarded-header chain (incl. raw X-Forwarded-For), VERBATIM
  request_body Long Text  the raw POST body, guest-controlled, VERBATIM
  creation     (built-in)  Frappe's own timestamp
```

A single helper `_audit(endpoint, label, status, *, business_reject, vm, source_ip,
fwd_headers, request_body)` is called on **every path of every endpoint, including the
reject/throw paths** (audit-before-throw). `source_ip` (the single value resolution acted
on) and `fwd_headers` (the whole forwarded chain verbatim) agree behind a correct edge; when
they **disagree** — a clean edge peer in `source_ip` but a guest-prepended
`X-Forwarded-For: <other-/128>` in `fwd_headers` — that is a recorded forgery attempt, the
hijack signal.

> **A `@rate_limit` throttle is *not* in this table.** The decorator raises *before* the
> endpoint body, so `_audit()` never runs for a throttled request — a throttle surfaces as
> Frappe's own 429 + rate-limiter logs, not a row here. The table records business
> decisions, not transport throttling. (Auditing throttles from the decorator seam is a
> future enhancement.)

**Retention.** The table grows **unbounded** — one row per request, forever, storing
guest-controlled `fwd_headers`/`request_body` verbatim (a size/PII caution for any export).
A prune is **wanted but out of scope for v1**; named here, not built.

## Component L — custom (non-wildcard) domains (Phase 2, SNI passthrough — BUILT)

A **custom domain** is an arbitrary external FQDN the customer already owns
(`shop.acme.com`), routed to one site VM — the full-FQDN sibling of `register`. It keys on the
**whole host** and is **SNI passthrough**: the proxy reads the SNI at L4 (`ssl_preread`, no
decrypt) and forwards the raw TLS stream to the VM's `:443`, which terminates with **its own
Let's Encrypt cert**
([12-proxy.md](./12-proxy.md#the-stream-front-door-sni-passthrough-for-custom-domains),
[13-tls.md](./13-tls.md#custom-domains-sni-passthrough-the-vm-holds-the-cert)) — the proxy
holds **zero** per-domain certs.

- **`Custom Domain` DocType** (`atlas/atlas/doctype/custom_domain/`): autoname `field:domain`
  (the full FQDN, fleet-unique), `virtual_machine` Link, `address` (the VM's `/128`,
  denormalized passthrough target), `site` (the regional FQDN it aliases — provenance), and
  `status` Select **Active/Failed** (informational — Active on register; Failed signals a
  reconcile error). Hooks mirror `Subdomain` and share the **same dedup reconcile job**
  (`auto_reconcile_subdomains`), reconciling on `active`. The dot ban + per-VM cap stay on
  `Subdomain` and do **not** apply here.
- **`register_custom_domain(domain)` / `deregister_custom_domain(domain)`** — the full-FQDN
  twins of `register`/`deregister`: same trust root (caller resolution by source `/128`),
  same audit, same atomic arbitration (the `Custom Domain` unique key).
  `validate_custom_domain` requires a real FQDN **not** under the regional wildcard (a
  wildcard-shadowing name belongs in the `register(label)` path). `register_custom_domain`
  inserts `status=Active` and the domain enters **both** proxy maps immediately.
- **Two maps, one fill-time (no readiness gate).** The custom-domain → VM map lives in
  **both** proxy subsystems — a `:80` ACME-passthrough copy (http `acme_domains`) and a
  `:443` SNI-passthrough copy (stream `domains`) — the **same** row set, differing only in
  value shape (the `:80` copy is the bare bracketed v6 so the VM can complete its first
  HTTP-01 issuance; the `:443` copy appends `:443`). A domain is in both maps the moment it
  is registered. If the VM's cert isn't issued yet the proxy forwards a handshake the VM
  can't complete (a transient client-side error that self-heals once the cert lands); pure
  passthrough, no cross-tenant effect, so a gate isn't worth its cost. `proxy.py` reconciles
  all three maps per proxy (subdomain `/sync`, SNI `SYNC-SNI` over the stream-admin line
  protocol, ACME `/acme/sync`), each on its own byte-diff.
- **Guest binary** (`bench-domain-provider`): `register`/`deregister` route a custom
  (non-peeling) domain to `register_custom_domain`/`deregister_custom_domain`. The VM issues
  its own cert out-of-band (pilot's `setup-letsencrypt` over the `:80` ACME route); Atlas
  does nothing on cert issuance — no confirm verb, no timer.
- **Teardown** (Component F.1): `terminate()` deletes every `Custom Domain` for the VM, the
  full-FQDN sibling of `_delete_subdomains`, so a custom-domain route never outlives its VM.

## Identity injected into the guest

The **only** thing routing injects is the Atlas base URL, to `/etc/atlas-routing.env`
(`0644 root:root`) — the guest needs somewhere to POST and nothing else. It carries **no VM
UUID and no token**: caller resolution is by source address, so the guest never sends a
VM-identifying value, and there is no secret to ride MMDS (unauthenticated plain HTTP any
tenant SSRF can read).

- **Cold provision** — [`rootfs.inject_identity`](../scripts/lib/atlas/rootfs.py) writes the
  file while the rootfs is mounted, alongside `authorized_keys` and the network env. The base
  URL rides an optional `routing_base_url` field on `Identity`, threaded from a
  `ROUTING_BASE_URL` Task var ([`provision-vm.py`](../scripts/provision-vm.py)) the controller
  sets to `frappe.utils.get_url()` — **the FQDN of the trusted edge**, so the guest's POSTs
  traverse the hop that overwrites XFF (*Caller resolution*).
- **Warm clone** — the disk is never mounted, so the base URL rides MMDS: `_mmds_metadata`
  adds `routing_base_url`, and the in-guest
  [`atlas-warm-freshen.py`](../bench/atlas-warm-freshen.py) writes the env file when it adopts
  a clone's identity.

> **`/etc/atlas-vm-uuid` is not a routing dependency.** Caller resolution is by source
> address, so routing needs neither a cold-path UUID injection nor a `vm_uuid` in the MMDS
> payload. `/etc/atlas-vm-uuid` remains only the warm-freshen adopted-identity marker,
> untouched by this chapter.

## Why this is simple, and where the risk lives

- **Simple** — reuses the whole `Subdomain` → proxy engine; the new code is four endpoints +
  the audit log + the denylist DocType + a thin guest client, with **no pull, no sweeper, no
  TTL/heartbeat, no token lifecycle, no MMDS secret**. Teardown is one place (`terminate()`),
  and a guest with no inbound SSH still routes its sites.
- **The risk concentrates in Caller resolution** — it gates a *write* (and the same-fate
  `list` read, and the rate limiter). If the edge fails to overwrite XFF (or the host lets a
  VM spoof another's v6 source), a guest can act as another VM — a hijack. The IPv6-only
  client (Component D) is load-bearing (a v4 POST has no per-VM source). This is the one
  property that must be verified on a host before shipping; everything else degrades
  gracefully, and the audit log **detects** a failure rather than preventing it.
- **Accepted residual** — a lost `deregister` on a still-running VM leaves a 404-serving route
  (no `default_server`) until the owner clears it (`list` + `deregister`) or terminate does.
  The only intentional gap; documented, not swept. Every write is one `Subdomain` change +
  reconcile and every call one `Bench Routing Audit` row, so a failure is "did the `register`
  POST arrive and pass the rules."

## Deferred (out of scope for v1)

- A per-region shared secret on the endpoints (caller resolution by source address +
  rate-limit are the v1 controls).
- The `allocate_ipv6` reuse guard (v1 relies on `terminate()` deleting a VM's rows before its
  `/128` is released — belt-and-suspenders only, [09-roadmap.md](./09-roadmap.md)).
- A TTL + guest keepalive heartbeat to expire stale routes on a still-running VM (v1 accepts
  the 404-serving dead-link window, narrowable by the owner via `list` + `deregister`).
- A scheduled sweeper (v1 has none — `terminate()` is the only controller-side teardown, and
  it is total).
- A "management access lost" / liveness signal per VM (one-way push has no pull whose failure
  would surface key loss; revisit if operators need it).
- Auditing `@rate_limit` throttles (the v1 audit log records business decisions, not transport
  throttles).
- A retention prune for `Bench Routing Audit` (it grows unbounded in v1).
- Multi-region cross-region suffix hardening (single-region today; the reconstruct-and-compare
  rule is specified now so it's correct when a second region lands).
- **Per-token whole-domain routing — a future billable tier.** Beyond the `*-{token}`
  suffix-match ([vm-url-tokens](../llm/references/vm-url-tokens.md)), routing an **entire
  domain** `*.{token}.{region}.{domain}` to a single VM needs **one wildcard TLS cert per
  token** — a per-token issuance cost, so a **paid service**, not the default. It composes
  with the Phase-2 SNI hook (the per-token wildcard cert is one more entry in the per-domain
  cert map). Documented, not built.

## Testing

- **Unit (milliseconds)** — each contract above has a regression; the load-bearing ones:
  - **Caller resolution:** all four endpoints resolve the VM from `frappe.local.request_ip`
    against `Virtual Machine.ipv6_address`, ignore any `vm_uuid` param, and reject no-VM /
    Terminated / proxy sources with no write (no inventory for `list`). The leftmost-XFF
    forgery must **NOT** resolve to the named victim — the one-way model's worst-failure
    regression.
  - **`register`:** the full rule chain in order + `active=1` on ok; `taken` on an owned label
    **and** on a `DuplicateEntryError` race (the atomic-reservation regression); `reserved`/
    `at_limit`/`invalid`; the row's `virtual_machine` is source-resolved; a re-register of an
    owned label is idempotent `ok`.
  - **`deregister`:** deletes only the caller's own row (another VM's is a no-op), idempotent
    on an absent row (drop + create-failure rollback), fires the `on_trash` reconcile.
  - **`check_label`/`list`:** advisory status mapping + the region suffix; `list` returns only
    the caller's own rows (`{"domains": []}` when empty), no write, no cap effect.
  - **Cap / denylist / audit / terminate:** the tier lookup + `at_limit` (never evict); a live
    `Subdomain Denylist` `enabled` flip; a `Bench Routing Audit` row on **both** ok and reject
    that **survives a request rollback** (the InnoDB-would-lose-it regression) with no
    `frappe.db.commit()`; `terminate()` deletes **every** `Subdomain` and the scheduler carries
    **no** sweeper entry.
  - **Guest client:** the typed `Registered`/`Declined`/`NotConfigured`/`TransportError`
    contract (the caller aborts the create on `Declined`); **IPv6-only** — an `AF_INET6`
    connector that raises `TransportError` rather than falling back to v4.
- **Host facts (e2e)** — rides the self-serve use case
  ([`self_serve_site.py`](../atlas/tests/e2e/use_cases/self_serve_site.py)) on a real bench VM:
  `register` then `bench new-site` → the reservation is in the proxy's **live map** (read
  back); a forced create failure → the `deregister` rollback leaves no stale `Subdomain`; drop
  + `deregister` → it drops from the live map; a direct `terminate` leaves none. **IPv6 origin
  (the trust root):** the in-guest POST traverses IPv6, so a `register` through the trusted edge
  resolves to *that* VM by its v6 `/128` even against a forged `X-Forwarded-For` (audited); the
  host blocks a second VM emitting that source; a v4 attempt fails to resolve. Only a host run
  proves these, and the feature is not safe to ship until they pass.
