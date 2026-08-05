# Phase 4 — service installation moves to Satellite (revised scope)

Supersedes the Phase-4 section of `phases-1-5-migration.md`. That draft assumed a
Central re-point (Resolution A/B) where **Central talks to Satellite**. New constraints
from the user (2026-07-15):

1. **Central and Pilot are Central's, now and in the future — DO NOT TOUCH them.** No
   changes to `Site`/`Pilot` doctypes, `create_site`/`create_pilot`, `central_report.py`,
   `central_link.py`, `api/site.py`, `pilot_credential_id`.
2. **Central should never communicate with Satellite (ideally).** No Central↔Satellite
   edge. Atlas is the only intermediary.

So Phase 4 shrinks to one thing: **the guest-plane service installation moves to
Satellite; everything orchestration-side stays with Central.**

## The seam (the only code that moves)

The service-installation = the guest deploy, a self-contained pair:

- `atlas/atlas/deploy_site.py` (314 lines) — the driver: `wait_for_ssh` → `scp` the
  script into the guest → run it → `wait_for_http` readiness → return `{serving,
  login_url}`. Plus `regenerate_login`, `readiness_path_for_mode`.
- `bench/deploy-site.py` — the stdlib-only in-guest script: rename baked `site.local` →
  FQDN, regenerate the bench nginx vhost, confirm serving, mint the login URL.

Both terminate INSIDE the VM → guest-plane → Satellite (spec/28 boundary rule). They
become `satellite/bench.py` (or `services/site.py` + a copied `bench/deploy-site.py`)
driving the script over `run_guest`/`scp_guest` — the same idiom as `services/proxy.py`
push_cert. Copy `deploy-site.py` verbatim into Satellite's tree (stdlib-only, no host
`scripts/lib`).

## Inputs the guest deploy consumes (deploy_site.py:162–194)

| Input | Source after the split | Owner |
|---|---|---|
| VM guest IPv6 | already mirrored | — |
| FQDN / site name | the routing label — **already Satellite's** (Phase 2) | Satellite |
| `build_mode` (site vs admin; readiness path) | **ADD to Atlas read API** (`_vm_payload`); decision 9 keeps build_mode in Atlas as an image attr | Atlas exposes → Satellite reads |
| `warm_snapshot` / warm-vm-uuid | **ADD to Atlas read API** (mirror field) | Atlas exposes → Satellite reads |
| `central_endpoint`, `central_auth_token`, `admin_domain` | Central's values, threaded through, opaque to Satellite | Central owns; pass-through |

Everything Satellite needs is on the Atlas mirror/read-API. Central pushes nothing to
Satellite.

## The trigger — Central → Atlas → (webhook) → Satellite

Reuse the Phase-0 channel (Atlas read API + signed VM webhooks). No new Central↔Satellite
edge:

1. Central asks **Atlas** to provision a VM and records the deploy intent + params **on
   the Atlas VM record** (Central→Atlas — an existing link).
2. Atlas fires its existing signed VM webhook; Satellite mirrors the VM and reads the
   intent + params off the Atlas read API.
3. Satellite installs via a `site` **Service Binding** (`after_insert`→apply runs
   deploy-site.py + creates the route + probes readiness; `on_trash`→withdraw tears down).
4. Handoff flows back the OTHER way: the deployed site phones **Central directly** over
   the baked-in `central_endpoint`/`central_auth_token`. So `login_url`/readiness never
   goes Satellite→Central either.

Central talks only to Atlas; the deployed *site* talks to Central; Satellite talks only
to VMs + Atlas's read API. No Central↔Satellite edge anywhere.

## Stays with Central (untouched)

`create_site`/`create_pilot` orchestration minus the deploy call: VM/snapshot choice,
`_provision_backing_vm` (clone), the `Site`/`Pilot` records, `pilot_credential_id`, the
tenant handoff, `central_report.py`/`central_link.py`/`api/site.py`. `build_mode` +
Image Build stay in Atlas (decision 9).

## The wrinkle to flag

The **admin/pilot-mode** deploy (`--mode admin`, `--admin-domain`, `pilot_credential_id`,
`central_endpoint`/`auth_token`) is threaded straight from `create_site` and is
inseparable from the Pilot console. Keep the pilot-console deploy coupled to Central;
Satellite owns the **plain site install** cleanly and forwards the Central params into the
guest as an opaque blob it never interprets.

## Concrete steps (all ADDITIVE on Satellite; NOTHING deleted from Atlas here)

1. **Atlas read API** (`atlas/atlas/api/satellite.py::_vm_payload`): expose `build_mode` +
   `warm_snapshot` (+ the deploy-param blob if/when the intent model lands). Additive,
   read-only — does NOT touch Site/Pilot.
2. **Satellite**: copy `bench/deploy-site.py` into the tree; add `satellite/bench.py`
   (port of `deploy_site`/`regenerate_login`/`wait_for_http` over run_guest/scp_guest);
   add a `site` service handler + seed it in DEFAULT_SERVICES.
3. **Satellite mirror**: add `build_mode`/`warm_snapshot` fields to the Virtual Machine
   mirror + `registration._upsert_vm`.
4. **No Atlas deletion of Site/Pilot/deploy_site in this phase** — the user keeps them.
   (The old plan's "delete Site/Pilot" is dropped entirely.)

## Verify-yourself gaps (one-host dev setup)

- No real bench snapshot / golden image in dev → the deploy itself is unit-testable only
  (mock run_guest/scp_guest, assert the deploy-site.py argv + the readiness probe). A real
  end-to-end deploy needs a baked bench VM.
- The Fake-server short-circuit (`is_fake_server`) has no Satellite analogue; the deploy
  service must no-op / be mockable for tests the way Atlas's `_deploy_site` does.
- The Central-intent-on-the-VM model (step 1) is a design assumption — Central's actual
  mechanism for recording deploy intent on the Atlas VM is Central's to define; this plan
  only needs Atlas to EXPOSE whatever intent exists on its read API.

## Relationship to the other deferred Atlas deletions

Phases 2/3/5 left several Atlas deletions "for Phase 4" because they were entangled with
Site/Pilot (the shared `Subdomain`/`Custom Domain`/`Port Mapping` doctypes, proxy.py/
tcp_proxy map-builders, `Root Domain`, tls/dns dirs). **Under the new constraint (Site/
Pilot stay in Atlas forever), those deletions DO NOT happen** — Atlas keeps its own
routing/proxy/tls for the Site/Pilot path, and Satellite runs its parallel copy for the
guest-plane path. The transient two-writer state becomes the steady state; it is benign
in a no-production setup and is the accepted cost of leaving Site/Pilot with Central.
