# Central — the global control plane

Central is a global management dashboard for Frappe Cloud. One Central talks to
*many* Atlas instances, each running its own region and provider. Atlas is the
**client** of Central — the mirror image of the provider relationship, where
Atlas is the client of a vendor (DigitalOcean, Scaleway).

This document describes the Atlas side of that seam. The Central app itself
lives in a different repository; the only contract Atlas depends on is a small
set of whitelisted HTTP methods (see *The wire contract* below).

> **The management plane now runs behind a Central-managed tunnel.**
> [21-tunnel.md](./21-tunnel.md) reverses registration (Central orchestrates it,
> the operator no longer clicks **Register** on Atlas) and moves all Central→Atlas
> traffic onto a private WireGuard address (`tunnel_url`), with Atlas firewalling
> its public interface. The sections below describe the seam's *behavior*
> (commands, events, catalogs); the *transport and registration* are now as 19
> specifies. Where the two differ, 19 is authoritative.

## What Central does for Atlas

1. **Registration (now Central-initiated).** The operator feeds Central an Atlas
   instance's admin key + `base_url` + region, and **Central** orchestrates
   registration — allocating the `atlas_id`, standing up the tunnel, and pushing a
   scoped service user back to Atlas ([21-tunnel.md](./21-tunnel.md)). Atlas no
   longer pushes `register`; it exposes the inbound `provision_tunnel` /
   `confirm_tunnel` surface Central drives. The `atlas_id` Central assigns is what
   Atlas stamps on every event so Central can route them to this cluster.
2. **VM Sizes.** Today each Atlas hardcodes its size catalog
   (`atlas/atlas/sizes.py` `SIZE_PRESETS`). Central becomes the source of truth:
   Atlas **fetches sizes** from Central into a local `Central Size` catalog.
3. **Expected bench images.** Central declares which bench images each Atlas is
   *expected* to offer (V15, V16, Develop…). Atlas **fetches** that list into a
   local `Central Image` catalog. Central sets the *expectation*; Atlas still
   bakes each image with the existing Image Build pipeline — the `bench-v15` /
   `bench-v16` / `bench-nightly` recipes ([15-image-builder.md](./15-image-builder.md))
   — and **promotes** each golden to a base image. `Central Image.bake_status`
   shows expectation-vs-reality per image.

   **The link is an exact name-match.** `upsert_central_images` sets a `Central
   Image`'s `local_image` (and flips `bake_status` to **Baked**) iff a
   `Virtual Machine Image` of the **same name** as `Central Image.image_name`
   exists. So the operator (or the promote default) must name the promoted image
   exactly `bench-v15` / `bench-v16` / `bench-nightly` — which is what each bench
   recipe's `promote_image_name` defaults to. A mismatch leaves the `Central Image`
   orphaned at `Expected`. Nothing else links them: there is no push from Central,
   no `series`-based fallback — the name is the whole contract.
4. **Event reporting.** Atlas reports every Virtual Machine lifecycle event
   (created / status changed / terminated), **Site** lifecycle event (created /
   status changed), Snapshot completion, and Server state change back to Central,
   so the global dashboard reflects fleet state in near-real time.

## Central as the front door

Central is the **face of all customer actions**; Atlas is its regional backend.
The customer never talks to Atlas directly — they act in Central, which (after
checking who they are, which team they act for, and what they've paid for)
performs the action against the right regional Atlas.

Central does this **not** through a bespoke command API but by behaving as an
ordinary authenticated Frappe client:

- **One service user.** Central authenticates to a regional Atlas as a single
  service user (a Frappe API key/secret), the same `token key:secret` header the
  telemetry client uses in reverse. It is *not* a per-customer login.
- **Whitelisted methods are the command surface.** Lifecycle actions are the
  standard controller methods — `Virtual Machine.provision` / `start` / `stop` /
  `restart` / `pause` / `resume` / `snapshot` / `rebuild` / `resize` /
  `terminate`, `run_doc_method` — the same surface Desk drives. **Creation** has
  two dedicated operator endpoints that get-or-create the `Tenant` and insert the
  resource in one call: `atlas.atlas.api.provision.create_vm(central_reference,
  …)` and `atlas.atlas.api.site.create_site(central_reference, subdomain, …)`
  (the self-serve site, [14-self-serve.md](./14-self-serve.md)). Both return the
  mirror row Central reflects. Atlas adds no other inbound command endpoint.
- **Tenant is the attribution key.** The create endpoints get-or-create the
  `Tenant` ([02-doctypes.md § Tenant](./02-doctypes.md#tenant)) from
  `central_reference` and stamp it `set_only_once` on the resource. Atlas has **no
  end-user `owner` scoping** — the `tenant` link is how a resource is tied back to
  a Central team ([11-user-ui.md](./11-user-ui.md)).
- **Authorization split.** Central **pre-checks** capability, billing, and
  quota / entitlement before it calls. Atlas **trusts that session** — it runs
  no `fc_teams` / capability engine of its own — and enforces only what only the
  region knows: **physical capacity**. If Central authorized a create but no
  Active server in the region has room, the create is rejected with a typed
  no-capacity error ([placement.py](../atlas/atlas/placement.py)) and no
  `Virtual Machine` row leaks; Central surfaces it (retry, queue, or alert the
  operator to add a Server).

The telemetry seam below (Atlas → Central event reporting, size / image fetch)
is unchanged and complementary: it keeps Central's asset registry in sync with
the fleet state the commands above produce.

## DocTypes

- **Central Settings** (single) — the credentials and this Atlas's tunnel identity.
  Mirrors `DigitalOcean Settings`. Fields: `url`, `api_key`, `api_secret` (Password),
  `enabled` (master switch — event reporting is skipped when off), the read-only
  `atlas_id`, and a read-only `status` breadcrumb (the last register / event-delivery
  outcome — a glance-only convenience; the event *history* belongs to the planned
  `Central Event` log, not the Single). The region is read from `Atlas Settings.region`
  (`placement.atlas_region()`), not a Central Settings field. **Plus a Tunnel section**
  (`tunnel_ip`, `tunnel_cidr`, `hub_public_key`, `hub_endpoint`, `wg_public_key`,
  `wg_listen_port`, `tunnel_status`); `url` / `api_key` / `api_secret` now hold the
  **pushed per-Atlas Central service-user** creds — all written by Central's
  `provision_tunnel`, no longer hand-entered (registration is **Central-initiated**;
  see [21-tunnel.md](./21-tunnel.md)).
- **Central Size** — a size Central says this Atlas should offer (`slug`,
  `title`, `vcpus`, `cpu_max_cores`, `memory_megabytes`, `disk_gigabytes`,
  `monthly_cost_usd`, `enabled`, `central_metadata`). Distinct from
  `Provider Size` (what the *vendor* sells); the field shape matches
  `SIZE_PRESETS` so these rows can later replace the hardcoded presets.
- **Central Image** — a bench image Central expects (`image_name`, `title`,
  `series`, `enabled`, `local_image` → `Virtual Machine Image`, `bake_status`
  Expected/Baked/Stale, `central_metadata`).

## Buttons (Central Settings → Actions ▾)

Each is a whitelisted controller method returning a plain dict for a toast,
exactly like `DigitalOceanSettings.test_connection`:

- **Test Connection** — `ping()`; green `OK` / red `Failed`.
- **Fetch Sizes** — `fetch_sizes()`; upserts `Central Size` rows
  (insert / update / disable-missing, same shape as `provider.upsert_catalog`).
- **Fetch Images** — `fetch_images()`; upserts `Central Image` rows.

There is **no Register button** anymore: registration is Central-initiated
([21-tunnel.md](./21-tunnel.md)). The inbound `provision_tunnel` / `confirm_tunnel`
methods (in `atlas/atlas/api/central_link.py`) are how Central registers this Atlas;
they are not operator buttons.

## Event reporting

Reporting is wired with `doc_events` in `hooks.py` (no controller edits) →
`atlas/atlas/central_report.py`. A status transition on a `Virtual Machine`,
`Site`, `Virtual Machine Snapshot`, or `Server`, and a VM / `Site` `after_insert`,
enqueue a background `deliver` job (`enqueue_after_commit=True`, so a rolled-back
transaction is never reported). The job POSTs to Central and records the outcome
in `Central Settings.status`. Everything is gated on
`Central Settings.enabled`, so a site without Central configured pays nothing,
and a delivery failure is logged to the Error Log — it never blocks the
operation.

The event types: `vm.created` / `vm.status_changed` / `vm.deleted`,
`site.created` / `site.status_changed`, `snapshot.completed`, and
`server.status_changed`. The **`site.status_changed` for `Running`** carries the
tenant handoff in its payload — the live `url` and the baked `admin_password`
([14-self-serve.md](./14-self-serve.md)); earlier site transitions carry neither
(there is no handoff to give yet). `atlas.atlas.api.site.get_site(name)` is the
poll equivalent of the site events, returning the same shape for Central to
self-heal a missed delivery.

**Deferred (durable delivery).** v1 is fire-and-forget: an event is lost if
Central is down when its job runs. The planned upgrade is a `Central Event`
outbox DocType (`event_type`, `payload`, `status`, `attempts`, `last_error`)
drained by a minutely `scheduler_events` job for at-least-once delivery.

## The wire contract

**Atlas → Central (outbound, unaffected by the firewall).** Atlas calls Central's
whitelisted methods at `<url>/api/method/central.api.atlas.<name>` with
`Authorization: token <api_key>:<api_secret>` — where the creds are now the **pushed
per-Atlas service user** ([21-tunnel.md](./21-tunnel.md)). The methods Atlas expects:

| Atlas call | Central method | Returns |
| --- | --- | --- |
| `ping` | `central.api.atlas.ping` | `{ label }` |
| `fetch_sizes` | `central.api.atlas.sizes` | `[ { slug, title, vcpus, cpu_max_cores, memory_megabytes, disk_gigabytes, monthly_cost_usd } ]` |
| `fetch_images` | `central.api.atlas.images` | `[ { image_name, title, series } ]` |
| `post_event` | `central.api.atlas.event` | (ignored) |

`register` is gone (registration is Central-initiated). **Central → Atlas
(inbound)** now travels over the tunnel (`tunnel_url`) authenticated as the **Atlas
admin** token, and adds the `provision_tunnel` / `confirm_tunnel` / `tunnel_status`
methods — all specced in [21-tunnel.md](./21-tunnel.md). The command surface (VM
lifecycle methods, `run_doc_method`) and the reconcile read below are unchanged in
shape; only their address (now `tunnel_url`) and bootstrap moved.

The route names and payloads are the single external dependency; the whole
contract is absorbed in `atlas/atlas/central.py` (`CentralClient`), so a change
on Central's side is a one-file edit here.

Every VM and Site event payload carries `central_reference` — the owning team,
resolved from the resource's `Tenant` (None for operator/e2e resources) — so
Central can attribute the event to a tenant without a reverse lookup.

## Reconcile (Central → Atlas)

Event delivery is fire-and-forget, so Central also **pulls** the authoritative VM
list periodically to correct drift. Atlas exposes one operator-only read for this:

| Central call | Atlas method | Returns |
| --- | --- | --- |
| reconcile mirror | `atlas.atlas.api.inventory.tenant_vms(central_reference?)` | `[ { name, central_reference, status, gateway_url } ]` |

It returns every tenant-tagged VM (optionally scoped to one `central_reference`);
untenanted operator VMs are never returned. This is the only Central→Atlas read;
all Central→Atlas *writes* reuse the existing whitelisted VM controller methods.
