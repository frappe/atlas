# Atlas ↔ Satellite dedup migration — remove Atlas's routing/TLS/DNS stack, rewire Site/Pilot

Status: DESIGN (read-only draft, pending adversarial review + user approval). Supersedes
`phase-4-service-install.md`'s "keep two writers forever" conclusion. Confirm that reversal
before executing (see Assumption A0).

Atlas branch `feat/vm-service-seam`; Satellite branch `main`. Two separate git repos.
Satellite already has the whole guest plane live e2e (routing/proxy/tcp/dns/tls/site).
This plan REMOVES Atlas's duplicate copy and DELEGATES Site/Pilot routing/TLS/DNS to
Satellite. Site + Pilot doctypes STAY in Atlas (Central seam untouched).

--------------------------------------------------------------------------------
## A. Assumptions (flag every one)
--------------------------------------------------------------------------------
- A0 (decision reversal): The user's 2026-07-16 directive overrides `phase-4-service-
  install.md`. Site/Pilot routing/TLS/DNS moves to Satellite; the transient two-writer
  state is NOT the steady state. CONFIRM.
- A1: Central must NOT be modified. `api/site.py::create_site`, `central_report.py`,
  `central_link.py`, `pilot_credential_id`, the `Site`/`Pilot` doctype JSON schemas, and
  the `_mirror`/`get_site` wire shapes stay byte-identical to Central.
- A2: `Server` and `Virtual Machine` in Satellite are intentional mirrors — untouched.
- A3: `build_mode` + Image Build stay in Atlas (provisioner image attribute; already on
  the read API per commit 19d1a3d). Not part of this dedup.
- A4: `deploy_site.py` + `bench/deploy-site.py` (the guest install) is Satellite's per
  `phase-4-service-install.md` §"The seam". This plan assumes Satellite's `services/
  site.py`+`bench.py` (already present) are the live install path and Atlas's copy is
  removed here.
- A5: One Atlas ⇄ one Satellite in dev; the plan is multi-Atlas-safe but only verifiable
  N=1.
- A6: `active_root_domain()` (region + FQDN suffix) is Atlas-owned state that Site/Pilot
  read to build the FQDN. Satellite's equivalent is `Region Domain`. The FQDN must be
  computed identically on both sides (same label + same region domain) or routes mismatch.

--------------------------------------------------------------------------------
## B. Dependency / coupling map (every Atlas reference)
--------------------------------------------------------------------------------
Legend: [DELETE] leaves Atlas · [KEEP] stays · [REWIRE] behavior changes · [EXTRACT] move
helper to a neutral module.

### B1. The 6 duplicated doctypes (dirs under atlas/atlas/doctype/)
- `subdomain/` [DELETE] — controller creates the proxy map + `subdomain_map()` +
  `auto_reconcile()` (imports `proxy.reconcile_proxies`).
- `custom_domain/` [DELETE] — references `atlas.atlas.proxy` reconcile.
- `port_mapping/` [DELETE] — imports `atlas.atlas.tcp_proxy.reconcile_proxies`.
- `tls_certificate/` [DELETE] — `issue`/`push_to_proxies`/`_publish_wildcard`, imports
  `dns, proxy, tls`; `renew_expiring` daily scheduler entrypoint.
- `route53_settings/` [DELETE] — imports `atlas.atlas.dns`.
- `lets_encrypt_settings/` [DELETE] — read by `tls/letsencrypt.py`.
- ALSO transitively coupled (same stack):
  `root_domain/` [DELETE] — `issue_certificate()`; read by `placement.active_root_domain`,
    `atlas_settings.active_region`, `tls_certificate`, `virtual_machine._deprovision_proxy`,
    `setup.py`, `bootstrap.py`. Deleting Root Domain removes `active_root_domain()`'s
    backing table — Site/Pilot + `api/site.py::check_subdomain` depend on it (see D7).
  `tls_provider/`, `domain_provider/` — already collapsed to Settings; only referenced by
    migrate patches (guarded). Leave the dirs if still present; do not un-guard patches.

### B2. Service modules (atlas/atlas/*.py)
- `dns/` (`__init__` registry, `base`, `route53`, tests) [DELETE] — sole callers: `tls/*`,
  `route53_settings.py`, `tls_certificate.py` (all leaving).
- `tls/` (`__init__`, `base`, `letsencrypt`, `zerossl`, `self_managed`, tests) [DELETE]
  — callers: `tls_certificate.py`, `root_domain.py`, `desk_buttons`/`test_tls_certificate`.
- `proxy.py` [SPLIT]:
  - [DELETE] the routing half: `reconcile_proxies`, `reconcile_proxy`, `_desired_maps`,
    `read_live_maps`, `_reconcile_proxy`, `_sync_map`, `push_cert`, `wildcard_targets`,
    `_proxy_vms`, `canonical_json`, `REGION_FILE`, `_point_cert_symlink_command`,
    `regenerate_placeholder_cert`.
  - [KEEP in Atlas] `build_proxy` (provisioner image build — used by `bootstrap.ensure_proxy`,
    `image_recipes`) AND the generic guest-SSH helpers `_record_guest_task`, `_remote_parent`,
    `_write_guest_file`, `_curl_command`.
  - [EXTRACT] the guest-SSH helpers to a neutral module (proposal: `atlas/atlas/guest_ssh.py`)
    because `deploy_site.py:41` (`from atlas.atlas.proxy import _record_guest_task,
    _remote_parent`) and `image_builder.py:29` import them and both STAY in Atlas. Do this
    BEFORE deleting proxy's routing half so importers never dangle.
- `tcp_proxy.py` [DELETE] — imports `proxy._proxy_vms/_record_guest_task/canonical_json`;
  sole non-test caller is `port_mapping.py` (leaving). `test_tcp_proxy.py` [DELETE].
- `subdomain_label.py` [KEEP] — imported by `site.py`, `pilot.py`, AND `api/site.py`
  (Central seam). MUST stay. (Satellite has its own copy at `routing/labels.py`.)
- `deploy_site.py` [KEEP or DELETE per A4] — if Site/Pilot no longer deploy from Atlas,
  Atlas's `deploy_site.py` loses its callers and can be deleted last. Its `proxy` import
  must first be repointed to `guest_ssh.py`.

### B3. Wiring files
- `atlas/hooks.py`: `doctype_js` for Route53/Lets Encrypt/Root Domain/TLS Certificate
  [DELETE lines]; `scheduler_events.daily` → `tls_certificate.renew_expiring` [DELETE];
  `doc_events` for Site/Pilot → Central reporting [KEEP]; VM satellite_events [KEEP — the
  trigger channel].
- `atlas/setup.py`: `setup_tls_layer`, the `tls` config block, `_stage_tls`,
  `_acme_directory_from_env` — seed Route53/LE/Root Domain + provider-type Settings fields
  [DELETE/REWIRE → Satellite setup]. `test_setup.py` asserts them [UPDATE].
- `atlas/bootstrap.py`: `issue_certificate`, `push_certificate_to_proxies`, `ensure_proxy`
  (calls `proxy.build_proxy` [KEEP] + reserved IP), `setup_tls_layer` call, and the
  self-serve orchestration that issues+pushes certs [REWIRE] — TLS/cert-push/wildcard-DNS
  move to Satellite or drop; `build_proxy` + reserved-IP stay (provisioner). Large surface.
- `atlas/atlas/reset.py`: `_db_footprint`/`_delete_db_rows`/`_count_linked` delete
  `Subdomain` + `Site` rows. [REWIRE] — drop Subdomain once it leaves; `Site` stays.
- `atlas/atlas/migration.py`: `_repoint_routes` rewrites `Subdomain.address` + calls
  `proxy.reconcile_proxies` (call sites L908, L1169). [REWIRE/DELETE] — post-split Atlas
  can't re-point Satellite's routes; Satellite's converging reconcile re-derives from the
  mirror (D6).
- `atlas/atlas/doctype/virtual_machine/virtual_machine.py`: `read_proxy_maps` [DELETE];
  `_deprovision_proxy` [DELETE]; `_delete_subdomains`/`_clear_subdomain_references` [DELETE];
  `_delete_custom_domains` [DELETE]; terminate() calls to those four [DELETE]; `is_proxy`
  field + `_publish_wildcard` read [DELETE]. (`is_gateway`/host-mesh are Phase-1, out of scope.)
- Migrate patches (`atlas/patches/v1_0/*`) reference these doctypes but are ALL guarded by
  `table_exists`/`has_column`/`exists("DocType", …)` [KEEP UNCHANGED] — see R1.

### B4. Central seam (MUST NOT break, cannot touch Central)
- `api/site.py::create_site` → inserts `Site` → after_insert → auto_provision.
  `check_subdomain`/`get_site`/`_mirror` read `active_root_domain()` + Site fields.
- `central_report.py`: `on_site_*`, `on_pilot_update`, `report_site_status`,
  `report_pilot_status`, `_pilot_vm_payload` — read Site/Pilot `login_url`, `status`,
  `subdomain`. [KEEP] — the rewire must preserve these fields on the Atlas rows.

### B5. Tests referencing the stack (update/delete with their target)
test_subdomain, test_custom_domain, test_port_mapping, test_tls_certificate,
test_root_domain, test_site, test_pilot, test_virtual_machine, test_virtual_machine_migration
(proxy_module stubs, 9 refs), test_setup, test_api_site, test_central, e2e use_cases
(self_serve_site, proxy_vm, tls_issuance, bench_self_routing, desk_buttons,
_tls_deploy_persist), tests/_*_billable_run, _routing_host_run.

--------------------------------------------------------------------------------
## C. The rewire mechanism (Site/Pilot → Satellite) — the hard part
--------------------------------------------------------------------------------
Today `Site.auto_provision`/`Pilot.auto_provision` do, in Atlas: clone/boot VM → create
`Subdomain` (reconciles the Atlas proxy) → `deploy_site` (Atlas SSH) → wait HTTP → attach
Pilot (2nd Subdomain). Every routing/deploy step is in-Atlas.

Target: Atlas keeps the VM lifecycle + the Site/Pilot rows + the Central handoff; Satellite
owns the Subdomain create/delete, the deploy, and the readiness probe.

### C1. Trigger tissue (the piece flagged "NOT built")
**Mechanism 1 (Atlas-mediated, no Central↔Satellite edge — RECOMMENDED):**
1. `Site.auto_provision` (Atlas) clones+boots the VM as today (unchanged).
2. Instead of local `_create_subdomain` + `_deploy_site`, Atlas records the deploy intent
   as fields ON THE VM ROW (additive): `deploy_intent` (Check), `deploy_fqdn`,
   `deploy_mode`, `deploy_admin_domain`, plus the opaque central blob. These ride the read
   API `_vm_payload` (additive read-only, the `build_mode`/`warm` pattern from 19d1a3d).
3. Atlas fires its existing signed VM webhook on the status change.
4. Satellite's `webhook.receive` → `registration.register_vm` mirrors the VM AND reads the
   intent. A new step creates BOTH a `Subdomain(subdomain=<label>, virtual_machine=<mirror>,
   active=1)` (routing) and a `site` Service Binding (install, reusing `run_deploy`). The
   Subdomain's after_insert runs `routing.enqueue_reconcile`; the binding's apply runs
   `bench.deploy_site` + readiness.
5. Handoff flows back unchanged: the deployed site phones Central over its baked-in
   `central_endpoint`/`central_auth_token`. Atlas's `Site.login_url` is filled by Central's
   callback (see R4).

Who creates the Subdomain: SATELLITE. Who triggers TLS: Satellite — the regional wildcard
`*.region` already covers subdomains via `services/tls.py::issue_and_push` (daily sweep);
custom domains use SNI passthrough (no Atlas cert). Teardown: `Site.terminate` (Atlas)
terminates the VM → Atlas fires `vm.deregistered` → Satellite `deregister_vm` deletes the
mirror → cascades to bindings → `teardown_vm_routes` deletes the routes + reconciles. So
Atlas's `_delete_subdomains` becomes a no-op and is removed.

**Mechanism 2 (Central-facing Satellite call):** REJECTED — phase-4 forbids a
Central↔Satellite edge and spec/28 says Atlas never calls into Satellite.

### C2. FQDN parity (A6)
Atlas passes the fully-resolved `deploy_fqdn` in the intent (not just the label), so
Satellite routes the exact FQDN. Satellite validates the label with `routing/labels.py`.

### C3. Login-URL / readiness handoff (the sharp edge — see R4)
`Site.login_url` must still populate for Central's `_mirror`. Preferred: the deployed site
pushes it to Central directly (Central already supports the callback); Atlas's `Site.login_url`
may stay blank or be filled by Central's re-write. This is the one spot the clean split rubs
against Central's existing poll of Atlas `get_site` for `login_url`.

### C4. Pilot (attached admin console)
Same delegation: intent carries `admin_domain`/`mode`; Satellite's `site` binding already
accepts them; the pilot's second Subdomain is created by the same registration step (N
Subdomains per VM already supported). The admin-JWT mint + `pilot_credential_id` threading
stays Central's (opaque blob). FLAG: phase-4 warned admin/pilot deploy is coupled to Central.

--------------------------------------------------------------------------------
## D. Phased sequence of small atomic commits (each: importable + tests green)
--------------------------------------------------------------------------------
Build/verify the Satellite trigger path FIRST; delete Atlas's stack LAST.

### Phase S — Satellite: build the Central-driven routed-site trigger (additive)
- S1 [satellite]: In registration (or a `deploy` handler), when a mirror carries
  `deploy_intent`, create the `Subdomain` + `site` binding. Region Domain parity check.
  Unit-test with a mocked read API.
- S2 [satellite]: Ensure `teardown_vm_routes` fires on `deregister_vm` (bindings cascade).
- S3 [satellite]: Accept the pilot/admin second-route intent (two Subdomains per VM).

### Phase A-add — Atlas: expose deploy intent (additive, read-only, Central-safe)
- A1 [atlas]: Add `deploy_intent`/`deploy_fqdn`/`deploy_mode`/`deploy_admin_domain` to
  `Virtual Machine`; surface in `api/satellite.py::_vm_payload`. Does NOT touch Site/Pilot.
- A2 [atlas]: `Site.auto_provision`/`Pilot.auto_provision` WRITE the intent on the VM behind
  a flag `delegate_routing_to_satellite` (default OFF). Flag OFF = identical behavior.

### Phase X — cutover (flip the flag; still no deletions)
- X1 [atlas]: Flag ON in dev. E2E: create_site → VM boots → webhook → Satellite creates
  Subdomain + deploys + routes. Verify Central's `get_site`/events still shape-correct.
  Rollback = flip OFF.
- X2 [atlas]: Make `_create_subdomain`/`_deploy_site`/`_wait_for_http`/`_provision_pilot`
  + Pilot equivalents no-ops when delegating. Tests assert the intent write.

### Phase D — Atlas: delete the now-dead stack (flag permanently ON)
- D1: EXTRACT guest-SSH helpers `proxy._record_guest_task/_remote_parent/_write_guest_file/
  _curl_command` → `atlas/atlas/guest_ssh.py`; repoint `deploy_site.py`, `image_builder.py`,
  `tcp_proxy.py`. Pure refactor.
- D2a: delete `port_mapping/` + test. D2b: delete `tcp_proxy.py` + test.
- D3: delete `subdomain/` + `custom_domain/` + tests; update `reset.py`; remove VM-controller
  `_delete_subdomains`/`_clear_subdomain_references`/`_delete_custom_domains` + terminate()
  calls + `read_proxy_maps`; update `test_virtual_machine`.
- D4: delete proxy.py's routing half → reduce to `build_proxy`; remove `is_proxy` +
  `_deprovision_proxy` + `_publish_wildcard`; update `test_proxy`, `test_virtual_machine_
  migration` (9 refs), migration `_repoint_routes` (D6).
- D5: delete `tls_certificate/`, `root_domain/`, `route53_settings/`, `lets_encrypt_settings/`
  + `tls/` + `dns/` + tests; remove `renew_expiring` scheduler + 4 doctype_js; remove
  `setup.setup_tls_layer`/`_stage_tls` + bootstrap `issue_certificate`/
  `push_certificate_to_proxies` + provider-type Settings writes (MOVE to Satellite).
- D6: `migration.py::_repoint_routes` — delete + its two call sites; Satellite's converging
  reconcile re-derives from the mirror (`vm.updated` webhook). TODO `vm.migrating` webhook if
  a sequenced cutover is later needed.
- D7: `active_root_domain()` loses its table — replace backing store with `Atlas Settings.
  region_domain` Data (region already in Settings) so Site/Pilot + `api/site.py::
  check_subdomain` still build the FQDN. The ONE spot deletion forces an Atlas rewire Central
  indirectly reads — kept inside Atlas, Central unaffected.

### Phase Z — Atlas: retire the local deploy driver (optional, last)
- Z1: once Site/Pilot never deploy locally, delete `deploy_site.py` + test. FLAG: defer if
  any e2e still exercises the Atlas deploy path.

--------------------------------------------------------------------------------
## E. Per-phase verification & rollback
--------------------------------------------------------------------------------
- Atlas unit: `bench --site atlas.localhost run-tests --app atlas` (targeted per module).
- Satellite unit: `bench --site orchestrator.localhost run-tests --app satellite`.
- Migrate check: `bench --site atlas.localhost migrate` (catches patch/reset breakage).
- E2E gate (Phase X): `self_serve_site` end-to-end on the two-site dev bench.
- Rollback: Phase X behind `delegate_routing_to_satellite` — flip OFF. Phase D destructive
  → rollback = `git revert` the atomic commit. Satellite Phase S is additive/safe.

--------------------------------------------------------------------------------
## F. Risk / breakage analysis
--------------------------------------------------------------------------------
- R1 (migrate patches): guarded by `table_exists`/`has_column`/`exists` — no-ops on migrated
  sites; a fresh DB short-circuits. DO NOT delete/un-guard. Verify `bench migrate` clean after D5.
- R2 (reset.py): deletes fewer rows once Subdomain leaves — update footprint/docstring. Low
  blast radius (dev tool, developer_mode-gated).
- R3 (bootstrap.py): removing the TLS/cert half without Satellite's equivalent leaves a
  region with no wildcard cert. Sequence: Satellite `issue_and_push` + `Region Domain` seeded
  BEFORE Atlas bootstrap TLS is removed.
- R4 (Central login-URL poll — POSSIBLY IMPOSSIBLE cleanly): Central polls Atlas `get_site`
  for `login_url`. With deploy on Satellite, Atlas no longer mints it. Either (a) the site
  pushes it to Central directly (preferred, no wire change) or (b) Atlas reads it from
  Satellite (violates "Atlas never calls Satellite"). CALL OUT: confirm the site→Central
  callback owns `login_url` so Atlas's field may stay blank.
- R5 (FQDN drift): mitigated by passing the resolved `deploy_fqdn` in the intent (C2).
- R6 (`central_report` reads Site/Pilot fields): fields stay on the Atlas rows — safe — but
  their VALUES now come from the Satellite path (R4). No Central changes.
- R7 (two-writer window during Phase X): make Atlas's local `_create_subdomain` a HARD no-op
  when delegating, not "both".
- R8 (proxy.py `build_proxy` entanglement): keep `build_proxy` + guest-SSH helpers; D1
  extraction guards this.
- R9 (e2e/billable harness imports `from atlas.atlas import proxy`): 10+ imports — update or
  remove in lockstep with D4 or test collection errors.

--------------------------------------------------------------------------------
## G. Site/Pilot sub-decision — recommendation
--------------------------------------------------------------------------------
RECOMMEND (a): keep Site/Pilot doctypes IN Atlas, strip + delegate their routing/TLS/DNS to
Satellite. Do NOT attempt a bigger cutover that moves Site/Pilot.

- Central hard-depends on Atlas's Site/Pilot (`create_site`, `central_report`, `get_site`/
  `_mirror`, `pilot_credential_id`) — moving them forces forbidden Central changes.
- Routing/TLS/DNS is the ONLY duplicated part and is cleanly separable via intent-on-VM +
  webhook because Satellite already owns the whole guest plane live.
- Site/Pilot's own logic (clone snapshot, VM lifecycle, placement, immutability) is
  provisioner logic that legitimately stays.
- The residual coupling after (a) is exactly one field — `login_url` (R4) — resolvable by the
  existing site→Central callback.

--------------------------------------------------------------------------------
## H. ADVERSARIAL REVIEW CORRECTIONS (2026-07-16, 3 critics vs. the actual code)
--------------------------------------------------------------------------------
The plan's migrate-safety holds (patches are Patch-Log-skipped / guarded / reload_doc
catches a missing dir / seed_settings is try/except-wrapped — verified). But the review
broke the plan's CORE assumption and found concrete misses:

### H0. CORE REFRAME — `login_url` cannot legally leave Atlas ⇒ KEEP THE DEPLOY IN ATLAS.
Critic 1 proved: `login_url` is minted IN-GUEST → emitted on the `ATLAS_RESULT` stdout line
→ parsed by Atlas `deploy_site.py::_parse_result` → stamped on the Site row → **Central POLLS
Atlas `get_site` for it** (Atlas authoritative). The plan's "the site pushes login_url to
Central via its baked endpoint" DOES NOT EXIST (`central_endpoint`/`X-Pilot-Token` is a
different bench-auth channel carrying no login_url). So if Satellite runs the deploy and
`_deploy_site` becomes a no-op (X2), `login_url` goes blank → Central serves a dead "Open"
for every Running tenant (the exact `front_door.py` regression). There is NO spec-legal
return path (Atlas can't read Satellite's stdout or mirror).
**Correction: Atlas KEEPS the deploy** (`deploy_site.py` + login_url mint + Site-row stamp).
Move ONLY routing/TLS/DNS to Satellite. This still deletes all 6 duplicated doctypes +
`dns/`/`tls/`/proxy-routing/`tcp_proxy` and preserves Central's contract untouched. Drop
Phase X2's "`_deploy_site` no-op" and Phase Z (deploy stays).

### H1. `subdomain_doc` Link (site.json/pilot.json → Subdomain) — VERIFIED NOT Central-visible
(`_mirror`/`get_site` don't return it). Safe to drop in D3 as internal cleanup — but it IS a
migrate-breaker if left, so D3 MUST remove the field from site.json + pilot.json + the
`DF.Link` type hints (site.py:83, pilot.py:64) + the VM controller's `_clear_subdomain_
references` (virtual_machine.py:1055-1061).

### H2. TEARDOWN event is wrong. `vm.deregistered` fires only on ROW DELETE (`on_trash`);
`VM.terminate()` sets status=Terminated + save() — it never deletes the row. Tenant teardown
emits `vm.updated`. Satellite route-teardown must key off `vm.updated` + `status==Terminated`,
NOT `vm.deregistered`. AND: Atlas deletes routes SYNCHRONOUSLY in the terminate txn today
(spec/18 "no sweeper" relies on it) — moving to an after-commit best-effort webhook opens a
convergence window (Central sees Terminated while routes briefly live). Accept it (Satellite
reconcile is the backstop; a stale route to a dead VM 502s, never mis-routes) but the plan
must retract the "no sweeper" invariant explicitly.

### H3. D6 route re-derivation is FALSE as written. `subdomain_map()` reads the STORED
`Subdomain.address`; `registration._upsert_vm` updates only the VM mirror's `guest_ipv6` and
never re-saves dependent routing rows; Satellite has NO VM `on_update` hook. So a change-
address migration leaves routes pointing at the OLD address with nothing to trigger a
reconcile. **New Satellite work required before D6:** a VM-mirror `on_update` (or address-
change webhook handler) that, on a `guest_ipv6` change, re-saves every dependent Subdomain/
Custom Domain/Port Mapping (re-running `routing_address`) + calls `enqueue_reconcile`. This
replaces Atlas's deleted `migration.py::_repoint_routes`.

### H4. D1 EXTRACTION misses importers of the shared guest-SSH helpers — add to the repoint
list: `customer_gateway.py:252`, `image_build.py:541`, `image_recipes.py:139`. And
`image_recipes.py::_finalize_proxy` (a LIVE provisioner recipe callback) imports
`proxy.REGION_FILE` + `active_root_domain` — so `REGION_FILE` is a PROVISIONER constant:
KEEP/extract it, do NOT delete it with the routing half (D4).

### H5. D7 under-scopes `active_root_domain()` — it returns a Root Domain DOC with `.domain`
+ `.region` and has 8+ live callers incl. `atlas_settings._proxy_region_and_domain`,
`image_recipes.py:148`, `subdomain_label.py`, `api/site.py`, `site.py`, `pilot.py`,
`virtual_machine.py`. The Settings-backed replacement MUST preserve the `.domain`/`.region`
interface and repoint ALL callers atomically.

### H6. Phase-D "each commit green" is FALSE without: removing Site/Pilot
`_create_subdomain`/`_delete_subdomain` (not neutering them), cleaning the workspace JSON
(`workspace/atlas/atlas.json` link_to for all 6 doctypes), and updating the tests the plan's
B5 omits: `test_private_networking_wiring.py`, `api/test_provision.py`, `test_image_builder.py`,
`test_atlas_settings.py` (the last two are KEEP files whose fixtures create Root Domain).

### H7. Two-writer window: fold the `_create_subdomain` no-op INTO the flag-flip (gate it from
A2 onward), not a separate X2 commit. Rollback framing: flag-OFF is valid only PRE-D3; after
that, rollback = `git revert` the atomic commit.

### H8. Phase-X e2e gate CANNOT be `self_serve_site` (billable host-bound + imports the
deleted `proxy`/`tls_issuance`). Build a fake-provider + mocked-Satellite variant that
exercises intent-write → webhook → Satellite-Subdomain, or gate Phase X on the new unit test.

### Net corrected shape
Atlas KEEPS: VM lifecycle, Site/Pilot, the DEPLOY (deploy_site + login_url), build_proxy +
guest-SSH helpers (extracted), REGION_FILE, active_root_domain (→ Settings). Atlas LOSES: the
6 routing/tls/dns doctypes + `dns/`/`tls/` + proxy-routing + tcp_proxy + Subdomain
create/reconcile (→ Satellite). Satellite GAINS: create-Subdomain-from-intent + a VM
on_update route re-derivation + TLS issue on the intent. login_url + Central contract:
UNCHANGED.
