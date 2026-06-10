# Planned vs actual — self-serve implementation drift log

Tracks where the implementation diverged from the plans in this directory, and
why. Updated as each phase lands. "Planned" = what `0N-*.md` said; "Actual" =
what was built; "Why" = the reason for the divergence.

---

## Phase 01 — Golden bench image

### D01-1 — Bake strategy: build-in-guest + snapshot, NOT chroot-at-sync

- **Planned (01-golden-image.md "Decision"):** bake during sync — add a
  `bench_preinstall` flag to `SyncImageInputs` + the `Virtual Machine Image`
  doctype, and a chroot/systemd-nspawn bake step inside `sync-image.py` between
  unsquash and pack-ext4. Golden image = a from-URL `Virtual Machine Image` row.
- **Actual:** build-in-guest + snapshot. New `bench/build.sh` (committed tree
  beside `proxy/`), driven by `atlas.atlas.bench_image.build_bench` over
  guest-SSH (mirrors `proxy.build_proxy`); the built VM is stopped and
  snapshotted, and that `Virtual Machine Snapshot` is the golden image. No
  change to `sync-image.py` or `SyncImageInputs`; no new doctype field.
- **Why:** the codebase's proven precedent for preinstalling heavy software is
  the proxy's build-in-guest + snapshot (memory: "built-in-VM not
  custom-rootfs"). A chroot bake hits apt's MariaDB/Redis postinst, which expect
  a running init that a bare chroot lacks (would need a `policy-rc.d` deny shim +
  a capability `sync-image.py` has never had: executing code inside the rootfs).
  Building in a real booted guest sidesteps that and reuses the existing snapshot
  machinery. **Confirmed with the operator** before building.

### D01-2 — Image registration: clone-from-snapshot, not a new default image row

- **Planned (01 step 4):** register the golden variant in the image catalogue
  (`bootstrap.py` `DEFAULT_IMAGE`/`MINIMAL_IMAGE`) and consider making it
  `Atlas Settings.default_user_image` so placement lands self-serve Sites on it.
- **Actual:** the golden image is a `Virtual Machine Snapshot`; site VMs get it
  via `clone_to_new_vm` (the snapshot already carries `source_image` +
  `disk_gigabytes`). No `Virtual Machine Image` catalogue row, no
  `default_user_image` change yet.
- **Why:** follows from D01-1 — a snapshot is not an image row. **How plan 02
  consumes it:** `Site.before_insert` placement must resolve the golden *snapshot*
  (e.g. an `Atlas Settings.default_bench_snapshot` link) and the backing VM must
  be created via the snapshot clone path, not `image=`. Flagged for Phase 02.

### D01-3 — db secret decision (resolves 01 + 03 shared open question)

- **Planned (01 + 03 open question):** undecided — per-VM at provision vs
  generate at deploy.
- **Actual:** MariaDB `root_password` is baked, fixed, localhost-only in
  `bench/bench.toml` (single-tenant VM, db never crosses a VM boundary). The
  per-site Frappe **Administrator** password is generated per VM by
  `deploy-site.py` (Phase 03), never baked.
- **Why:** simplest correct split; documented in `bench/bench.toml` header and
  `spec/08-images.md`. **Phase 03 must honor it:** generate + return the admin
  password, do not touch the db root password.

### D01-4 — disk/memory bumped for the build VM

- **Planned (01 open question "Disk size"):** bump `default_disk_gb` for the
  variant; confirm thin-pool fit.
- **Actual:** the build VM (and therefore the snapshot and clones) uses
  `GOLDEN_DISK_GB = 12`, `GOLDEN_MEMORY_MB = 2048` (constants in the e2e module).
  The base `ubuntu-24.04` is 4 GB; `provision-vm` grows the per-VM rootfs to the
  larger `disk_gigabytes`.
- **Why:** a Frappe clone + uv venv + node deps overflow 4 GB. 12 GB is a first
  estimate — **to confirm on the real bake** (host fact; revise here if the bake
  shows it's too tight or wasteful).

### D01-5 — Bake the SITE too (`site.local`); deploy renames it, doesn't `new-site`

- **Planned (01 step 4 "Leave it stopped and site-less"; D01-3 "the site is
  per-VM, never baked"):** the golden image is site-less; `deploy-site.py` (Phase
  03) creates the site per-VM with `bench new-site <fqdn>`. The whole earlier
  design (D01-3, D03-1..4, the README/spec) rests on "bake the *bench*, not a
  *site*."
- **Actual (operator-requested):** `bench/build.sh` now also bakes a fully-created
  site under the fixed standard name **`site.local`** (past the setup-wizard
  gate). `bench/deploy-site.py`'s per-site step is no longer `bench new-site` — it
  **renames** the baked site (`os.rename(sites/site.local → sites/<fqdn>)`) and
  **resets** its Administrator password to the per-VM secret, then `setup
  production`. New helpers `_rename_site` / `_reset_admin_password` replace
  `_create_site` / `_mark_setup_complete`; `_preflight`/`_rename_site` fail loud if
  the clone carries no baked site (i.e. was cloned from the old site-less
  snapshot). The controller seams (`deploy_site(vm, site)`, `wait_for_http`) are
  **unchanged in signature**, so `site.py` + `test_site.py` are untouched.
- **Why it is sound (the load-bearing fact):** in bench-cli a site's identity *is
  its directory name* under `sites/` — `bench.sites()` builds each site from
  `d.name`, nginx's `server_name` is `site.all_domains == [dir_name]`, Frappe
  resolves the `Host` header to `sites/<host>/`, and the db name lives in that
  dir's `site_config.json` (so it travels with the move — **no DB rename**). So
  Contract A's "name on disk IS the FQDN, verbatim" is satisfied by a directory
  move. This moves the multi-minute `bench new-site` (schema create + frappe
  install + migrate) OFF the signup path — paid once at bake time — leaving deploy
  a sub-second move + an nginx regen.
- **D01-3 still holds:** only the db root password is baked + shared
  (localhost-only, single-tenant). The baked site's admin password is a throwaway
  reset per-clone, so the per-site-secret discipline is intact — the rename just
  changed *where* the slow create happens, not the secret model.
- **Spec/docs:** `spec/08-images.md` golden section (baked `site.local`, the
  rename model, the "why rename" paragraph), `bench/README.md` (layout + serving
  model), `bench/bench.toml` + `bench/build.sh` + `bench/deploy-site.py` +
  `atlas/atlas/bench_image.py` + `atlas/atlas/deploy_site.py` docstrings.
- **Tests:** `test_deploy_site.py` `TestGuestScriptTypedIO` gained
  `test_baked_site_constant_matches_build_sh`,
  `test_rename_site_fails_loud_when_baked_site_absent`,
  `test_rename_site_moves_baked_dir_and_resets_password`. The controller-driver
  tests are unchanged (signatures held).
- **Open / to verify on a host:** the bake's `bench new-site site.local` + the
  setup-complete `execute` calls, and the deploy's `os.rename` + `set-admin-password`
  against the renamed dir + `setup production` regenerating the vhost for the new
  `server_name`, are host facts proven only by a real bake + clone + serve. In
  particular confirm: (a) Frappe serves the renamed dir under the new `Host` with
  no `host_name`/cache residue from `site.local`; (b) `set-admin-password` resolves
  `--site <fqdn>` to the renamed dir; (c) nothing in the baked site_config pins the
  old name. **Requires a re-bake** of the golden snapshot (the per-deploy rename
  cannot work against an old site-less or `new-site`-style clone).

## Phase 02 — Site DocType + routing read API

### D02-1 — Region/domain resolved from `Root Domain`, not new region+domain config

- **Planned (02 "The model", Contract A):** `name = {subdomain}.{region}.{domain}`,
  treating `region` and `domain` as separate inputs the Site carries.
- **Actual:** the FQDN is `{subdomain}.{root_domain.domain}`, and `region` is
  read off the **single active `Root Domain`** row. `Root Domain`
  (`blr1.frappe.dev` ↔ region `blr1`) already is the frozen source of truth that
  ties a region to the wildcard zone the proxy fleet terminates (it owns the TLS
  cert). `Site.before_insert` resolves it via the new
  `placement.active_root_domain()`; `autoname()` builds `<subdomain>.<domain>`.
- **Why:** reuses the existing proxy/TLS seam instead of inventing parallel
  region/domain config, and keeps Contract A's "one routing string" honest — the
  domain a Site lands under is *exactly* the one the proxy already has a cert
  for. Atlas is single-region (`DigitalOcean Settings.region`), so "the single
  active Root Domain" is unambiguous; resolution fails loud on zero/many.

### D02-2 — `default_bench_snapshot` Atlas Settings link (consumes D01-2)

- **Planned (02 NOTE on D01-2):** placement must resolve the golden *snapshot*
  (e.g. an `Atlas Settings.default_bench_snapshot` link) and provision via the
  clone path.
- **Actual:** added `Atlas Settings.default_bench_snapshot`
  (Link → Virtual Machine Snapshot). `placement.default_bench_snapshot()`
  resolves + asserts it is `Available` (fail loud); `auto_provision` clones via
  `Virtual Machine Snapshot.clone_to_new_vm(title=fqdn, ssh_public_key=<fleet
  key>)`. No `Virtual Machine Image` row, no `image=` provision (D01-2 holds).

### D02-3 — Phase-03 host steps are module seams, fully unit-tested by mocking

- **Planned (02 "State machine"):** steps 3–4 (run `deploy-site.py`, wait for
  HTTP 200) are 03's contract; 02 owns the orchestration.
- **Actual:** `auto_provision`'s deploy + http-wait are thin module functions
  (`_deploy_site`, `_wait_for_http`) that import from the not-yet-built
  `atlas.atlas.deploy_site`. The whole state machine (clone → boot → Deploying →
  http → Subdomain → Running, and the fail-loud → Failed path) is built and
  unit-green by patching those seams. **Plan 03 must create
  `atlas.atlas.deploy_site` exposing `deploy_site(vm_name, site_name)` and
  `wait_for_http(ipv6_address)`** — the two seam imports — or adjust the seams to
  match what it builds.

### D02-4 — `subdomain_doc` Link + clear-before-delete on terminate

- **Planned:** terminate deletes the Subdomain, terminates the VM, marks
  Terminated.
- **Actual:** the Site stores the created Subdomain's name in a `subdomain_doc`
  Link field (so terminate knows which row to drop). Because Frappe's
  link-integrity guard queries the DB, `terminate()` **persists `subdomain_doc =
  None` (db_set) before** `frappe.delete_doc("Subdomain", …)`, or the delete
  hits `LinkExistsError`. Same clear-then-remove order used for the VM.

### D02-5 — `auto_provision` enqueue timeout is 1800s (not 300s)

- **Actual:** `after_insert` enqueues with `timeout=1800` (vs the VM/Subdomain
  jobs' 300s) because the Site job clones + boots a VM **and** runs the in-guest
  deploy + waits for the HTTP 200 — minutes of wall-clock, not seconds.

### Pre-existing test-DB pollution (not introduced here)

Two unrelated unit tests fail on the shared test site **independent of this
phase** (confirmed by running them on the base commit with Phase-02 changes
stashed): `test_placement.test_full_server_throws_at_default_factor` and
`test_virtual_machine.test_cpu_max_cores_defaults_to_vcpus`. Cause: prior e2e
runs left **committed** Active `Server` rows (and VMs) in the test DB, so the
capacity/cpu-default boundary assertions see room/state they didn't seed. The
Site fixtures deliberately leave their server **not** Active to avoid adding to
this. Fixing the pollution (a test-DB reset or scoping those tests to their own
seeded rows) is out of scope for Phase 02.

## Phase 03 — deploy-site.py + readiness gates

### D03-1 — Serving on :80 is bench-cli's OWN nginx (`setup production`), not a dev webserver or a custom systemd unit

- **Planned (03 step 5 + open Q):** `bench start` to serve on `:80`; the open
  question warned *against* in-guest nginx ("the proxy owns TLS") and flagged
  binding `:80` as undecided.
- **Actual:** `deploy-site.py` runs **`bench setup production`**, which turns on
  `dns_multitenant` (Host-header routing) and generates + reloads bench-cli's own
  **nginx + supervisor** so that nginx serves every site on `:80` by Host header.
  No `bench start`, no custom systemd unit.
- **Why:** the operator clarified the model — *bench-cli runs nginx on :80/:443
  in the guest, and we route the proxy's south hop to :80*. The plan's "no
  in-guest nginx" assumption conflated the **edge** proxy's TLS termination (still
  the only public TLS) with bench-cli's normal production serving. The in-guest
  nginx is plaintext `:80` only; **TLS still terminates at the edge proxy** (no
  in-guest certbot — that part of the open Q holds). bench-cli's `setup
  production` is whole-bench + idempotent, so it is safe to re-run after each
  `new-site`. `nginx + supervisor` are baked into the golden image (build.sh, plan
  01) so deploy is config+reload, not an apt install — adds a `[nginx]` section
  (`enabled`, `http_port = 80`) to `bench/bench.toml`.

### D03-2 — Admin password is generated controller-side and stored encrypted on `Site.admin_password`

- **Planned (03 open Q):** undecided — shown-once SPA / encrypted Site field /
  magic link, "decide with 02 and 04".
- **Actual:** the controller (`deploy_site`) generates the per-site Administrator
  password with `frappe.generate_hash(length=24)`, passes it to the guest as the
  `bench new-site --admin-password` argv flag (over the encrypted SSH channel,
  never a guest file), and **returns it**; `auto_provision` stores it in a new
  encrypted **`Password` field `Site.admin_password`**, written *before* the
  readiness wait so it survives an http-gate timeout. Shown once in the SPA is
  plan 04's job. The db root password is untouched (baked, D01-3).
- **Why:** the `Password` fieldtype is Frappe's at-rest-encrypted secret store and
  needs no new doctype; it matches the user-owned-doctype shape and the "stored on
  the Site row (encrypted)" option. **Adds `Site.admin_password`** (migrate-only
  field).

### D03-3 — `wait_for_http` gained a `host_header` arg; probes `/api/method/ping`, not `/`

- **Planned (02 seam D02-3 + 03):** `wait_for_http(ipv6_address)` — one arg;
  predicate "a `/` 200 past the setup-wizard gate".
- **Actual:** signature is `wait_for_http(ipv6_address, host_header, …)`. The Site
  seam `_wait_for_http(site, vm_name)` passes the FQDN as `host_header` so the
  bench's **multitenant** nginx routes the probe to *this* site (a bare `/` with no
  Host would hit the default vhost, not the site). The predicate is
  **`/api/method/ping` == 200** — Frappe's built-in unauthenticated method, 200
  once the web server is up *and* the site DB resolves, independent of the
  setup-wizard (the wizard only gates `/`, not the API). The fresh-site-setup-gate
  concern is still handled, but in **`deploy-site.py`** (it sets
  `Installed Application.is_setup_complete` so `/` serves the app for the owner),
  not in the readiness predicate.
- **Why:** the per-tenant Host header is load-bearing under `dns_multitenant`
  (D03-1); a Host-less `/` probe would be a false signal. `/api/method/ping` is the
  honest "Frappe is serving THIS site" gate and is simpler than asserting a
  wizard-state endpoint. **The 02 seam was adjusted** (D02-3 explicitly permitted
  this) — `site.py`'s `_deploy_site` now returns the password and `_wait_for_http`
  takes the Site; `test_site.py` updated to match.

### D03-4 — The in-guest script is self-contained, not an import of `scripts/lib/atlas/_task.py`

- **Planned (03):** "a frozen `DeploySiteInputs(TaskInputs)` dataclass … `emit()`
  … stdlib-only, lib under `scripts/lib/atlas/`."
- **Actual:** `bench/deploy-site.py` inlines a minimal `DeploySiteInputs` /
  `DeploySiteResult` (kebab-flag argparse, one `ATLAS_RESULT={json}` line) rather
  than subclassing the host `TaskInputs` — the guest has only Frappe + bench-cli,
  not the Atlas `scripts/lib`, and (like `bench/build.sh`) the `bench/` tree is
  self-contained, uploaded verbatim. It keeps the idiom's *shape*, not its import.
- **Why:** the script runs in a clone that never carries `scripts/lib`; uploading
  the lib just for two tiny dataclasses is more coupling than inlining them. The
  controller doesn't parse the `ATLAS_RESULT` line (it returns the password it
  generated), so the result is for the audit-trail/idempotency signal, not a
  controller contract.

### Open / to verify on a host (Phase 03)

- **`setup production` specifics are host facts (plan 05 e2e):** whether bench-cli's
  nginx listens on **`[::]:80`** (IPv6 — the proxy's south hop is v6-only), whether
  it apt-installs nginx/supervisor or assumes the baked ones, the exact
  `http_port`/`server_name` wiring, and that `is_setup_complete` via `bench frappe
  … execute` actually persists. All proven only against a real golden VM. Risk:
  if bench-cli's nginx binds v4-only, the south hop fails and the deploy must add
  an explicit `listen [::]:80` — revise here if so.
- **`bench frappe … execute` arg shape:** `execute` takes a dotted path + `--args`
  JSON and auto-commits; driving `frappe.db.set_value` by path (no inline body)
  is the verified form, but the auto-commit + the `Installed Application` filter
  `{"app_name": "frappe"}` are confirmed only on the real bench.

## Phase 04 — Signup + email verification

### D04-1 — Contract-A validators live in a shared module, not duplicated

- **Planned (04 "Reuse `Site`'s validators"):** factor the label/denylist checks
  into a shared helper so `Site` and `Site Request` enforce the same rules.
- **Actual:** new `atlas/atlas/subdomain_label.py` owns `RESERVED_SUBDOMAINS`,
  `validate_label`, `validate_reserved`, `normalize`, and `is_taken`. `Site`
  re-exports `RESERVED_SUBDOMAINS` (the spec + a test reference
  `site.RESERVED_SUBDOMAINS`) and its `_validate_label`/`_validate_reserved`
  delegate to the module; `Site Request.before_insert` and the signup API call the
  same functions. A test asserts both doctypes share the *same* frozenset object.
- **Why:** exactly the plan's intent; one source of truth so a request can never
  reserve a name `Site` would reject. `is_taken` is the best-effort early "taken"
  check the plan asked for (authoritative uniqueness is still `Site`'s FQDN key).

### D04-2 — `Site Request` is `autoname: hash`; the token is a separate field

- **Planned (04 field sketch):** `token` is "the verification secret"; the sketch
  was ambiguous about whether it is the row name.
- **Actual:** the row name is a random `hash`; `token` is a separate `Data` field.
  The URL carries the *token*, not the name — so the secret is decoupled from the
  key (a token could be rotated/expired without renaming the row, and the name
  never leaks the secret). Lookup is `frappe.db.get_value("Site Request", {"token":
  …})`.

### D04-3 — `owner` re-stamp uses `db_set` (it is a constant field)

- **Planned (04 step 5 + permissions):** "the *owner* of a request is the verified
  user — set it at fulfilment."
- **Actual:** the request is created by Guest (`owner = Guest`) and re-owned to the
  verified user in `verify()`. Frappe's `owner` ("Created By") is a **constant**
  field — assigning `self.owner` + `.save()` throws `CannotChangeConstantError`, so
  fulfilment uses `self.db_set("owner", user)` after the normal save. (The Site's
  owner is set the clean way — by inserting it *as* the user via `frappe.set_user`,
  the same proven pattern `test_site` uses, so the Site is stamped on insert and
  never needs a re-own.)

### D04-4 — `Atlas User` `desk_access = 0` is load-bearing for the account model

- **Planned (04 step 5.1):** create a `User` with `user_type = Website User`.
- **Actual:** the controller sets `user_type = "Website User"` on insert, but
  Frappe's `User.set_system_user` **recomputes** `user_type` from the user's roles:
  any role with `desk_access` promotes them to `System User`. So "the fulfilled
  user is a Website User" depends on the `Atlas User` role staying `desk_access =
  0` (the `role.json` fixture value). A unit test surfaced this: the test DB had
  the role drifted to `desk_access = 1`, flipping the new user to System User. The
  test now pins `desk_access = 0`; the production fixture is already correct.
- **Why:** documenting the coupling — if the role fixture ever gains desk access,
  self-serve users silently become Desk-capable System Users. The fixture value is
  part of the contract (noted in spec/14 + spec/11).

### D04-5 — Signup surface is a www page + guest API (not a Web Form), reusable account

- **Planned (04 "A. Signup form" open question):** Web Form **or** www page;
  account-vs-site model left to confirm.
- **Actual (confirmed with operator):** a server-rendered `/signup` www page
  (`atlas/www/signup.{html,py}`) posting to a guest whitelisted method
  (`atlas.atlas.api.signup.request_site`), and a reusable account (one verified
  `User`, one Site per signup, more Sites later via the SPA). Verification logs the
  user in (`login_manager.login_as`) and redirects to `/dashboard`. The email is a
  Jinja template under `atlas/templates/emails/site_verification.html` (a missing
  template is a hard `TemplateNotFound`, so it ships with the code).

### D04-6 — Throttle is two layers: IP/email rate-limit + per-email pending cap

- **Planned (04 "Throttle"):** rate-limit per email/IP; cap outstanding unverified
  per email.
- **Actual:** `@frappe.rate_limiter.rate_limit(key="email", limit=5, seconds=3600)`
  on the guest method (skips itself when there is no HTTP request, i.e. in unit
  tests — so tests call `request_site.__wrapped__`), **plus** an explicit
  `MAX_PENDING_PER_EMAIL = 3` check counting `Pending` rows for the email. The
  per-email cap is the abuse control that matters for fan-out and is unit-tested
  directly; the IP rate-limit is a request-context concern proven in the running
  app.

### Deferred to plan-04 SPA work (named, not half-built)

- **The user-facing Sites screen** (list own Sites + create-from-subdomain in the
  Vue SPA, and the **shown-once admin-password reveal** gated on `status ==
  Running`) is NOT built here. This phase delivers the *guest on-ramp* (signup →
  verify → fulfil → logged in at `/dashboard`) and the backend reveal
  (`site.get_password("admin_password")`). The in-SPA Sites page is the remaining
  plan-04 frontend slice — the permission layer + the password field already
  support it. Flagged so plan 05's e2e knows the reveal is backend-only today.

## Manual-run findings (2026-06-09, first real host signup test)

> **OUTCOME — the self-serve flow is PROVEN working end-to-end (2026-06-09).** A
> fresh Site (`golden2.atlas1.x.frappe.dev`) inserted → worker `auto_provision`
> drove clone→boot→deploy→200→Subdomain→Running with no intervention →
> off-droplet HTTPS through the proxy returned `{"message":"pong"}` over **both
> IPv4** (reserved IP `144.126.253.46`) **and IPv6** (proxy `/128`). All five code/
> image fixes below (M-1 settings, M-4 orchestration, M-4-followup timeout, M-5
> v6-vhost, M-6 golden) were required to get there. Golden image is the
> proven-VM snapshot `tpm31foak4`; the from-scratch `build.sh` bake is the one
> thing still unproven (M-6 follow-up).

### M-1 — The unit suite clobbers FOUR Atlas Settings fields, not the two earlier memory named

- Running the unit suite (e.g. Phase-06 verification) overwrites, on the shared
  test site, with test fakes: `Atlas Settings.ssh_private_key_path`,
  **`ssh_public_key`**, **`ssh_key_id`**, and `DigitalOcean Settings.api_token`.
  Earlier memory ([[atlas-real-provision-traps]]) named only the key path + token.
- Symptoms cascade and look like unrelated bugs: guest SSH fails
  `Permission denied (publickey)` / `invalid format` (key path = fake);
  `auto_provision` crashes `MandatoryError: ssh_public_key` when
  `_provision_backing_vm` clones (`site.py:219` reads the now-blank
  `ssh_public_key`); host ops fail auth (token = blank).
- **Restore ALL FOUR from site config before any real provision:**
  `ssh_private_key_path` → real abs path (config `atlas_ssh_private_key_path`,
  expand `~`), `ssh_public_key` → `<key>.pub` contents, `ssh_key_id` → the real
  DO key id (config `atlas_ssh_key_id`, e.g. `56640774`, not `key-id-123`),
  `api_token` → `db_set` (it is `set_only_once`, plain `.save` throws
  `CannotChangeConstantError`).

### M-2 — `auto_provision` must run on the WORKER; synchronous `bench execute` rolls back

- Driving `auto_provision` **synchronously** (`bench execute …auto_provision`)
  fails at `_wait_for_vm_ssh`: the clone's own `after_insert` enqueues the VM
  provision (boot) job, but with no worker in the sync context that job never
  runs, so the VM never boots, `wait_for_ssh` times out (~30s), the exception
  propagates, and `bench execute` **rolls the whole transaction back** — Site
  reverts to `Pending`, the cloned VM row vanishes, nothing is left to inspect.
- This is DRIFT D05-4 restated for the manual path: the chain is worker-driven by
  design. To re-drive a stuck `Pending` Site, **`frappe.enqueue(…auto_provision,
  queue="long", timeout=1800)` + commit** (exactly what `after_insert` does) and
  let the running worker process both it and the clone's provision job.

### M-4 — site.auto_provision deadlocked on its own clone (CODE BUG, fixed)

- **Symptom:** signup → request `Fulfilled`, but Site stuck `Pending`, **no VM, no
  Task**, and the URL shows bench-nginx "This site isn't here". Error Log: every
  failure was `DoesNotExistError: Virtual Machine <uuid> not found` thrown by
  `virtual_machine.auto_provision` (the clone's OWN boot job).
- **Root cause (design defect):** `site.auto_provision` cloned the backing VM and
  then called `_wait_for_vm_ssh` **in the same job/transaction**. But the clone
  only boots when ITS `after_insert`-enqueued `vm.auto_provision` job runs — a
  separate transaction that cannot start until the parent commits. The parent
  blocked on a boot that needed the parent to commit → `wait_for_ssh` timed out →
  `except` re-raised → **the whole transaction rolled back, deleting the clone VM
  row**. Seconds later the clone's boot job dequeued, couldn't find its VM, died
  `DoesNotExistError`; the parent's rollback also reverted the Site `Failed`→
  `Pending`. A deadlock by construction. The e2e never hit it because it polls
  `Site.status` from outside and lets the two worker jobs run (D05-4); the live
  controller did the clone + wait inline.
- **Fix (`site.py` `auto_provision`):** (1) `frappe.db.commit()` immediately after
  `db_set("virtual_machine", vm_name)` so the clone's boot job becomes live and
  runs; (2) replaced `_wait_for_vm_ssh` with `_wait_for_vm_running` — poll the VM's
  COMMITTED status to `Running` with `rollback()` (the proven `_tasks.wait_for_vm_running`
  shape) instead of SSH-waiting in an uncommitted txn; (3) in `except`,
  `db_set("status","Failed")` + `frappe.db.commit()` before re-raise, so the Failed
  status sticks instead of being rolled back to Pending.
- **Tests:** `test_site` 24→26 — added `test_commits_after_clone_so_boot_job_can_run`
  (asserts commit precedes the running-wait — the hand-off) and
  `test_failed_status_is_committed`. The orchestration tests now also
  `patch.object(site_module.frappe.db, "commit")` so the new real-path commits
  don't leak rows past IntegrationTestCase's rollback (would otherwise add to the
  D02 test-DB pollution).
- **Spec impact:** the contract in spec/14 ("auto_provision drives clone→boot→
  deploy→200→Subdomain→Running") is unchanged in shape; only the *mechanism* of the
  boot-wait changed (status-poll across a commit boundary, not inline SSH). No
  spec/14 text change needed — it never specified the inline-vs-poll mechanism.
- **Follow-up (boot-wait timeout, fixed):** after the deadlock fix, the first real
  run still hit `Failed: Backing VM … did not reach Running within 600s` — but the
  VM's `provision-vm.py` Task **succeeded ~26 min after** the wait gave up (the VM
  was `Running` by the time we looked). The clone+boot on a cold dev box (limited
  worker concurrency, a 12 GB clone, jobs queued ahead) far exceeded the 600s guess.
  Bumped `_wait_for_vm_running` default to **1500s** (fits inside `after_insert`'s
  1800s job timeout). The orchestration is correct; the ceiling was just too low.
  Watch on a real fleet whether 1500s holds or the boot should be its own job the
  parent re-checks rather than a single long synchronous poll.

### M-5 — Site served on IPv4 but 404'd on IPv6 — the path that matters (CODE BUG, fixed)

- **Symptom:** after the orchestration + timeout fixes, the deploy chain ran all
  the way through `bench new-site` + `setup production` (site created, healthy),
  but `_wait_for_http` failed: `HTTP 200 … :80/api/method/ping not seen after Ns`.
  The site was up — just unreachable on the path the proxy uses.
- **Root cause (golden-image / deploy defect, proven on the live guest):** v4 curl
  with the right Host → **200**; `[::1]:80` same Host → **404**. From `nginx -T`:
  bench-cli's per-site vhost (`config/nginx/sites/<fqdn>.conf`) emits a bare
  `listen 80;` — **IPv4-only** — while the **stock Ubuntu default vhost**
  (`/etc/nginx/sites-enabled/default`) holds `listen [::]:80 default_server;
  server_name _;`. So every v6 request hit the catch-all default → 404, never the
  site vhost. The edge proxy reaches each site over its public **/128 (IPv6 is the
  only inbound path)**, so the site was dead on the only path that counts while v4
  looked perfect. Verified the fix live: add `listen [::]:80;` to the per-site
  vhost + remove the default → `[::1]` and the public /128 both return 200 `pong`.
- **Fix (durable, two layers — the vhost is regenerated per `new-site`, the default
  ships with the apt package):**
  - `bench/build.sh` (bake-time): `rm -f /etc/nginx/sites-enabled/default`.
  - `bench/deploy-site.py` (deploy-time): `_enable_ipv6_listeners()` inserts
    `listen [::]:80;` beside each `listen 80;` in the generated site vhosts after
    `setup production`, then reloads nginx (idempotent: guarded on a presence check;
    layout-agnostic fallback to `config/nginx.conf`).
  - Also hardened `_serving()` to probe **`[::1]`** (not just `127.0.0.1`) so this
    bug class surfaces in-guest at deploy time, not only at the controller's
    `_wait_for_http` minutes later.
- **Why the e2e missed it:** worth watching — `self_serve_site` asserts inbound v6
  to the **proxy** (D05-3), but the proxy→site **south hop** had only ever been
  exercised against `proxy_vm`'s echo-server stand-in, not a real bench-cli vhost.
  A real `bench setup production` vhost was first exercised here, on the manual run.
- **Requires a re-bake** of the golden snapshot so new site VMs inherit the
  build.sh change (the deploy.py change applies per-deploy regardless).

### M-7 — Re-bake hung then failed: two host-SSH reliability bugs (both fixed)

- Re-running the bake under the rename-model `build.sh` (which adds the slow
  `bench new-site site.local` to bake time) **hung to the full 1800s timeout**,
  twice. Root-caused to **two** distinct, compounding bugs:
  1. **No SSH keepalive (CODE FIX).** `_ssh/transport.py SSH_OPTIONS` set
     `ConnectTimeout=30` (bounds only the *handshake*) but no `ServerAliveInterval`.
     A connection that went half-open *mid-command* blocked for the entire
     `timeout_seconds` instead of dying. Fix: `ServerAliveInterval=15` +
     `ServerAliveCountMax=4` (≈60s to give up). Benefits every long SSH op (bake,
     deploy, proxy build), not just this. ssh unit suites green (15+5). This turned
     the silent 1800s hang into a fast, legible failure — which exposed bug 2.
  2. **Recycled-IP stale host key (the [[atlas-real-provision-traps]] #1 trap).**
     With keepalive, the bake failed in 52s with `REMOTE HOST IDENTIFICATION HAS
     CHANGED … Offending key in ~/.atlas/known_hosts:74`. DO **recycled the `/128`**
     of a just-terminated build VM onto the new build VM, but with a new host key;
     `StrictHostKeyChecking=accept-new` **hard-fails on a *changed* key** (it only
     auto-accepts genuinely *new* hosts). The earlier 1800s hangs were almost
     certainly this same changed-key scp, hanging instead of erroring for lack of
     keepalive. Fix (manual, this run): `ssh-keygen -R <addr> -f ~/.atlas/known_hosts`
     for the recycled addresses before re-baking.
  3. **The build was a foreground child of the SSH session (CODE FIX).** Past the
     host-key wall, the bake ran build.sh for 162s then died on `Connection reset
     by peer / Broken pipe`. `build_bench` ran build.sh via one long `run_ssh`, so
     build.sh was a child of that SSH session — a mid-build connection reset
     **SIGHUP'd and killed the bake** (VM healthy before + after, no OOM, build just
     gone). Keepalive can't save this: it's a real reset, not a stall. Fix:
     `_run_detached_build` launches build.sh under **`setsid nohup`** (free of the
     session), tees to `build.log`, stamps `build.done` with the exit code, and
     **polls** for the marker over short, independently-retried SSH calls. A reset
     now fails one poll (retried next loop) while the build runs on. `bench_image`
     unit tests updated to the detached flow + assert `setsid`/`nohup` (5 green).
- **Durable follow-ups (NOT yet done):**
  - The provision/build path should `ssh-keygen -R` the target address
    **automatically** after creating a VM (or `wait_for_ssh` should treat a changed
    key as "recycled IP, re-pin"). Until then a recycled IP needs a manual `-R`.
    [[atlas-real-provision-traps]] #1 ("provision path arguably should `-R` first").
  - **`proxy.build_proxy` has the SAME foreground-build fragility** (one long
    `run_ssh` of build.sh) — it just hasn't been bitten yet (shorter build). It
    should adopt `_run_detached_build` (extract it to a shared `_ssh` helper).

### M-6 — Golden image re-bake flaked; pivoted to snapshotting the proven fixed VM

- After writing the M-5 v6-vhost fix into `build.sh`/`deploy-site.py`, the re-bake
  (`bench_image.run_smoke`) **failed operationally**: `build.sh` timed out at the
  full 1800s on a fresh build VM, and that VM had **no `/tmp/atlas-bench-build`** —
  the build never started (network egress was fine; the staged tree just wasn't
  there / the run made zero progress). Not a code bug — a flaky host/upload op, of
  the same family as the rest of this session's host friction.
- **Pivot (operator-approved):** instead of re-baking, snapshot the VM that was
  **already proven working** — `magicaldeploy`'s backing VM `32bc22d5`, which had
  bench + the live-verified v6 vhost fix + the default vhost removed and served 200
  on v6. Prep to golden: `bench drop-site` (→ site-less), removed the orphan
  per-site vhost + the `archived/sites/` backup, confirmed default vhost gone and
  nginx on `*:80`. Stop + snapshot → **`tpm31foak4` ("golden-bench-v2")**, set
  `Atlas Settings.default_bench_snapshot` to it (was `og2tcrubnu`, the pre-fix one).
- **Why this is sound:** the golden image only needs to carry the **bake-time**
  half of the M-5 fix (default vhost removed) + bench site-less; the **per-site
  v6 listener** is applied at *deploy time* by `deploy-site.py`'s
  `_enable_ipv6_listeners()` on every clone, so it doesn't need to be baked. This
  VM has the bake-time half and bench, so clones of it get a correct site.
- **Follow-up:** the durable `build.sh` change is committed but **never proven by a
  clean from-scratch bake** (the re-bake that would prove it flaked). Re-run
  `bench_image.run_smoke` on a fresh host when convenient to confirm `build.sh`
  bakes a correct golden from zero; until then the golden image is the
  proven-VM snapshot, which is functionally equivalent.

### M-3 — Stale e2e Subdomain rows masquerade as the new site's failure

- "This site isn't here" on the signup URL was NOT a TLS/proxy/site-name bug: the
  Site was stuck `Pending` (M-1 + M-2), so no real `Subdomain` row was created.
  The only Subdomain rows (`acme`, `mapped`, `another`) were **leftovers from the
  proxy-e2e run**, all pointing at the old e2e site VM. The proxy had no route for
  the new FQDN, so the request fell through to an empty bench → bench-nginx
  "site isn't here". Clean up stale e2e Subdomains when manually testing, or they
  confuse the diagnosis.

## Phase 05 — signup → live-site e2e proof

### D05-1 — One module, heavy reuse of three prior e2e modules (not a from-scratch superset)

- **Planned (05 "Shape"):** copy `proxy_vm.py` / `tls_issuance.py` as the closest
  templates; this module is "the superset."
- **Actual:** `self_serve_site.py` *imports and calls* the prior modules' helpers
  rather than re-deriving them — `proxy_vm._provision_proxy_vm`,
  `_allocate_and_attach`, `_assert_live_map`, `_teardown`; `tls_issuance._issue_certificate`,
  `_seed_tls_doctypes`, `_cleanup_tls_doctypes`, `_preflight_controller_deps`; and
  `bench_image._bake`. The new module's own code is just the signup→verify drive,
  the worker-wait, the Contract-C negative, the v6 inbound probe, and teardown of
  the new rows (Site / Site Request / User).
- **Why:** the proxy/TLS/bench-image facts are already proven by those modules;
  re-implementing them would duplicate (and drift from) proven code. The reuse
  makes this module a thin orchestration over a proven substrate — exactly the
  plan's intent, realized via import rather than copy.

### D05-2 — Golden snapshot is resolve-or-bake (operator decision), not assumed-present

- **Planned (05):** "the *site VM* must boot from 01's golden image (that's the
  point)" — silent on whether the e2e bakes it or requires it pre-baked.
- **Actual (confirmed with operator):** `_resolve_or_bake_golden_snapshot` uses
  `Atlas Settings.default_bench_snapshot` if it exists + is Available; otherwise it
  bakes one inline via `bench_image._bake(server)` on the shared droplet and writes
  the setting so `Site.auto_provision`'s placement resolves it. Fails clean if
  neither is possible (the bake itself raises on the shared droplet) — before any
  billable *site* provision.
- **Why:** the golden bake (plan 01) has never run on a real host (D01 open item),
  so requiring it pre-baked would make the e2e un-runnable until a separate manual
  bake. Resolve-or-bake makes the module self-sufficient on first run yet cheap on
  repeat runs (the snapshot is the artifact, kept).

### D05-3 — Off-droplet **IPv6** inbound is a NEW assertion (no prior module proves it)

- **Planned (05 host fact #3):** "Prove it over both the reserved IPv4 and public
  IPv6."
- **Actual:** `_assert_inbound_https("-6", proxy_vm.ipv6_address, fqdn)` is the
  first e2e assertion of **inbound** v6 to the proxy. `proxy_vm.py` proves inbound
  v4 (reserved IP) + the v6 **south hop** (proxy→site), but never an off-droplet
  client→proxy v6 request. nginx already listens `[::]:443` (proxy/conf/nginx.conf),
  and the proxy VM's public `/128` is reachable off-droplet (memory:
  vm-inbound-ipv6-only), so the path exists; this module is what exercises it.
- **Why:** the idea doc's "works on IPv4 and IPv6" is a self-serve-layer
  requirement the proxy module didn't carry. **Risk to watch on the host:** the
  controller's own network must have a working v6 route to the proxy /128 — if the
  e2e runner host is v4-only, the v6 probe fails for an environmental reason, not a
  product bug (the error message says so).

### D05-4 — `auto_provision` runs on the WORKER, waited via status-poll (not driven inline)

- **Planned (05 findings from 04):** "in a real (non-`in_test`) run on the droplet
  it runs automatically — confirm the worker is up."
- **Actual:** `_wait_for_site_running` polls `Site.status` with `frappe.db.rollback()`
  (to read the worker's committed per-step `db_set`s) until Running / Failed /
  deadline (1800s), mirroring `_tasks.wait_for_vm_running`. The whole clone → boot →
  deploy → 200 → Subdomain chain is the worker job; the e2e never calls
  `auto_provision` inline. On Failed/timeout it dumps the backing VM's recent Task
  rows so the operator sees where the chain stalled.
- **Why:** matches the harness's established contract — the VM-provisioning e2e
  already relies on the worker to flip a VM to Running, and the Site job is the same
  shape (just longer). **Precondition (documented in spec/14 + README):** the
  background worker must be running (and on macOS carry
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, the macos-worker-fork-crash trap).

### D05-5 — Single-active-Root-Domain shim (the live domain ≠ the TLS-config domain)

- **Planned:** not anticipated — the plan assumed seeding a Root Domain is enough.
- **Actual:** on the test site the active Root Domain is `blr1.frappe.dev` but
  `get_tls_config()` resolves `atlas_tls_domain = atlas1.x.frappe.dev`. Since
  `active_root_domain()` **throws on more than one active row**, the module
  `_quiet_other_root_domains(domain)` sets every *other* active Root Domain to
  `is_active=0` for the run and `_restore_root_domains` reactivates them in
  teardown. The seeded `<domain>` row is left active (seeding recreates it).
- **Why:** without this, `verify()` → `Site.before_insert` → `active_root_domain()`
  would throw "Several domains are active" the moment a real second domain exists.
  `tls_issuance` never hit this because it doesn't insert a `Site` (which is the only
  caller of `active_root_domain()` at fulfilment). Note: `_cleanup_tls_doctypes`
  *deletes* the seeded `<domain>` row in teardown (it doesn't restore it) — same as
  `tls_issuance`'s own teardown; the live `blr1.frappe.dev` is what gets restored.

### D05-6 — Readiness/inbound probe is `/api/method/ping`→`pong`, not proxy_vm's marker file

- **Planned (05 "smoke"):** mirror proxy_vm's stand-in-site marker check.
- **Actual:** the off-droplet HTTPS probes assert `"pong"` from
  `/api/method/ping` — there is no stand-in marker file because the upstream is a
  *real Frappe site*, not proxy_vm's `phase-proxy-start-site.sh` echo server. This is
  the same honest "Frappe is serving THIS site" signal the readiness gate uses
  (D03-3), reused at the off-droplet edge so the v4/v6 probes prove the full path
  end to end (proxy TLS → south hop → Frappe), not just "something answered".
- **Why:** the real site has no marker to echo; `pong` is the multitenant-routed,
  Host-header-correct success token that already gates readiness, so it is the right
  edge assertion too.

### Open / to verify on a host (Phase 05)

- **The whole e2e has not been run on a real droplet yet.** It imports cleanly, all
  reused seams resolve, the preflight/skip discipline is verified
  (`get_tls_config` + `_preflight_controller_deps` reachable, Root-Domain quiet/
  restore is a pure read/write pair), and the unit suites for every layer it drives
  are green (Site 24, signup 7, deploy_site 11). But the host facts — golden clone
  serves, worker-driven Running, v4+v6 inbound — are proven only by an actual
  billable run on the operator's turn. Carries every Phase-01/03 host open item
  (the bake, `setup production` binding `[::]:80`, `is_setup_complete` persisting)
  plus the D05-3 v6-route-from-the-runner risk.
- **Run it (operator turn, billable):**
  `bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.self_serve_site.run_smoke`
  — and grep the log for FAIL (run_smoke exits 0 even on a failed assertion path
  inside a swallowed step; the module raises on real failures, but the lvm-traps
  habit of grepping the log still applies).

## Phase 06 — spec & docs surfaces (cross-cutting)

### D06-1 — Roadmap close was 3 entries, not 1 (proxy + TLS backfilled with self-serve)

- **Planned (06 FINDINGS):** the one remaining 06 gap is to "move self-serve into
  a shipped Changes entry" in `spec/09-roadmap.md` (and confirm there's no
  deferred self-serve line to remove — there wasn't).
- **Actual:** the `Changes` log was stranded at **`v0.6`** (LVM thin-pool), while
  **three** shipped chapters had no entry — 12 (proxy), 13 (TLS), 14 (self-serve).
  Adding only self-serve would have jumped `v0.6 → v0.9`-equivalent with proxy
  and TLS silently absent. Backfilled all three terse entries in the existing
  v0.1–v0.6 style: **`v0.7` Reverse proxy** (→ 12-proxy.md), **`v0.8` TLS &
  domain layer** (→ 13-tls.md), **`v0.9` Self-serve sites** (→ 14-self-serve.md).
- **Why:** 06's stated intent is "the spec reflects shipped reality, nothing
  missed." The plan author was scoped to the *self-serve* slice and didn't see
  the proxy/TLS changelog gap (those shipped under their own phases, which never
  added their roadmap Changes lines). Fixing only self-serve would have left a
  worse inconsistency than the one being fixed. **Operator-approved** before the
  edit (chose "backfill all three").

### D06-2 — Everything else 06 listed was already on disk (audit, no edit)

- **Planned (06):** a table of spec files to update as each plan lands.
- **Actual:** the Phase-06 audit verified each claimed-done surface against disk —
  `spec/14`, `README` (read-order #14 + Testing + entry-points), `02-doctypes`
  (count 22, Site/Site Request), `11-user-ui` (perm rows + signup on-ramp),
  `08-images` (golden-image §). All present and correct; **no edits needed** —
  the slices genuinely landed alongside plans 01–05 as the FINDINGS block claimed.
- **Why:** the per-plan definition-of-done ("update your spec slice as you land")
  worked — 06 was a verification pass plus the one roadmap close, not a backlog.

### D06-4 — Found + fixed a pre-existing stale test stub (test_letsencrypt `_StubDns`)

- **Found during 06 verification (not a 06 change):** the full unit run surfaced
  `Can't instantiate abstract class _StubDns without … 'upsert_wildcard'`. Root
  cause: the wildcard-DNS work ([[atlas-wildcard-dns]]) added `upsert_wildcard` as
  a 4th `@abstractmethod` on `DnsProvider`, but the `_StubDns` fixture in
  `atlas/atlas/tls/test_letsencrypt.py` was never updated — so it failed to
  instantiate (`test_letsencrypt` errored 2/3 even **standalone**).
- **Fix (operator-approved, kept):** added `upsert_wildcard(self, domain, targets)
  -> []` + the `WildcardTargets` import to the stub. Test-only, one method.
  A scan of all `DnsProvider`/`TlsProvider` test stubs confirmed this was the
  **only** drifted one (the two stubs in `dns/test_registry.py` already had it).
- **Why it matters here:** it's a TLS-layer bug, not self-serve's, but 06's
  verification is what caught it; left unfixed it errors `test_letsencrypt`
  standalone and cascades under queue pressure. Logged so the TLS phase knows it's
  already closed.

### D06-5 — Full-suite failures are environmental (queue + test-DB pollution), not code

- **Observed:** the full `run-tests --app atlas` showed 100+ errors; **every
  affected suite passes standalone.** Two independent environmental causes, both
  pre-existing and unrelated to 06:
  1. **`QueueOverloaded`** — the dev bench's `long` RQ queue had **599 stuck
     test-created `auto_provision` jobs** (no worker drains them during unit
     runs), tripping Frappe's 600-job enqueue guard the moment enough
     VM-inserting `setUp`s ran. Drained by the operator → cascade gone.
  2. **Test-DB pollution (DRIFT D02 family)** — 5 residual FAILs from committed
     Active `Server`/`VM`/`TLS Provider` rows left by past e2e runs:
     `test_placement.test_full_server_throws_at_default_factor`,
     `test_virtual_machine.test_cpu_max_cores_defaults_to_vcpus`,
     `test_api_server_capacity.test_used_vcpus_sums_non_terminated_vms`,
     `tls_certificate.test_tls_provider_denormalized_from_domain`, and the
     SPA-build-empty `test_website_route.test_logged_in_user_gets_context`
     ([[atlas-spa-build-test-gate]]). **Proven pre-existing** by re-running with
     the 06 fix stashed (still fails identically). Not 06's to fix; a test-DB
     reset is the documented remedy (D02).
- **Net after queue drain:** `Ran 438, 0 errors, 5 known-environmental failures` —
  all self-serve + TLS suites green in the full run.

### D06-3 — Companion doc trimmed to a pointer + host-bound remaining

- **Planned (06):** trim `self-serve-parallelism.md` to remaining-only once the
  work lands (the way `proxy-design.md` was trimmed after the proxy shipped).
- **Actual:** replaced its stale "Tracks to build" / "Contracts to freeze" body
  with a pointer header (→ `spec/14`, `plans/self-serve/`, `DRIFT.md`) and a
  "Remaining (host-bound, not yet proven)" section carrying the open D01/D03/D05
  items (golden bake, `[::]:80`, `is_setup_complete`, the unrun e2e). To delete
  once the host run lands.

## Phase 01 — open items (carried)

### Open / to verify on a host (Phase 01)

- The bake (`build.sh`) has **not** been run on a real droplet yet — unit tests
  cover the controller's pure parts (upload mapping, Task recording) and
  `build.sh` passes `bash -n`, but the apt/clone/uv/node bake is a host fact
  proven only by `bench_image.run` on a real server (deferred to the host-bound
  run, like every other e2e). Risks to watch: `bench init` Node install time;
  whether `bench init` needs interactive input (the build runs non-interactive);
  the 12 GB disk sizing; whether bench-cli's `bench init` on Python 3.14 in the
  guest matches the controller (memory: py3.14-except trap — remote droplets may
  run older Python, but here bench.toml pins `python = "3.14"` and bench-cli
  builds its own venv, so this is contained).

## Live host run (2026-06-10) — rename model proven end to end + the one bug it found

> **OUTCOME — the rename model is now HOST-PROVEN from a clean slate.** On a fresh
> `atlas.tests.local` (empty: no server, no snapshot, no settings) the full flow
> was stood up and a real signup driven through: bootstrap → **golden image baked
> from scratch by `build.sh`** (snapshot `m6tuh2lmou`, 434s — the from-scratch bake
> M-6 left unproven) → proxy VM + reserved IPv4 + real LE-staging wildcard cert →
> `request_site` → `verify()` → Site cloned from the golden snapshot → boot →
> `deploy-site.py` → HTTP 200 → Subdomain → proxy reconcile → off-droplet HTTPS on
> **both v4 (reserved IP) and v6 (proxy /128)**. Closes HANDOFF G1 (the re-bake)
> and G2 (re-prove the rename model) — both of which the prior session could not
> land (it fell back to a hand-fixed snapshot under the OLD new-site model).

### L-1 — The rename `default_site` bug (CODE BUG, fixed) — the one host-only defect

- **Symptom:** the deploy ran clean (dir renamed `site.local`→`<fqdn>`, admin pw
  reset, `setup production` + the v6 listener all applied, `created: true`), but
  `_serving` and the controller's `wait_for_http` never saw a 200 — every request
  404'd **`site.local does not exist`** on *both* v4 and v6. So the Site timed out
  at the readiness gate and flipped `Failed`; `auto_provision` never reached step 5,
  so **no Subdomain was created and the proxy had no route** (which read as "the
  proxy isn't routing" — it was, there was just nothing to route to).
- **Root cause (proven live):** bench-cli's `frappe serve` does **not** resolve the
  served site from the `Host` / `X-Frappe-Site-Name` header on a snapshot-booted
  clone — `dns_multitenant` routing never engages for it. Every request falls back
  to `default_site`, which the bake leaves as `site.local`. After the rename that
  dir is gone → 404. Confirmed by hitting gunicorn directly on `:8000` with an
  explicit `X-Frappe-Site-Name: <fqdn>` header: still 404 `site.local`. This is
  exactly DRIFT D01-5 open item (a) ("no `host_name`/cache residue from
  `site.local`") — the residue is `default_site`.
- **Fix (`bench/deploy-site.py`):** new `_set_default_site(<fqdn>)` rewrites
  `default_site` in `sites/common_site_config.json` to the FQDN, called in `main()`
  on **every** deploy (idempotent — also runs on a rename-skipping re-run so a
  half-completed deploy self-heals), plus `_restart_web()` after `setup production`
  to recycle gunicorn so it re-reads the new `default_site` (a `setup production`
  reload does not always restart a running web worker — also proven needed live).
  With the fix, the site serves 200 `pong` on v4 + v6 and the user's own fresh
  signup went green end to end through the proxy.
- **No re-bake needed:** `deploy-site.py` is uploaded fresh per deploy (not baked),
  so the fix applies to every clone of the existing golden snapshot.
- **Spec/docs:** `spec/14` deploy steps (added step 3 "repoint default_site",
  corrected the `dns_multitenant` claim, added the web restart) and `spec/08`
  ("Why rename" now states the `default_site` host fact). `test_deploy_site.py`
  gained `test_set_default_site_repoints_to_fqdn` (15 green).

### L-2 — Bootstrap gaps the run surfaced (fixed)

- **`ssh_public_key` was unset** → the Site clone path (`_provision_backing_vm`
  reads `Atlas Settings.ssh_public_key` for `clone_to_new_vm`) would have thrown
  `MandatoryError` on the first signup. `bootstrap.py` only set it from an explicit
  `atlas_ssh_public_key` config key (rarely present). **Fix:** `_resolve_fleet_public_key`
  derives it from `atlas_ssh_private_key_path` (`ssh-keygen -y`) when the config key
  is absent, wired into `ensure_provider` + `restore_credentials`. A key-path-only
  bootstrap now stands up signup with no extra config.
- **`run_with_self_serve` doc** now spells out the three dev steps (bake golden →
  run bootstrap → stand up a proxy) and the ~2 GB/site RAM budget, so another dev
  can reproduce the flow.

### L-3 — Host RAM caps concurrent sites (operational, not a bug)

- On the shared dev droplet, running the proxy VM + multiple ~2 GB site VMs + the
  build VM overcommits RAM; gunicorn in a site VM gets OOM-killed and nginx then
  answers **502** (the proxy faithfully proxies that 502 — again, the proxy is
  fine). This is a host-sizing fact, not a code defect: size the host for the
  number of concurrent sites, or terminate the build VM after baking.
