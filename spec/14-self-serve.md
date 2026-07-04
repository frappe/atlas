# Self-serve sites

Turns *create_site → live Frappe site* into a few-seconds flow: Central requests
a site for a tenant (a subdomain label), and Atlas clones a golden bench VM,
deploys a site into it, and puts it behind the regional proxy at
`acme.blr1.frappe.dev`. The proxy ([12-proxy.md](./12-proxy.md)) and TLS
([13-tls.md](./13-tls.md)) halves already exist; this chapter is the **site
layer** that drives them.

> **Central drives the front door.** Customer signup, identity, and team
> membership live in **Central** ([16-central.md](./16-central.md)). Atlas owns no
> end-users: there is no `/signup` page, no email verification, no `User` rows, no
> `Atlas User` role. Central authenticates the tenant and calls Atlas's whitelisted
> `atlas.atlas.api.site.create_site` as the operator (token auth), passing the
> `team` (the Central `Team.name`) + the subdomain. Atlas get-or-creates the
> `Tenant`, inserts the `Site` stamped with it, and reports progress back to
> Central — pushed as `site.*` events (see [16-central.md](./16-central.md) § Event
> reporting) and pollable via `atlas.atlas.api.site.get_site`. The earlier
> `/signup`→`/verify`→`Atlas User` on-ramp was removed when this landed.

This chapter is the durable spec — the whole self-serve layer is built and
**host-proven**: the `Site` layer, the in-guest deploy script + HTTP readiness
probe, and the Central-facing `create_site`/`get_site` surface are built and
unit-green; the golden-image bake (`build.sh`) and the end-to-end flow are
host-proven — a golden snapshot baked from scratch, and a real `create_site` →
cloned golden site → deploy → live HTTPS through the proxy on IPv4 + IPv6. The per-VM deploy **renames**
the baked `site.local` to the FQDN via `bench rename-site`, which regenerates the
bench's nginx vhost (`server_name <fqdn>` + a v6 listener) and reloads — no admin
reset (the owner signs in via the one-click `login_url` the deploy mints), no `bench
restart`. The production gunicorn is multitenant (no `--site`), so it resolves the
renamed `<fqdn>` from the request `Host` header per request and the rename serves it
live.

## The one routing string (Contract A)

One identity threads the whole system — never transformed between roles:

```
subdomain FQDN  ==  proxy Host header  ==  Site doctype key
                e.g.  acme.blr1.frappe.dev
```

The FQDN is the **one routing identity** — the proxy `Host` header, the `Site`
key, **and** the on-disk Frappe site name, one string never transformed between
roles. The per-VM deploy renames the baked `site.local` dir to `<fqdn>`, so on
disk it is `sites/<fqdn>`. The production gunicorn is **multitenant** — `frappe
serve` (`frappe.app:application`) runs with no `--site`, so it resolves the site
from the request `Host` header **per request** (`get_site_name(request.host)`,
nothing cached at boot); once `sites/<fqdn>` exists and the bench's nginx vhost
carries `server_name <fqdn>`, the running workers serve it with **no restart**. The
bake still marks the vhost `default_server` so a pre-rename probe (the warm resume,
before the deploy runs) answers off the baked `site.local`.

- The **subdomain label** (`acme`) is a single DNS label — **no dots** — so the
  site stays inside the one regional wildcard `*.blr1.frappe.dev` the proxy
  already terminates. A dotted label would escape the wildcard and need its own
  cert (deferred).
- The full FQDN is built once in `Site.autoname()` as
  `<subdomain>.<region domain>`, where the region domain comes from the single
  active [Root Domain](./02-doctypes.md#root-domain) — the same row that ties a
  region to its wildcard zone for TLS. That FQDN is the Host header the proxy
  routes on, the `Site` key, and (after the per-VM rename) the on-disk site dir
  name — one string in every role.
- **Reserved denylist** — `www admin api proxy app dashboard mail ns root`, plus
  anything already taken (the FQDN-key uniqueness check throws a clean *"subdomain
  taken"*). Lives with the `Site` validation.

## The readiness signal (Contract B)

A `Site` flips to **Running only on an observed HTTP 200** from the guest's
`:80` — **not** on the backing VM's `status == Running`, which means "the
microVM launched", *not* "Frappe is serving". These are different events
separated by the whole deploy run.

- Until the 200 is observed the Site sits in `Deploying`; on 200 it goes
  `Running`; on failure it goes `Failed`.
- The probe is `atlas.atlas.deploy_site.wait_for_http` *(built)*: an HTTP GET to
  the guest `:80` over the VM's public `/128` — the same south-hop path the proxy
  uses, off-host so it is an honest end-to-end probe. It targets
  **`/api/method/ping`** (Frappe's built-in unauthenticated method, 200
  `{"message":"pong"}` once the web server is up *and* the site DB resolves) with
  the **FQDN as the `Host` header** (Contract A) — the same header the proxy
  sends, so the probe is an honest mirror of the real request path: the renamed
  `server_name <fqdn>` vhost answers it and the multitenant `frappe serve` resolves
  `sites/<fqdn>` from that `Host`. It polls until a clean 200 (connection-refused /
  502 are "not
  ready yet", swallowed) and raises `frappe.ValidationError` on timeout.

## Ownership / tenancy (Contract C)

```
Central (owns the tenant) → create_site(team, subdomain) → Tenant + Site (Pending)
```

- **Central is the gate.** Atlas exposes no guest write — `create_site` is
  operator-authorized (the Central service token). Central decides who may request
  a site (it owns identity, billing, and team membership); Atlas just provisions
  what Central asks for. There is no email-verification step in Atlas because there
  is no unauthenticated caller to gate.
- The `Site` row carries a **`tenant`** link (→ [Tenant](./16-central.md), keyed on
  the Central `Team.name`) for attribution — the same tenancy model VMs use
  ([16-central.md](./16-central.md)). Atlas no longer has end-user `owner` scoping
  or an `Atlas User` role; the fleet is operator/Central-facing (System Manager).

## The create_site → live surface *(built)*

```
1. create_site(team, subdomain, email?)               (operator API, Central token)
2.   ensure_tenant(team, email)                        → get-or-create Tenant
3.   insert Site (Pending), stamped with the tenant     → returns the mirror row
4. Site.after_insert → auto_provision (worker)          → provision → deploy → 200 → Running
5. site.* events to Central / get_site poll             → status, then URL + login URL
```

- **`atlas.atlas.api.site.create_site(team, subdomain, email=None)`** is the
  write endpoint. It `ensure_tenant`s the Central team
  (`email` seeds a new Tenant; an existing one is reused), inserts the `Site` with
  the tenant stamped, and returns the **mirror row** Central reflects (`name`,
  `team`, `subdomain`, `status`, `fqdn`). The `Site`
  controller enforces the **same Contract-A label rules** (shared
  `atlas.atlas.subdomain_label` — one source of truth for the label shape +
  reserved denylist) and the authoritative FQDN uniqueness, throwing a clean
  "already taken" Central surfaces. Atlas is single-region, so Central never
  picks a region — the FQDN suffix comes from the active `Root Domain`. Runs
  `ignore_permissions` — operator orchestration, not desk RBAC.
- **`atlas.atlas.api.site.get_site(name)`** is the read/poll half: the same mirror
  row, plus — once `status == Running` — the live `url`, the `login_url` and its
  `login_url_expires_at` (the tenant handoff). Before Running those are `None` (no
  handoff to give yet). Central polls this as a self-heal fallback to the pushed
  events; on the onboarding poll it compares `login_url_expires_at` to now and, if
  the URL has lapsed, calls `regenerate_login_url()` (via `run_doc_method`) to
  re-mint a fresh one before showing it.
- **Event reporting.** The `Site` doc_events
  (`atlas.atlas.central_report.on_site_after_insert` / `on_site_update`) push
  `site.created` on insert and `site.status_changed` on every status transition to
  Central, gated on `Central Settings.enabled` (a site without Central configured
  pays nothing). The `site.status_changed` for `Running` carries the tenant handoff
  (`url` + `login_url` + `login_url_expires_at`); earlier transitions carry neither.
  Delivery is fire-and-forget (the documented v1 tradeoff); the `get_site` poll is
  the self-heal. See [16-central.md](./16-central.md) § Event reporting.

**Login handoff.** After the Site reaches `Running`, the one-click `login_url`
minted in the guest is surfaced to Central — both in the `site.status_changed`
Running event payload and via `get_site` (gated on `status == Running`). It is a
plain `Small Text` (read-only, `no_copy`), not an encrypted password, so it is read
straight off the doc. Because the URL is short-lived (a real 24h Administrator
session, `login_url_expires_at` = mint + 1440 min), Atlas exposes
`regenerate_login_url()` (a whitelisted doc method) that re-mints it in the guest;
Central calls it via `run_doc_method` when the stored URL has expired. There is no
Atlas-hosted status page; the handoff is that one-click link + the live URL,
delivered to Central. The baked Administrator password still exists (a randomized
bake-time throwaway) but is never surfaced — the tenant signs in via `login_url` and
can rotate the password later themselves.

## The `Site` DocType *(built — this phase)*

Fields, validation, permissions, and the full field table are in
[02-doctypes.md → Site](./02-doctypes.md#site). The lifecycle:

1. **`before_insert`** validates the label (single dotless DNS label, not
   reserved) and sets `status = Pending`. The `Site` carries no `region` field
   (Atlas is single-region); the FQDN suffix comes from the active `Root Domain`
   at `autoname`. The owning `tenant` is set by `create_site` from the
   Central team; Atlas stamps no end-user `owner`.
2. **`autoname`** builds the FQDN key (Contract A).
3. **`after_insert`** enqueues `auto_provision` (`queue="long"` — it SSHes).
4. **`auto_provision(site_name)`** — the background orchestration:

   | Step | Action | Owned by |
   | ---- | ------ | -------- |
   | 1 | Clone the backing VM from `Atlas Settings.default_bench_snapshot` (`Virtual Machine Snapshot.clone_to_new_vm` — carries the baked bench + grown disk). `status → Provisioning`. | this layer |
   | 2 | `wait_for_ssh` — the cloned VM booted. | existing |
   | 3 | Run `deploy-site.py` in the guest: rename the baked `site.local` to the FQDN via `bench rename-site` (regenerates the vhost as `server_name <fqdn>` + a v6 listener + reloads + re-runs production setup, a fast no-op) — no admin reset, no restart (cold clones also `bench start` first to bring the stack up; a warm clone is already serving — see the in-guest deploy below). The guest then mints a one-click `login_url` (`bench browse --user Administrator --session-end`, a real 24h session) → stamped with `login_url_expires_at` on the Site. `status → Deploying`. | deploy seam |
   | 4 | `wait_for_http` — block on the guest's HTTP 200 (Contract B). | deploy seam |
   | 5 | Create the `Subdomain` row (this is what makes the proxy route it — its own `after_insert` reconciles the regional fleet). | this layer |
   | 6 | `status → Running`. | this layer |

   Any failure flips `status = Failed` and re-raises (fail loud, the job log
   carries the traceback). No-op if the Site has moved past `Pending`.

5. **`terminate()`** deletes the `Subdomain` (proxy stops routing on the next
   reconcile), terminates the backing VM, sets `Terminated`. Clears
   `subdomain_doc` before deleting the linked Subdomain (the link-integrity guard
   queries the DB, so the null is persisted first).

### Why clone-from-snapshot, not `image=`

The golden bench image is a **`Virtual Machine Snapshot`**
([08-images.md § golden bench image](./08-images.md)),
not a `Virtual Machine Image` catalogue row. The backing VM is **cloned** from it
(the snapshot carries `source_image` + the grown `disk_gigabytes`), so the
preinstalled bench + MariaDB + Redis come for free and `deploy-site.py` only does
the per-site work. Placement resolves the snapshot from
`Atlas Settings.default_bench_snapshot`; it fails loud when that is unset or not
`Available`.

The golden snapshot is a **durable artifact that outlives its build VM** — the
bake leaves the build VM as scratch and terminates it (and its row may later be
deleted entirely), but every self-serve site keeps cloning the golden
indefinitely. So `clone_to_new_vm` takes the clone's `server` from the snapshot's
own row (not the source VM) and reads the source VM only as a *sizing fallback*
when it still exists. The site VM is cloned at an **explicit** size — the
`Shared 4x` tier (2 GB / 0.25 core, `atlas.atlas.sizes`), via `Site._provision_backing_vm`
— rather than inheriting whatever the build VM happened to be, which both gives
the site the right tier and removes any dependency on the build VM surviving. The
tier is **2 GB, not the 512 MB `Shared 1x` entry tier**: the golden clone
auto-starts a full bench (MariaDB + Redis + gunicorn + workers), which at 512 MB
under a 1/16-core cap thrashes into swap so hard that even `deploy-site`'s
`wait_for_ssh` gate times out — the site never deploys. 2 GB matches the bake VM
the bench was built on (`bench_image` `GOLDEN_MEMORY_MB`); see the "~2 GB/site"
host-sizing note below.
(Before this, a clone after the build VM was gone failed with a raw
`DoesNotExistError` on the dangling `virtual_machine` link; it now fails loud with
a clear message only if a caller passes *no* sizing and the source is gone.)

### Warm-first provisioning

`Site._provision_backing_vm` is **warm-first**: the server choice still follows
the cold golden's row (above), but when that server carries an `Available`
`kind=Warm` snapshot (`placement.warm_bench_snapshot_for_server` — per-server,
because a memory snapshot only restores on the host it was captured on), the
clone **resumes** the pre-warmed golden instead of booting it
([05-virtual-machine-lifecycle.md → Warm snapshot fan-out](./05-virtual-machine-lifecycle.md#warm-snapshot-fan-out-one-golden-n-restored-clones)):
the site's backing VM is serving the baked `site.local` within low seconds of
provision, and only the per-VM rename + nginx-vhost regenerate remains. Warm is
**strictly an accelerator** with two independent degrade-to-cold layers: no warm row
on the
server → today's exact cold-clone path; a host that drifted under a stale row
(live migration, kernel/Firecracker upgrade) → `vm-restore.py`'s signature
guard cold-boots the warm disk, which still deploys correctly. A warm clone
restores at the **captured** vcpus/memory (the frozen vmstate pins them;
`clone_to_new_vm` rejects overrides) — only the tier's `cpu_max_cores` cgroup
cap is applied, so capacity accounting is unchanged.

## The in-guest deploy (`deploy-site.py`) *(built)*

The one piece that runs `bench` *inside* the guest. The controller side is
`atlas.atlas.deploy_site.deploy_site(vm, fqdn)`; the script is the committed
`bench/deploy-site.py`. It is the sibling of the golden-image bake
(`bench_image.build_bench`): drive an in-guest script over the **same
SSH-to-the-guest path** (`connection_for_guest`, the VM's public `/128` as root
with the fleet key), recording the op as a `deploy-site` Task row.

**What runs where** (two execution sites):

- `deploy_site` runs **in the guest**. The site VM is a *clone* of the golden
  snapshot taken after the bake's `/tmp` uploads were gone, so the deploy script
  is uploaded fresh per deploy (not assumed present), then run as root. It:
  1. **Pre-flights** — asserts bench-cli + the baked bench are present; a missing
     bench means the VM was cloned from the wrong snapshot, so it fails loud
     (unrecoverable, not retryable).
  2. **Cold clone only: production bring-up.** A freshly image-provisioned VM whose
     bench was never brought up runs an idempotent **`bench start`** first — brings
     the stack up via its lingering `systemctl --user` units. A **warm clone**
     (resumed from a memory snapshot — `--warm-vm-uuid` set) is already serving
     (`warm.sh` froze the stack up against `site.local`), so it skips this entirely.
  3. **Renames the baked site to the FQDN** — `bench rename-site site.local <fqdn>`,
     one bench-cli command (Contract A: the on-disk name now equals the proxy `Host`
     and the `Site` key). It moves `sites/site.local` → `sites/<fqdn>`, rewrites
     `bench.toml`, removes the stale `site.local.conf`, regenerates the vhost with
     `server_name <fqdn>` (on `listen 80;` + `listen [::]:80;`, both emitted by
     bench-cli — the edge proxy reaches the site over the VM's public **/128 (IPv6)**,
     the only inbound path, vm-inbound-ipv6-only), reloads nginx, and re-runs
     production setup for the new domain (idempotent — a fast no-op on a
     production-baked clone). No `default_server` catch-all (the old no-rename model
     needed one precisely because the on-disk name didn't match; the rename removes
     that need). The production gunicorn is **multitenant** (`frappe.app:application`,
     no `--site`), so it resolves the site from the request `Host` per request — the
     moment `sites/<fqdn>` exists and the vhost says `server_name <fqdn>`, the running
     workers serve it with **no restart**. Fails loud if neither the baked dir nor an
     already-renamed `<fqdn>` dir exists (a site-less snapshot). The setup-wizard gate
     is cleared at bake time; the db root password is baked + shared (08-images.md).
  4. **Mints the one-click `login_url`** — in **site** mode
     `bench browse --user Administrator --session-end <24h>` opens a real 24h
     Administrator session and builds `https://<fqdn>/app?sid=<sid>`; in **admin**
     mode `bench generate-admin-session --full-path` (a 5-minute single-use JWT,
     Pilot #117). The script emits it on its one `ATLAS_RESULT` line; the controller
     stamps it (+ `login_url_expires_at` = mint + the mode's TTL) on the doc. The
     `--regenerate-login` flag re-runs only this step (skips the rename/bring-up) so
     an expired URL can be re-signed on demand.
  - **No `set-admin-password`.** The tenant signs in via the minted `login_url`, not
    a password; the baked Administrator password is never surfaced (the tenant may
    rotate it later themselves). Resetting it per VM cost a full CPU-throttled
    `bench frappe` boot (~28s under the 0.25-core cap) that dominated the deploy;
    dropping it is the main latency win.
  - Idempotent (spec taste #14: retry = re-run): a re-run finds `sites/<fqdn>`
    already in place (the baked dir gone) and just re-asserts the vhost + serving.
- `wait_for_http` runs **on the controller** — see Contract B above. It runs
  *after* the rename, so it probes the FQDN `Host` against the new `server_name
  <fqdn>` vhost — the real south-hop path.

**Serving model.** The bench's own nginx is the in-guest front door on `:80`; the
**edge proxy** (12-proxy.md) routes `Host: acme.blr1.frappe.dev` → `[<vm-v6>]:80`,
where that nginx answers via the renamed **`server_name <fqdn>`** vhost, and the
multitenant gunicorn resolves the site from the `Host` per request. (The bake also
marks the vhost `default_server` so a pre-rename probe — the warm resume, before the
deploy renames — still answers off the baked `site.local`.) **TLS terminates at the
edge proxy, not in the guest** — there is no in-guest certbot; the south hop is
plaintext `:80` over public v6 (the accepted limitation under
[12-proxy.md § Accepted limitations](./12-proxy.md)). Baking the site past the
wizard and `setup production` *remove* the manual TLS/certbot steps a stand-alone
bench would need.

**Login-URL handoff.** The tenant is handed a one-click `login_url` — a real 24h
Administrator session minted in the guest by `deploy-site.py`
(`bench browse --user Administrator --session-end <24h>`, building
`https://<fqdn>/app?sid=<sid>`). It is stamped on the Site (plain `Small Text`,
read-only, `no_copy`) together with `login_url_expires_at` (mint + 1440 min) by the
orchestration *before* the readiness wait so it survives a later http-gate timeout,
and surfaced to Central (the `site.status_changed` Running event + `get_site` poll)
so the tenant clicks straight in. The baked Administrator password
(`Site.BAKED_ADMIN_PASSWORD`, in lockstep with build.sh's `BAKED_ADMIN_PASSWORD`) is
a randomized bake-time throwaway that still exists but is never surfaced — the tenant
rotates it later themselves; the db root password is never surfaced either
(single-tenant, localhost-only). When a stored URL lapses, Central re-mints it via
`regenerate_login_url()` (a whitelisted doc method that runs `deploy-site.py
--regenerate-login`, which skips the rename/setup and only re-signs). The
create_site path does no password work, which is what removed the ~28s `bench frappe`
boot.

## The Subdomain it creates

`auto_provision` step 5 inserts a [Subdomain](./02-doctypes.md#subdomain) whose
`subdomain` / `virtual_machine` flow straight from the Site — no
transformation (Contract A). Neither row carries a `region` field (Atlas is
single-region). The Subdomain is the proxy *map* row; the Site is
the tenant-owned aggregate. The Site stores the created Subdomain's name in
`subdomain_doc` so `terminate()` can drop it.

## Testing

- **Unit (milliseconds):**
  - *Site layer* — the routing-string validation (label/reserved/unique),
    immutability, the `auto_provision` state machine and its fail-loud path (host
    steps mocked at the module seams, incl. stamping the minted `login_url`), the
    `_create_subdomain` identity carry-through, and `terminate`. See
    `atlas/atlas/doctype/site/test_site.py`.
  - *Central API* — `create_site` get-or-creates the Tenant, stamps it on the
    Site, returns the mirror row, and gates the label;
    `get_site` hides the login handoff until Running. See
    `atlas/tests/test_api_site.py`. The `site.*` event reporting (created /
    status_changed, the Running handoff payload) is in `atlas/tests/test_central.py`.
  - *Deploy layer* — `wait_for_http`'s poll/timeout loop and 200-only
    predicate (the single probe mocked); the `deploy_site` upload + run +
    Task-record + fail-loud path (SSH transport mocked, no login_url minting); and the
    in-guest script's typed I/O (kebab-flag parsing, the one `ATLAS_RESULT` line,
    the rename + its idempotency/fail-loud, the v6-listener edit, the warm/cold
    branch). See `atlas/atlas/test_deploy_site.py`.
- **Host facts (e2e — `self_serve_site.py`):** the real `create_site` →
  golden-image clone + `deploy-site.py` (`bench rename-site` `site.local`
  → the FQDN, served for the FQDN `Host` on `:80`) → HTTP-200 readiness →
  Subdomain → an off-droplet `curl https://acme.<region domain>` over **both IPv4
  and IPv6** — proven on a real droplet, not in unit tests. It is the superset use
  case:
  reuses `proxy_vm`'s proxy + reserved-IP helpers, `tls_issuance`'s real
  LE-staging producer chain, and `bench_image`'s golden-snapshot bake (resolved
  from `Atlas Settings.default_bench_snapshot`, baked inline if absent). The
  `auto_provision` chain runs on the **background worker** (the same worker the
  VM-provisioning e2e relies on). It asserts the mirror row Central reflects and the
  Tenant stamp on the path. Like `tls_issuance` it owns its run (not in
  `run_all_smoke`) and skips cleanly (`MissingConfig`) when the e2e fixture has
  no `tls` block (`$ATLAS_E2E_CONFIG`, see the README), before anything billable. Split per the README "Host facts vs
  unit-covered logic" rule.
