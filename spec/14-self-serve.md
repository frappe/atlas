# Self-serve sites

Turns *signup → live Frappe site* into a few-seconds, self-serve flow: a user
picks a subdomain, and Atlas clones a golden bench VM, deploys a site into it,
and puts it behind the regional proxy at `acme.blr1.frappe.dev`. The proxy
([12-proxy.md](./12-proxy.md)) and TLS ([13-tls.md](./13-tls.md)) halves already
exist; this chapter is the **site layer** that drives them.

This chapter is the durable spec; the build history and planned-vs-actual drift
record live under [llm/plans/self-serve/](../llm/plans/self-serve/DRIFT.md). **Build status** is called out per section — the `Site` layer,
the in-guest deploy script + HTTP readiness probe (plan 03), and the
signup/verification surface (plan 04) are built and unit-green. The golden-image
bake (plan 01) and the end-to-end flow (plan 05) are **host-proven**: a golden
snapshot baked from scratch by `build.sh`, and a real signup → verify → cloned
golden site → deploy → live HTTPS through the proxy on IPv4 + IPv6 — the rename
model end to end. The deploy's `default_site` repoint (deploy step 3) was the one
host-only bug that run surfaced and fixed.

## The one routing string (Contract A)

One identity threads the whole system — never transformed between roles:

```
site-name-on-disk  ==  subdomain FQDN  ==  proxy Host header  ==  Site doctype key
                       e.g.  acme.blr1.frappe.dev
```

- The **subdomain label** (`acme`) is a single DNS label — **no dots** — so the
  site stays inside the one regional wildcard `*.blr1.frappe.dev` the proxy
  already terminates. A dotted label would escape the wildcard and need its own
  cert (deferred).
- The full FQDN is built once in `Site.autoname()` as
  `<subdomain>.<region domain>`, where the region domain comes from the single
  active [Root Domain](./02-doctypes.md#root-domain) — the same row that ties a
  region to its wildcard zone for TLS. That FQDN is the Frappe site name on disk
  in the guest, the Host header the proxy routes on, and the `Site` key.
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
  the **FQDN as the `Host` header** (Contract A), so the bench's multitenant nginx
  routes the probe to *this* site, not just "any site on the VM". It polls until a
  clean 200 (connection-refused / 502 are "not ready yet", swallowed) and raises
  `frappe.ValidationError` on timeout.

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

## The signup → verify → fulfil surface *(built — plan 04)*

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

**Admin handoff (plan 03/04).** After the Site reaches `Running`, the per-site
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
   | 3 | Run `deploy-site.py` in the guest: rename the baked `site.local` → `<fqdn>` + reset its admin password + `setup production` (nginx serves on `:80`). The per-site admin password → stored encrypted on the Site. `status → Deploying`. | plan 03 (seam) |
   | 4 | `wait_for_http` — block on the guest's HTTP 200 (Contract B). | plan 03 (seam) |
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

## The in-guest deploy (`deploy-site.py`) *(built — plan 03)*

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
  2. **Rename the baked `site.local` → `<fqdn>`** — `os.rename(sites/site.local →
     sites/<fqdn>)`. In bench-cli a site's identity *is* its directory name, so a
     directory move makes the on-disk site name the FQDN verbatim (Contract A) —
     no `bench new-site`, no DB rename (the db name travels in the moved dir's
     `site_config.json`). The slow schema-create + frappe-install is paid once at
     bake time, not per signup — see "Why rename" in
     [08-images.md](./08-images.md). Fails loud if the clone carries no baked
     `site.local` (i.e. was cloned from a site-less snapshot).
  3. **Repoint `default_site` → `<fqdn>`** (`sites/common_site_config.json`). This
     is **load-bearing on a real host**: bench-cli's `frappe serve` does *not*
     resolve the site from the `Host` / `X-Frappe-Site-Name` header on a
     snapshot-booted clone — `dns_multitenant` routing never engages for it, so
     every request falls back to `default_site`. The bake leaves
     `default_site = site.local`; after the rename that directory is gone, so
     without this step every request 404s *"site.local does not exist"* (the v4 +
     v6 paths both fail). A site VM is single-tenant (one site per VM), so pointing
     `default_site` at the renamed FQDN is both the fix and the correct model. Runs
     on every deploy (idempotent), including a re-run that skipped the rename, so a
     deploy that died mid-flight self-heals on retry.
  4. **Resets the Administrator password** — `bench frappe --site <fqdn>
     set-admin-password <pw>` against the just-renamed dir. The baked password is
     a shared throwaway and must never reach a user; the per-site password is
     generated by the controller (`frappe.generate_hash`) and passed as an argv
     flag over the encrypted SSH channel — never written to a guest file. The db
     root password comes from the baked `bench.toml` (shared + localhost-only;
     only the admin password varies per site — see [08-images.md](./08-images.md)).
     The setup-wizard gate is already cleared at bake time, so it is not re-set here.
  5. **`bench setup production`** — generates + reloads the bench's **own** nginx +
     supervisor config so nginx serves the site on `:80`. nginx + supervisor are
     baked into the golden image (plan 01), so this is config + reload, not an
     install. bench-cli sets `dns_multitenant`, but (per step 3) that does *not*
     actually drive Host-header routing on the clone — `default_site` is what
     resolves the site; this step brings nginx + supervisor up. It then **adds an
     explicit `listen [::]:80;`** beside each generated `listen 80;` and reloads:
     bench-cli's vhosts bind v4-only, but the edge proxy reaches the site over the
     VM's public **/128 (IPv6)** — the only inbound path (vm-inbound-ipv6-only) — so
     without the v6 listener the site serves on v4 and 404s on the path that matters.
     Finally it **restarts the bench web process** so gunicorn re-reads the new
     `default_site` (a `setup production` reload does not always recycle a running
     worker — proven needed on a real host).
  - Idempotent: a re-run that finds the site already renamed to the FQDN skips the
    rename but still re-points `default_site` and re-asserts serving.
- `wait_for_http` runs **on the controller** — see Contract B above.

**Serving model.** The bench's own nginx is the in-guest front door on `:80`; the
**edge proxy** (12-proxy.md) routes `Host: acme.blr1.frappe.dev` → `[<vm-v6>]:80`,
where that nginx matches the site by `server_name`. **TLS terminates at the edge
proxy, not in the guest** — there is no in-guest certbot; the south hop is
plaintext `:80` over public v6 (the accepted proxy-design limitation). Baking the
site past the wizard, the rename, and `setup production` *remove* the manual
TLS/certbot steps a stand-alone bench would need.

**Admin-password handoff.** The generated Administrator password is stored
encrypted in the `Site.admin_password` (`Password` field), written by the
orchestration *before* the readiness wait so it survives a later http-gate
timeout. It is shown once to the owner in the SPA (plan 04) so they can sign in;
the db root password is never surfaced (single-tenant, localhost-only).

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
    steps mocked at the module seams, incl. the admin-password storage), the
    `_create_subdomain` identity carry-through, `terminate`, and the owner-scoping
    permission contract. See `atlas/atlas/doctype/site/test_site.py`.
  - *Deploy layer (plan 03)* — `wait_for_http`'s poll/timeout loop and 200-only
    predicate (the single probe mocked); the `deploy_site` upload + run +
    Task-record + fail-loud path (SSH transport mocked); and the in-guest script's
    typed I/O (kebab-flag parsing, the one `ATLAS_RESULT` line, the on-disk
    idempotency predicate). See `atlas/atlas/test_deploy_site.py`.
  - *Status page (plan 04)* — `site_status.steps_for` maps each `Site.status` to
    the six-step checklist (Pending nothing-done, Provisioning both provision
    steps running, Deploying provision-done/deploy-running, Running all done,
    Failed deploy-phase failed, unknown status degrades without throwing). See
    `atlas/atlas/test_site_status.py`. The realtime push + owner-gating ride on the
    `auto_provision` and permission contracts already covered in the Site layer.
- **Host facts (plan 05 e2e — `self_serve_site.py`):** the real signup → verify →
  fulfil → golden-image clone + `deploy-site.py` (rename baked `site.local` +
  `setup production` actually serving on `:80`) → HTTP-200 readiness → Subdomain → an
  off-droplet `curl https://acme.<region domain>` over **both IPv4 and IPv6** —
  proven on a real droplet, not in unit tests. It is the superset use case:
  reuses `proxy_vm`'s proxy + reserved-IP helpers, `tls_issuance`'s real
  LE-staging producer chain, and `bench_image`'s golden-snapshot bake (resolved
  from `Atlas Settings.default_bench_snapshot`, baked inline if absent). The
  `auto_provision` chain runs on the **background worker** (the same worker the
  VM-provisioning e2e relies on). It also asserts the **Contract-C negative** on
  the real path: an unverified `Site Request` provisions no `Site` and no VM. Like
  `tls_issuance` it owns its run (not in `run_all_smoke`) and skips cleanly
  (`MissingConfig`) on a site without the `atlas_tls_*` keys, before anything
  billable. Split per the README "Host facts vs unit-covered logic" rule.
