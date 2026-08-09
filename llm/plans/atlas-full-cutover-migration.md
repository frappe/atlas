# Atlas → Satellite FULL cutover: move the entire guest plane, delete from Atlas, re-point Central

Status: DESIGN (read-only draft; pending multi-agent adversarial review + user approval).
Supersedes the HALF-cutover `atlas-dedup-migration.md`. Per the user's 2026-07-16 decision +
spec/28 §1b/§1c/§4: Site, Pilot, Site Request, deploy_site, front_door, api/site,
routing/TLS/DNS all move to Satellite; Atlas becomes a pure provisioner; Central re-points.
Repos: `apps/atlas` (feat/vm-service-seam) + `apps/satellite` (main), separate git repos.

## A. What Satellite ALREADY has (verified, live-e2e-proven)
Registration mirror `(atlas, remote_id)` + signed webhook + read-API client; full routing
(`routing/`, Subdomain/Custom Domain/Port Mapping/Denylist/Audit, `services/routing.py` incl.
`teardown_vm_routes`); proxy push; DNS (route53); TLS (`services/tls.py`, Region Domain,
Route53/Lets Encrypt Settings, TLS Certificate); site install (`services/site.py` +
`bench.py` incl. `_parse_result` of the `ATLAS_RESULT=` line — login_url already round-trips
through Satellite); service catalog.
Satellite does NOT yet have: `Site`/`Pilot`/`Site Request` doctypes+controllers; the
Central-facing `api/site.py` + `front_door.py`; a Central event client; a VM-mirror
`on_update` route re-derivation; teardown keyed off `vm.updated`+Terminated.

## B. What moves (precise map)
[AUTHOR]=create in Satellite via bench · [PORT]=move+rebind to satellite.ssh · [DELETE-ATLAS]
· [KEEP-ATLAS] · [NEW-SAT/NEW-ATLAS].

### B1 Doctypes to AUTHOR in Satellite (from Atlas JSON)
- Site [AUTHOR] — `virtual_machine`→VM MIRROR link, `subdomain_doc`→Sat Subdomain (internal,
  not in `_mirror` wire — verified), `tenant`→Data (Central Team.name), status/login_url/
  login_url_expires_at/pilot/timing.
- Pilot [AUTHOR] — same shape; attached + standalone paths.
- Site Request [AUTHOR?] — Atlas dir is an empty stub (no controller/JSON). FLAG: likely
  vestigial; confirm use before authoring, else drop from scope.
- Tenant — NOT authored; `tenant` stays a Data string on Site/Pilot/mirror.

### B2 Modules to PORT
- front_door.py [PORT] → resolves mirror VM → Sat Site/Pilot.
- api/site.py (create_site/check_subdomain/get_site/_mirror) [PORT] → Satellite Central-facing
  surface; uses routing/labels.py + active_region_domain().
- api/provision.py [SPLIT] — capacity/resize/VM-create half [KEEP-ATLAS]; pilot front-door
  read gets a Sat twin.
- deploy_site.py — ALREADY on Satellite (bench.py+services/site.py); Atlas copy [DELETE-ATLAS].
- subdomain_label.py — Sat has routing/labels.py; Atlas copy [DELETE-ATLAS] once importers leave.
- central_report.py site/pilot half [PORT→ satellite/central_report.py] + [NEW-SAT] a Satellite
  central.py client + a `Central` credential doctype. VM-lifecycle half [KEEP-ATLAS].

### B3 Atlas deletions (reuse dedup §B/§H ordering)
Doctypes subdomain/custom_domain/port_mapping/tls_certificate/route53_settings/
lets_encrypt_settings/root_domain/subdomain_denylist/bench_routing_audit + site/pilot/
site_request. Modules proxy(routing half)/tcp_proxy/dns/tls/deploy_site/front_door/api-site.
VM-controller fan-out (_delete_subdomains/_delete_custom_domains/_deprovision_proxy/
read_proxy_maps + is_proxy/build_mode?/pilot_credential_id) removed incrementally. Wiring:
Site/Pilot doc_events, doctype_js, renew scheduler, migration._repoint_routes.

### B4 What STAYS in Atlas (provisioner) — verified
VM lifecycle, clone_to_new_vm, placement, sizing, Firewall, IMAGE BUILD + build_mode
(hard-problem #3), api/provision.create_vm/capacity, api/satellite read API, satellite_events
webhook, SSH-key injection, proxy.build_proxy + guest-SSH helpers + REGION_FILE +
active_root_domain (imported by image_recipes:139/148, customer_gateway:252, image_build:541 —
verified) → EXTRACT to guest_ssh.py before any proxy deletion.

## C. The FOUR hard problems — resolutions
### #1 Site needs a VM, Satellite has no execution API
VM lifecycle STAYS in Atlas. Split create_site into provisioner (Atlas creates VM) +
orchestrator (Satellite creates Site + routes + deploys). Recommended flow:
1. Central → Satellite create_site(team, subdomain, pilot_credential_id, central_endpoint,
   central_auth_token) → Sat Site row (Pending), returns _mirror.
2. Sat Site.after_insert → provision job; obtains a VM via [NEW-ATLAS] `api/satellite.
   provision_bench_vm(subdomain, tenant, size, snapshot)` (token-authed WRITE; clones golden
   snapshot + boots exactly as today's `_provision_backing_vm`; returns {remote_id}).
   - The single deliberate exception to spec/28 §2 "no execution API": cloning+booting a
     microVM is irreducible provisioner work; Atlas already exposes create_vm to Central, so
     Satellite is just another create_vm caller — Atlas stays service-unaware.
   - ALTERNATIVE: Central makes two calls (Atlas create_vm → {remote_id} → Satellite
     create_site(remote_id,…)). Keeps Atlas byte-identical but is a bigger Central diff. Both
     are Central-coordination items; recommend provision_bench_vm (single Central call).
3. Atlas boots VM → existing signed `vm.updated` webhook on Running.
4. Sat webhook→register_vm mirrors (guest_ipv6 populated). Sat provision job polls mirror for
   Running, then: Subdomain (→reconcile), `site` binding (→run_deploy→bench.deploy_site→mint
   login_url→wait_for_http), stamp login_url on Sat Site, Pilot 2nd route, Running →
   central_report pushes site.status_changed to Central.
5. Teardown: Central → Sat Site.terminate deletes Subdomain(s); Central → Atlas terminates the
   VM (Central already does VM-terminate today). See R3.

### #2 Re-point Central WITHOUT touching Central's repo
Coordination contract (Central performs, you enable): (a) Central points base_url → Satellite
URL + method names → `satellite.api.site.*` + a new Satellite System Manager token. (Mimicking
Atlas's `atlas.atlas.api.site.*` path on Satellite is INFEASIBLE — needs an `atlas` package on
Satellite, violating "Satellite never installs Atlas". Reject.) (b) Wire shapes ported
byte-identical (_mirror/get_site/check_subdomain). (c) Central issues Satellite event-service
creds + Satellite pushes site.*/vm.status_changed to central.api.atlas.event.
Zero-downtime sequence: Phase S authors on Satellite (Atlas untouched, parallel-run) → enable
+ prove e2e against Satellite → Central re-points (EXTERNAL) → Phase D deletes Atlas. NO
Atlas→Satellite delegation during the window (forbidden). Do NOT delete Atlas api/site until
Central re-points.

### #3 Image Build / build_mode
KEEP in Atlas. Bake = clone scratch VM → install → snapshot → terminate = provisioner
execution Satellite can't do (same execution-API violation as #1, worse). build_mode is a
read-only-to-Satellite image attribute (on _vm_payload; consumed by bench.deploy_site). Explicit
justified deviation from spec/28 §4's "Image Build → satellite / delete build_mode."

### #4 login_url — now CLEAN (the decisive win over the half-cutover)
Site row lives on Satellite; Central polls Satellite get_site. bench.deploy_site (already ported,
already parses ATLAS_RESULT=) mints + stamps login_url on the Satellite Site row → Sat _mirror
returns it → Central reads it. Fully within Satellite; no cross-app return path. Removes the
half-cutover's one hard blocker (dedup §H0).

## D. Phased per-commit sequence (Satellite-additive FIRST; Atlas deletions gated behind Central re-point)
Phase S (Satellite additive, Atlas untouched):
- S1 [sat] `Central` credential doctype/Settings + port central.py client; unit test vs stub.
- S2 [sat] AUTHOR Site doctype + controller MINUS _provision_backing_vm (→ Atlas call);
  auto_provision restructured (poll mirror for Running → Subdomain + site binding). Unit test
  with mocked Atlas provision + Fake VM.
- S3 [sat] AUTHOR Pilot doctype + controller (attached + standalone; standalone boots a bench
  IMAGE via an Atlas call).
- S4 [sat] PORT front_door.py + api/site.py (create_site/check_subdomain/get_site/_mirror);
  unit test the mirror shape byte-matches Atlas's.
- S5 [sat] PORT central_report site/pilot half + regenerate_vm_login; wire Sat doc_events.
- S6 [sat] [NEW] VM-mirror on_update route re-derivation (dedup §H3): on guest_ipv6 change,
  re-save dependent Subdomain/Custom Domain/Port Mapping + enqueue_reconcile. Replaces Atlas's
  _repoint_routes. Unit test.
- S7 [sat] [NEW] Teardown keys off vm.updated + vm_status==Terminated (NOT vm.deregistered).
  Unit test.
Phase A-add (Atlas additive):
- A1 [atlas] [NEW] api/satellite.provision_bench_vm (clone+boot, returns {remote_id}). Deliberate
  spec-§2 exception — document. Additive; Atlas's own Site/Pilot still work.
Phase X (enable + parallel-run prove, no deletions):
- X1 Configure Sat Central creds + Region Domain + Atlas record. Operator-run
  satellite.api.site.create_site end-to-end on the two-site dev bench. E2E gate: Atlas's
  billable self_serve_site is unusable post-deletion — build a Fake-provider + real-Satellite
  variant or gate on S2-S5 unit tests + a manual two-site run. Rollback: Central still on Atlas.
Central re-point (EXTERNAL, RP1): Central flips URL+methods+token+event-creds; verify a full
create_site→Running→login_url→event round-trip before Phase D.
Phase D (Atlas deletions, gated behind confirmed RP1; reuse dedup §D + §H corrections):
- D1 EXTRACT guest-SSH helpers + REGION_FILE → guest_ssh.py; repoint deploy_site, image_builder,
  tcp_proxy, customer_gateway:252, image_build:541, image_recipes:139 (dedup §H4).
- D2 delete port_mapping + tcp_proxy (+tests).
- D3 delete subdomain + custom_domain (+tests); VM-controller _delete_subdomains/
  _clear_subdomain_references/_delete_custom_domains/read_proxy_maps + terminate() calls; update
  reset.py; clean workspace JSON (dedup §H6). bench migrate clean.
- D4 delete proxy routing half → build_proxy only; remove is_proxy/_deprovision_proxy/
  _publish_wildcard; update test_proxy, test_virtual_machine_migration (9 refs), e2e imports.
- D5 delete tls_certificate/root_domain/route53_settings/lets_encrypt_settings + tls/ + dns/ +
  tests; remove renew scheduler + 4 doctype_js; move setup/bootstrap TLS to Satellite (already
  there). R3: seed Sat Region Domain + issue_and_push BEFORE deleting Atlas bootstrap TLS.
- D6 active_root_domain → Atlas Settings.region_domain Data preserving .domain/.region; repoint
  KEEP callers (image_recipes:148, atlas_settings._proxy_region_and_domain, virtual_machine)
  (dedup §H5).
- D7 delete migration._repoint_routes + 2 call sites (S6 replaces it).
- D8 [FULL-CUTOVER-SPECIFIC] delete site/pilot/site_request doctypes+controllers+tests,
  front_door.py, api/site.py, deploy_site.py+tests; remove Site/Pilot doc_events + central_report
  site/pilot half; update test_central/test_api_site/test_site/test_pilot/test_deploy_site;
  clean workspace. bench migrate clean.
- D9 delete subdomain_label.py (importer-free after D8; re-grep first).

## E. Verification + rollback
Atlas unit (targeted); Satellite unit; `bench migrate` on every Atlas deletion commit (patches
guarded — do NOT un-guard, dedup §R1); Phase-X e2e via Fake-provider variant. Rollback: S/A/X
additive (don't re-point = Atlas keeps serving, zero risk); post-RP1 → re-point BACK to Atlas
(Site/Pilot present until Phase D); Phase D destructive → git revert per repo. After D8 Atlas
can't serve Central; full rollback = revert D1-D9 + re-point Central back.

## F. Risks
- R1 Central re-point window (HIGHEST): partial re-point (URL flipped, event creds missing) →
  mirror stuck Pending. Flip URL+methods+event-creds atomically; verify full round-trip before
  Phase D. Load-bearing Central-team step.
- R2 Site-needs-a-VM: provision_bench_vm is a WRITE on the read API (spec-§2 deviation) — needs
  sign-off, else Central-2-call fallback. Poll-for-Running loop needs a deadline + Failed path
  (VM boot fails in Atlas → Sat times out → Site Failed) + orphan handling (VM created, Site fails).
- R3 Teardown convergence: VM.terminate() sets Terminated+save() (no row delete → vm.deregistered
  never fires); signal is vm.updated+Terminated. Atlas deletes routes SYNCHRONOUSLY today (spec/18
  "no sweeper"); after-commit webhook → reconcile opens a stale-route window (a dead-VM route 502s,
  never mis-routes). ACCEPT but explicitly RETRACT spec/18 "no sweeper". VM-teardown ownership:
  Central calls Atlas (VM) + Satellite (Site) — two calls, a coordination item (Satellite→Atlas
  terminate would be a forbidden edge).
- R4 Route re-derivation: S6 is required new work; a live change-address migration is UNVERIFIABLE
  in one-host dev (needs 2 hosts) — unit-test only.
- R5 login_url: RESOLVED (win over half-cutover).
- R6 FQDN parity: Region Domain.domain must == old Root Domain.domain for the region (dedup §A6).
- R7 Central event creds: Central pushes per-Atlas creds via provision_tunnel today; Satellite has
  none → Central pushes to Satellite OR operator hand-configures. Coordination item.
- R8 Site Request likely vestigial (empty stub) — confirm before authoring.
- R9 Tenant: Data string, not a Link; verify _mirror returns team correctly.
- R10 UNVERIFIABLE in one-host dev: multi-Atlas federation, change-address re-derivation, teardown
  timing, real proxy fleet — N=1 unit-test only; live cutover needs ≥2-host staging.

## G. Needs Central-team coordination / genuinely external
1. Central re-point (base_url + method names + token) — Central performs.
2. Central event-service creds for Satellite (R7).
3. VM-teardown ownership (R3) — Central calls Atlas + Satellite.
4. provision_bench_vm vs Central-2-call (hard #1 / R2) — sign-off.
5. Live change-address migration + multi-host teardown (R4/R10) — needs staging.

## H. MULTI-AGENT ADVERSARIAL VERDICT (2026-07-16, 4 critics vs. the code)
The full cutover is NOT a self-contained Atlas+Satellite refactor — it is a cross-team,
multi-week program. Convergent blockers:

**Requires Central-repo changes (contradicts "don't touch Central"):**
- Central must mint+install a SATELLITE System Manager token (its Atlas token won't auth).
- Central must issue Satellite event-service creds (no `provision_tunnel` on Satellite;
  without creds `deliver()` skips → EVERY site.status_changed dropped → mirror stuck Pending).
- Two-call teardown (Central → Satellite for Site + → Atlas for VM).
- The KEPT Central-facing VM asset mirror (`_vm_payload`/`api/inventory.tenant_vms`/
  `regenerate_vm_login`) folds `gateway_url`/`login_url`/`status` off Site/Pilot via
  `front_door_for_vm` — deleting Site/Pilot changes that KEPT wire shape → Central-visible.
- `create_vm` (marked KEEP) actually creates a PILOT — deleting Pilot guts Central's bench-VM
  entry point.

**Requires a data migration of existing Site/Pilot rows (OMITTED):**
- Existing Sites live only in Atlas; at re-point, get_site/terminate → DoesNotExist on
  Satellite → lost logins + ORPHANED BILLED VMs, then D8 destroys the record. Point of no
  return = the FIRST live Satellite create_site, not "before Phase D."

**Requires net-new load-bearing Satellite machinery (does NOT exist):**
- Scheduled reconcile sweep (the cited "backstop" is NOT wired — hooks.py has only TLS renew).
- Site-level re-drive for a dead provision job (a lost webhook = stuck Pending forever).
- Teardown TRIGGER: today `vm.updated`+Terminated only upserts the mirror; `teardown_vm_routes`
  is reachable ONLY via a binding unbind nothing calls → routes are NEVER deleted, not "stale".
- Idempotency key on provision (double-VM on retry — `clone_to_new_vm` has no key).
- Mirror-name resolution (atlas+remote_id → mirror) + Link-integrity vs `deregister_vm`'s
  force-delete; a Fake-guest short-circuit for dev (Atlas's `is_fake_server` gate is deleted).

**Structural / spec:**
- `provision_bench_vm` is a spec-forbidden Satellite→Atlas WRITE edge; the Central-2-call model
  is safer but a bigger Central diff. Orphan VM has NO recovery (Satellite can't terminate an
  Atlas VM: atlas_client is GET-only, read API is read-only).
- `tenant` is a Link→Tenant (plan said Data); port must convert + drop `ensure_tenant`; Tenant
  STAYS in Atlas (8 kept doctypes link it).

**Unverifiable in one-host dev:** multi-Atlas, change-address re-derivation, teardown
convergence, real proxy fleet, Phase-X create_site→Running — all need multi-host + real
DO/Route53 staging.

**What IS safe (for balance):** `bench migrate` survives D8 (all 18 patches guarded, no
cross-doctype Link to Site/Pilot beyond Site→Pilot which delete together); workspace stale
links are cosmetic; the parallel-run double-event is benign; Site Request is genuinely
vestigial. But add a guarded `tabSite`/`tabPilot` drop patch + `reset.py` Site cleanup to D8.

**Bottom line:** categorically NOT "remove the duplicated doctypes." It is a cross-team program
needing Central-team coordination + a staging fleet. Recommend re-scoping (see the chat).
