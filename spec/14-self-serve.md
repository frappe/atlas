# Self-serve sites

Turns *signup → live Frappe site* into a few-seconds, self-serve flow: a user
picks a subdomain, and Atlas clones a golden bench VM, deploys a site into it,
and puts it behind the regional proxy at `acme.blr1.frappe.dev`. The proxy
([12-proxy.md](./12-proxy.md)) and TLS ([13-tls.md](./13-tls.md)) halves already
exist; this chapter is the **site layer** that drives them.

> **Status (Central pivot).** This self-serve signup flow is **Atlas-local and
> transitional**. Under the Central pivot ([16-central.md](./16-central.md)),
> customer signup, identity, and team membership move to Central; Central then
> drives site/VM creation in a region by calling Atlas's whitelisted methods as
> a service user, passing the `Tenant`. The flow below (the `/signup`/`/verify`
> on-ramp, local `Atlas User` creation) stays for this iteration and will be
> retired as Central takes over the front door.

This chapter is the durable spec — the whole self-serve layer is built and
**host-proven**: the `Site` layer, the in-guest deploy script + HTTP readiness
probe, and the signup/verification surface are built and unit-green; the
golden-image bake (`build.sh`) and the end-to-end flow are host-proven — a golden
snapshot baked from scratch, and a real signup → verify → cloned golden site →
deploy → live HTTPS through the proxy on IPv4 + IPv6. The per-VM deploy **renames**
the baked `site.local` dir to the FQDN and regenerates the bench's nginx vhost
(`bench setup nginx`: `server_name <fqdn>` + a v6 listener) and reloads — no admin
reset (the owner is handed the shared baked password and rotates it), no `setup
production`, no `bench restart`. The production gunicorn is multitenant (no
`--site`), so it resolves the renamed `<fqdn>` from the request `Host` header per
request and the rename + reload serve it live.

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

## Ownership / verification ordering (Contract C)

```
signup form → email verification → THEN Site row insert → verified user is `owner`
```

- **Email verification precedes the insert.** No droplet/site (billable) work
  happens for an unverified email — verification is the gate, so a typo'd or
  hostile address never triggers compute.
- The verified user is Frappe's built-in **`owner`** on the `Site` row — the same
  ownership model VMs/snapshots/SSH-keys use, scoped by
  `permission_query_conditions` → `atlas.atlas.permissions.owner_only` (`Site` ∈
  `_OWNED_DOCTYPES`). A user sees only their own Sites.

## The signup → verify → fulfil surface *(built)*

The public on-ramp inverts the order: the holding row first, the `Site` only
after the email is proven.

```
1. /signup form (guest)        → email + subdomain
2. request_site (guest API)    → Site Request (Pending, token)  + verification email
3. user clicks /verify?token=… → SiteRequest.verify()
4.   get-or-create User (Website User + Atlas User role)
5.   insert Site AS that user  → owner = user (Contract C)
6.   mark request Fulfilled, log the user in, redirect to /site-status?site=<fqdn>
7. Site.after_insert (above)   → provision → deploy → 200 → Running
8. /site-status                → live provisioning step view, then URL + admin password
```

- **`Site Request`** ([02-doctypes.md → Site Request](./02-doctypes.md#site-request))
  is the pre-verification holding row: email + subdomain + a `token`, status
  `Pending → Verified → Fulfilled` / `Expired`. It enforces the **same Contract-A
  label rules** as `Site` (shared `atlas.atlas.subdomain_label` — one source of
  truth for the label shape + reserved denylist), so a request can't reserve a
  name `Site` would reject. The token is valid 24h from creation.
- **`atlas.atlas.api.signup.request_site`** is the one guest-writable endpoint:
  it validates (email + the shared label rules), rejects a label already taken by
  a live `Site` (best-effort early feedback), caps outstanding `Pending` requests
  per email (3) and is IP/email rate-limited (5/hour), inserts the request
  (`ignore_permissions`), and queues the verification email
  (`templates/emails/site_verification.html`). Outbound email is an **operator
  prerequisite** (like the TLS controller-host deps) — with no email account
  configured the send is a no-op queue entry.
- **`atlas/www/verify.py`** (`/verify?token=…`, guest) looks the request up by
  token and calls `SiteRequest.verify()` — fulfilment is idempotent even under
  concurrency: `verify()` locks the request row `FOR UPDATE` and re-reads its
  status, so a link fetched twice at once (mail-scanner prefetch + the user's
  click) serializes — the second fetch sees `Fulfilled` and returns the same Site,
  never provisioning twice or double-inserting the `User` (which would race two
  `create_contact` jobs on `tabContact`). Throws a
  clean message on an expired token or a label taken since the request. On success
  it logs the user in (`login_manager.login_as`, the same path as Frappe's
  one-time login key) and redirects to `/site-status?site=<fqdn>` (the live
  provisioning view below — NOT the SPA machines list).
- **`atlas/www/site-status` page** (`/site-status?site=<fqdn>`, owner-gated) is the
  page the verified user lands on: a **live checklist of the six `auto_provision`
  steps** (clone → boot → deploy → respond → route → live), each shown done /
  running / pending / failed. The step view is derived from `Site.status` by the
  single source of truth `atlas.atlas.site_status.steps_for`, shared by the page's
  first render and the realtime payload. Updates are **pushed over realtime**
  (`Site.auto_provision` calls `frappe.publish_realtime("site_provisioning", …,
  user=owner)` on every transition) with a **slow polling fallback** (the
  whitelisted `site_status.progress`) so the view self-heals if a socket event is
  missed or the socket never connects. Once `Running`, it reveals the live URL +
  the one-time Administrator password (the admin handoff below). Owner-gated like
  every owned doctype: a non-owner or guest gets one neutral "not found or not
  yours" message — never another user's site or password.
- **Account model.** Fulfilment creates (or reuses) a real `User` — account-light:
  one verified account, one Site per signup, more Sites later through the SPA. The
  `Atlas User` role is **`desk_access = 0`** (the role fixture): a fulfilled user
  is a **Website User**, kept off Desk. (If the role ever drifts to desk access,
  Frappe would promote the user to System User — the fixture value is load-bearing.)

**Admin handoff.** After the Site reaches `Running`, the per-site
Administrator password stored encrypted on `Site.admin_password` is revealed on
the `/site-status` page the user is already watching (the reveal is
`site.get_password("admin_password")`, gated on `status == Running`). There is no
magic-login link; the handoff is that password + the live URL.

## The `Site` DocType *(built — this phase)*

Fields, validation, permissions, and the full field table are in
[02-doctypes.md → Site](./02-doctypes.md#site). The lifecycle:

1. **`before_insert`** validates the label (single dotless DNS label, not
   reserved), resolves `region` from the active `Root Domain`, sets
   `status = Pending`. `owner` is stamped by Frappe from the session user.
2. **`autoname`** builds the FQDN key (Contract A).
3. **`after_insert`** enqueues `auto_provision` (`queue="long"` — it SSHes).
4. **`auto_provision(site_name)`** — the background orchestration:

   | Step | Action | Owned by |
   | ---- | ------ | -------- |
   | 1 | Clone the backing VM from `Atlas Settings.default_bench_snapshot` (`Virtual Machine Snapshot.clone_to_new_vm` — carries the baked bench + grown disk). `status → Provisioning`. | this layer |
   | 2 | `wait_for_ssh` — the cloned VM booted. | existing |
   | 3 | Run `deploy-site.py` in the guest: rename the baked `site.local` dir to the FQDN + `bench setup nginx` (regenerate the vhost as `server_name <fqdn>` + a v6 listener) + reload — no admin reset, no restart (cold clones also `setup production` first to bring the stack up; a warm clone is already serving — see the in-guest deploy below). The owner is handed the shared baked admin password → stored encrypted on the Site. `status → Deploying`. | deploy seam |
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
the signup's backing VM is serving the baked `site.local` within low seconds of
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
     bench was never brought up runs **`bench setup production`** first —
     regenerates + installs + reloads the bench's own nginx + supervisor config and
     brings the stack up. A **warm clone** (resumed from a memory snapshot —
     `--warm-vm-uuid` set) is already serving (`warm.sh` froze the stack up against
     `site.local`), so it skips this entirely.
  3. **Renames the baked site to the FQDN** — `os.rename(sites/site.local →
     sites/<fqdn>)`, atomic and sub-millisecond (Contract A: the on-disk name now
     equals the proxy `Host` and the `Site` key). The production gunicorn is
     **multitenant** (`frappe.app:application`, no `--site`), so it resolves the
     site from the request `Host` per request — the moment `sites/<fqdn>` exists and
     the vhost says `server_name <fqdn>`, the running workers serve it with **no
     restart**. Fails loud if neither the baked dir nor an already-renamed `<fqdn>`
     dir exists (a site-less snapshot). The setup-wizard gate is cleared at bake
     time; the db root password is baked + shared (08-images.md).
  4. **Regenerates the nginx vhost** — `bench setup nginx` (NOT `setup production`):
     pure bench-cli config-gen — it scans `sites/`, finds the renamed `<fqdn>` dir,
     emits a vhost with `server_name <fqdn>` (matching the proxy's forwarded `Host`
     — the old no-rename model needed a `default_server` catch-all precisely because
     the on-disk name didn't match; the rename removes that need) and a
     `root .../sites/<fqdn>/public` files block, then `nginx -t`s and `systemctl
     reload`s. No Frappe boot, no process restart — sub-second. bench-cli only
     *writes* current sites' confs (never deletes stale ones), so the deploy removes
     the baked `site.local.conf` first. bench-cli's vhosts bind v4-only, but the edge
     proxy reaches the site over the VM's public **/128 (IPv6)** — the only inbound
     path (vm-inbound-ipv6-only) — so the deploy then adds `listen [::]:80;` beside
     `listen 80;` and reloads once more. No `default_server` (the `server_name
     <fqdn>` match is real now).
  - **No `set-admin-password`.** The owner is handed the shared baked Administrator
    password (rotated after first login); resetting it per VM cost a full
    CPU-throttled `bench frappe` boot (~28s under the 0.25-core cap) that dominated
    the deploy. Dropping it is the main latency win.
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

**Admin-password handoff.** The owner is handed the **shared baked** Administrator
password (`Site.BAKED_ADMIN_PASSWORD`, in lockstep with build.sh's
`BAKED_ADMIN_PASSWORD`) — the deploy no longer resets it per VM. It is stored
encrypted in `Site.admin_password` (`Password` field) by the orchestration *before*
the readiness wait so it survives a later http-gate timeout, and shown to the owner
in the SPA so they can sign in (and rotate it). The db root password is never
surfaced (single-tenant, localhost-only). Rotating the per-site password lazily
(first login / a background job) is deferred — the signup path does zero password
work, which is what removed the ~28s `bench frappe` boot.

## The Subdomain it creates

`auto_provision` step 5 inserts a [Subdomain](./02-doctypes.md#subdomain) whose
`subdomain` / `region` / `virtual_machine` flow straight from the Site — no
transformation (Contract A). The Subdomain is the proxy *map* row; the Site is
the user-owned aggregate. The Site stores the created Subdomain's name in
`subdomain_doc` so `terminate()` can drop it.

## Testing

- **Unit (milliseconds):**
  - *Site layer* — the routing-string validation (label/reserved/unique),
    immutability, the `auto_provision` state machine and its fail-loud path (host
    steps mocked at the module seams, incl. storing the baked admin password), the
    `_create_subdomain` identity carry-through, `terminate`, and the owner-scoping
    permission contract. See `atlas/atlas/doctype/site/test_site.py`.
  - *Deploy layer* — `wait_for_http`'s poll/timeout loop and 200-only
    predicate (the single probe mocked); the `deploy_site` upload + run +
    Task-record + fail-loud path (SSH transport mocked, no admin password); and the
    in-guest script's typed I/O (kebab-flag parsing, the one `ATLAS_RESULT` line,
    the rename + its idempotency/fail-loud, the v6-listener edit, the warm/cold
    branch). See `atlas/atlas/test_deploy_site.py`.
  - *Status page* — `site_status.steps_for` maps each `Site.status` to
    the six-step checklist (Pending nothing-done, Provisioning both provision
    steps running, Deploying provision-done/deploy-running, Running all done,
    Failed deploy-phase failed, unknown status degrades without throwing). See
    `atlas/atlas/test_site_status.py`. The realtime push + owner-gating ride on the
    `auto_provision` and permission contracts already covered in the Site layer.
- **Host facts (e2e — `self_serve_site.py`):** the real signup → verify →
  fulfil → golden-image clone + `deploy-site.py` (rename `site.local` → the FQDN +
  `bench setup nginx`, served for the FQDN `Host` on `:80`) → HTTP-200 readiness →
  Subdomain → an off-droplet `curl https://acme.<region domain>` over **both IPv4
  and IPv6** — proven on a real droplet, not in unit tests. It is the superset use
  case:
  reuses `proxy_vm`'s proxy + reserved-IP helpers, `tls_issuance`'s real
  LE-staging producer chain, and `bench_image`'s golden-snapshot bake (resolved
  from `Atlas Settings.default_bench_snapshot`, baked inline if absent). The
  `auto_provision` chain runs on the **background worker** (the same worker the
  VM-provisioning e2e relies on). It also asserts the **Contract-C negative** on
  the real path: an unverified `Site Request` provisions no `Site` and no VM. Like
  `tls_issuance` it owns its run (not in `run_all_smoke`) and skips cleanly
  (`MissingConfig`) on a site without the `atlas_tls_*` keys, before anything
  billable. Split per the README "Host facts vs unit-covered logic" rule.
