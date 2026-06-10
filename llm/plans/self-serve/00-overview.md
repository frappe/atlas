# Self-serve site — plan index

Breaks [self-serve-parallelism.md](../../self-serve-parallelism.md) into
right-sized, independently-buildable plans. The proxy + TLS halves are built and
in spec ([12-proxy.md](../../../spec/12-proxy.md),
[13-tls.md](../../../spec/13-tls.md)); what remains is the **site** layer that
turns *signup → live Frappe site* into a few-seconds flow.

Each plan below is self-contained: it states its goal, the files it touches, the
contract it consumes/produces, the build steps, and how it's proven. Read this
index first for the dependency graph and the three contracts that must be frozen
before forking the parallel tracks.

## The flow being built

```
signup form ─▶ email verify ─▶ Site row insert ─▶ VM provision (golden image)
   (04)            (04)            (02)               (01 image, existing VM)
                                      │
                                      ▼
                          deploy-site.py in guest ─▶ HTTP 200 probe ─▶ Site=Running
                                   (03)                   (03)            (02)
                                      │
                                      ▼
                          Subdomain row ─▶ proxy routes ─▶ live at acme.fra1.frappe.dev
                          (existing proxy layer; 02 creates the Subdomain)
```

## The plans

| # | Plan | Track | Gates on | Provable once | Status |
| - | ---- | ----- | -------- | ------------- | ------ |
| 01 | [Golden bench image](./01-golden-image.md) | T-IMG | nothing | a VM boots from it | **built + host-proven (from-scratch bake, 2026-06-10)** |
| 02 | [Site DocType + routing API](./02-site-doctype.md) | T1 | nothing (contracts frozen) | unit, milliseconds | **built + unit-green** |
| 03 | [deploy-site.py + readiness gates](./03-deploy-site-script.md) | T2 | 01 (needs a booted golden VM) | against any golden VM | **built + unit-green + host-proven (default_site fix, DRIFT L-1)** |
| 04 | [Signup + email verification](./04-signup-verification.md) | T3 | 02 (needs Site doctype) | unit + once 02 lands | **built + unit-green (SPA Sites screen deferred)** |
| 05 | [signup→live-site e2e proof](./05-e2e-proof.md) | Phase 7 | 01,02,03,04 + proxy/TLS | host, end-to-end | **built + HOST-PROVEN end to end (signup→live site, v4+v6, 2026-06-10)** |
| 06 | [Spec & docs surfaces](./06-spec-and-docs.md) | cross-cutting | the others | on landing each | **done — audited + roadmap closed (v0.7/0.8/0.9)** |
| 07 | [Fast deploy (sub-5s, no pool)](./07-fast-deploy.md) | perf | 01–06 + HANDOFF G1/G2 | unit now; host on re-bake | **planned — gated on the in-flight re-bake** |

## Dependency graph & sequencing

```
01 golden-image  ───────────────┐ (longest wall-clock: apt+clone+pip in rootfs)
                                 ├──▶ 03 deploy-site ──┐
02 site-doctype ──┬──▶ 04 signup ┘                     ├──▶ 05 e2e proof
                  └───────────────────────────────────┘
06 spec/docs: written alongside each plan as it lands.
```

- **Start 01 first.** It's the longest single wall-clock item (building a
  bench-preinstalled rootfs) and it blocks nobody — kick it off and let it bake
  while 02 proceeds in parallel.
- **01 and 02 are fully parallel.** 02 is pure Frappe doctype + permissions +
  one read endpoint; it needs no host and no image.
- **03 needs 01** (a booted golden VM to run `deploy-site.py` against) but is
  otherwise independent.
- **04 needs 02** (the `Site` doctype it inserts) but its forms/verification are
  buildable in parallel and unit-provable before 02 fully lands.
- **05 is last by definition** — it consumes everything.
- **06 is not a phase you "do at the end"** — each plan's "Spec & docs" step
  updates its slice of 06 as it lands. 06 collects the cross-cutting surfaces
  (doctype catalogue count, README use-case table, roadmap → built move) so
  nothing is missed.

## The three contracts to freeze before forking 02 / 03 / 04

The proxy/TLS contracts are locked. These three — consumed by the site tracks —
are **not yet written down**. Freeze them here; their durable home is the new
spec chapter (see [06](./06-spec-and-docs.md), recommended `spec/14-self-serve.md`).
All three tracks must agree on these *before* they fork, or 02's state field and
03's probe (etc.) will disagree.

### Contract A — the one routing string

One identity threads the whole system:

```
site-name-on-disk  ==  subdomain FQDN  ==  proxy Host header  ==  Atlas Site key
                       e.g.  acme.fra1.frappe.dev
```

- The **subdomain label** (`acme`) is a single DNS label — **no dots** — so the
  site stays inside the one regional wildcard `*.fra1.frappe.dev` that the proxy
  already terminates. A label with a dot would escape the wildcard and need its
  own cert (deferred — see proxy remaining-work #8).
- The full FQDN is the Frappe **site name on disk** in the guest, the **Host
  header** the proxy routes on, and the **`Site` doctype key**. One string, four
  roles — never transformed between them.
- **Reserved-name denylist.** `www`, `admin`, `api`, `proxy`, `app`, `dashboard`,
  `mail`, `ns`, `root`, plus anything already taken. Lives with the `Site`
  doctype validation (02).

*Consumed by:* 02 (autoname + validation), 03 (site dir on disk), 04 (the
subdomain field on the signup form).

### Contract B — the readiness signal

A `Site` flips to **Running only on an observed HTTP 200** from the guest's
`:80` — **not** on the VM's `status == Running`, which means "jailer launched the
microVM", *not* "Frappe is serving". These are different events separated by the
whole `deploy-site.py` run.

- 02's `Site.status` field and 03's probe must name the same signal. The probe is
  an HTTP GET to the guest `:80` returning 200 (over the VM's `/128`, the same
  path the proxy's south hop uses).
- Until the 200 is observed the Site sits in an intermediate state (e.g.
  `Deploying`); on 200 it goes `Running`; on timeout it goes `Failed`.

*Consumed by:* 02 (the `status` Select values + the method that sets `Running`),
03 (the `wait_for_http` gate that returns the signal).

### Contract C — ownership / verification ordering

```
signup form → email verification → THEN Site row insert → verified user is `owner`
```

- **Email verification precedes the VM insert.** No droplet/site work happens for
  an unverified email — verification is the gate, so a typo'd or hostile address
  never triggers a (billable) provision.
- The verified user is stamped as Frappe's built-in **`owner`** on the `Site`
  row (the same ownership model VMs/snapshots/SSH-keys use, scoped by
  `permission_query_conditions`).
- 02's permissions (`Atlas User`, `if_owner`) must match what 04 stamps: the
  user who verified owns and sees only their own Site.

*Consumed by:* 04 (owns the ordering + stamps `owner`), 02 (permissions must
match).

## Conventions every plan inherits (from the existing codebase)

So the plans don't each re-derive them:

- **User-owned doctype shape** — copy
  [virtual_machine.py](../../../atlas/atlas/doctype/virtual_machine/virtual_machine.py)
  and [subdomain.py](../../../atlas/atlas/doctype/subdomain/subdomain.py):
  `IMMUTABLE_AFTER_INSERT` tuple guarded in `validate()`, status Select written
  by controller methods (read-only on form), `after_insert()` enqueues the
  background job, `@frappe.whitelist()` methods that `frappe.throw` early on
  wrong state and return a Task name.
- **Ownership scoping** — Frappe's built-in `owner` column + a
  `permission_query_conditions` entry wired in
  [hooks.py](../../../atlas/hooks.py) pointing at
  [permissions.py](../../../atlas/atlas/permissions.py) (`owner_only`). No custom
  owner field.
- **Task scripts** — typed-Python under [scripts/](../../../scripts/): a frozen
  `TaskInputs` dataclass (`--kebab-case` flags via `from_args`), one
  `ATLAS_RESULT={json}` line out via `emit()`, stdlib-only, lib under
  `scripts/lib/atlas/`. Run on the host via `run_task(...)`; run *in the guest*
  via the SSH-to-guest path the proxy uses (`connection_for_guest`).
- **Readiness** — `wait_for_ssh` already exists
  ([_ssh/transport.py](../../../atlas/atlas/_ssh/)); `wait_for_http` is new (03).
- **Placement** — a user-created resource fills its server/image defaults via
  [placement.py](../../../atlas/atlas/placement.py), not by asking the user.
- **e2e** — one module per use case under
  [atlas/tests/e2e/use_cases/](../../../atlas/tests/e2e/use_cases/), `run()` +
  `run_smoke()`, host facts vs unit-covered logic split (spec README "Testing").

## Scope discipline (don't build these)

Explicitly out of scope for this layer — noted so no plan drifts into them:

- No teams / sharing / billing / quotas (a Site has one `owner`, full stop).
- No multi-label subdomains / custom-domain certs (one wildcard covers all; proxy
  remaining-work #8).
- No `Site Member`, no approval workflow — signup is self-serve, verification is
  the only gate (Contract C).
- No second image-build *pipeline* — 01 is one new image **variant**, not a
  general builder (spec non-goal "No image build pipeline" still holds; 01 is a
  baked artifact, see its plan).
- The proxy south-side firewall, health-withdrawal, and reconcile-scheduling gaps
  are **proxy** remaining-work (proxy-design.md), not site work — out of scope
  here.
