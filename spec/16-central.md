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
   registration — standing up the tunnel and pushing a scoped service user back to
   Atlas ([21-tunnel.md](./21-tunnel.md)). Atlas no longer pushes `register`; it
   exposes the inbound `provision_tunnel` / `confirm_tunnel` surface Central drives.
   The authenticated service user is what attributes every inbound event to its
   cluster — Central resolves the sending Atlas from the session, with no separate id.
2. **VM Sizes.** Central owns the size catalog and **sends the concrete resource
   values per VM** when it provisions one: `create_vm`
   (`atlas/atlas/api/provision.py`) takes `vcpus` / `memory_megabytes` /
   `disk_gigabytes` / `cpu_max_cores` directly off the call. Atlas keeps no local
   catalog of Central's sizes; `atlas/atlas/sizes.py` `SIZE_PRESETS` remains only
   as the operator-convenience ladder behind the desk/dashboard New Machine picker.
3. **Bench images.** Atlas bakes each bench image with the existing Image Build
   pipeline — the `bench-v15` / `bench-v16` / `bench-nightly` recipes
   ([15-image-builder.md](./15-image-builder.md)) — and **promotes** each golden to
   a base image named exactly after its series (`bench-v15` etc., what each recipe's
   `promote_image_name` defaults to). Central selects the version per VM through the
   ordinary `image` field on `create_vm`; Atlas keeps no separate expected-image
   catalog.
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
  resource in one call: `atlas.atlas.api.provision.create_vm(team, …)` and
  `atlas.atlas.api.site.create_site(team, subdomain, …)`
  (the self-serve site, [14-self-serve.md](./14-self-serve.md)). Both return the
  mirror row Central reflects. Atlas adds no other inbound command endpoint.
- **Tenant is the attribution key.** The create endpoints get-or-create the
  `Tenant` ([02-doctypes.md § Tenant](./02-doctypes.md#tenant)) — named by the
  Central `Team.name` passed as `team` — and stamp it `set_only_once` on the
  resource. Atlas has **no
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
  `enabled` (master switch — event reporting is skipped when off), and a read-only
  `status` breadcrumb (the last register / event-delivery
  outcome — a glance-only convenience; the event *history* belongs to the planned
  `Central Event` log, not the Single). The region is read from `Atlas Settings.region`
  (`placement.atlas_region()`), not a Central Settings field. **Plus a Tunnel section**
  (`tunnel_ip`, `tunnel_cidr`, `hub_public_key`, `hub_endpoint`, `wg_public_key`,
  `wg_listen_port`, `tunnel_status`); `url` / `api_key` / `api_secret` now hold the
  **pushed per-Atlas Central service-user** creds — all written by Central's
  `provision_tunnel`, no longer hand-entered (registration is **Central-initiated**;
  see [21-tunnel.md](./21-tunnel.md)).

Atlas keeps **no local catalog of Central's sizes or expected images**: Central
sends a VM's resource values and `image` per provision call (`create_vm`), so
there is nothing to fetch and cache.

## Buttons (Central Settings → Actions ▾)

Each is a whitelisted controller method returning a plain dict for a toast,
exactly like `DigitalOceanSettings.test_connection`:

- **Test Connection** — `ping()`; green `OK` / red `Failed`.

There is **no Register button** anymore: registration is Central-initiated
([21-tunnel.md](./21-tunnel.md)). The inbound `provision_tunnel` / `confirm_tunnel`
methods (in `atlas/atlas/api/central_link.py`) are how Central registers this Atlas;
they are not operator buttons.

## Event reporting

Reporting is wired with `doc_events` in `hooks.py` (no controller edits) →
`atlas/atlas/central_report.py`. A status transition on a `Virtual Machine`,
`Site`, `Virtual Machine Snapshot`, or `Server`, and a VM / `Site` `after_insert`,
write a `Central Event Log` row and enqueue a background `deliver` job
(`enqueue_after_commit=True`, so a rolled-back transaction is never *delivered*).
The job POSTs to Central and stamps the log row plus the `Central Settings.status`
breadcrumb. Everything is gated on `Central Settings.enabled`, so a site without
Central configured pays nothing, and a delivery failure is logged to the Error Log
— it never blocks the operation.

**`Central Event Log` (audit trail).** Every emit is recorded as a row before
delivery is attempted (`event_type`, `payload`, `status` ∈
`pending`/`ok`/`error`/`skipped`, `attempts`, `last_error`, `http_status`,
`occurred_at`, and a `reference_doctype`/`reference_name` snapshot of the source).
The DocType is **MyISAM** (`engine`), like `Bench Routing Audit`: the INSERT is
non-transactional, so the row **survives a rollback** of the business change that
triggered it — you can always see what Atlas *tried* to emit, even for a reverted
save. `deliver` only runs on commit, so a rolled-back emit leaves its row at
`pending` and is never POSTed (log the attempt, skip the delivery).
`Central Settings.status` stays the at-a-glance breadcrumb; the log is the
queryable history.

The event types: `vm.created` / `vm.status_changed` / `vm.deleted`,
`site.created` / `site.status_changed`, `snapshot.completed`, and
`server.status_changed`. The **`site.status_changed` for `Running`** carries the
tenant handoff in its payload — the live `url`, the one-click `login_url` and its
`login_url_expires_at` ([14-self-serve.md](./14-self-serve.md)); earlier site transitions carry neither
(there is no handoff to give yet). `atlas.atlas.api.site.get_site(name)` is the
poll equivalent of the site events, returning the same shape for Central to
self-heal a missed delivery.

**Deferred (durable delivery).** Delivery is still fire-and-forget: an event is
lost (its `Central Event Log` row stays `pending`/`error`) if Central is down when
its job runs — the log records the miss but does not retry it. The planned upgrade
turns the log into a true outbox: a minutely `scheduler_events` job drains
`pending`/`error` rows (bumping `attempts`/`last_error`) for at-least-once
delivery. The `pending` state is deliberately ambiguous today — job-not-yet-run
vs. rolled-back-emit — which a drainer would disambiguate by age.

## The wire contract

**Atlas → Central (outbound, unaffected by the firewall).** Atlas calls Central's
whitelisted methods at `<url>/api/method/central.api.atlas.<name>` with
`Authorization: token <api_key>:<api_secret>` — where the creds are now the **pushed
per-Atlas service user** ([21-tunnel.md](./21-tunnel.md)). The methods Atlas expects:

| Atlas call | Central method | Returns |
| --- | --- | --- |
| `ping` | `central.api.atlas.ping` | `{ label }` |
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

Every VM and Site event payload carries `team` — the owning Central `Team.name`,
resolved from the resource's `Tenant` (None for operator/e2e resources) — so
Central can attribute the event to a tenant without a reverse lookup.

## Reconcile (Central → Atlas)

Event delivery is fire-and-forget, so Central also **pulls** the authoritative VM
list periodically to correct drift. Atlas exposes one operator-only read for this:

| Central call | Atlas method | Returns |
| --- | --- | --- |
| reconcile mirror | `atlas.atlas.api.inventory.tenant_vms(team?)` | `[ { name, team, status, gateway_url, login_url, login_url_expires_at } ]` |

It returns every tenant-tagged VM (optionally scoped to one `team`);
untenanted operator VMs are never returned. The wire shape stays **VM-shaped**, but
a bench VM's front door lives on the [Pilot](./02-doctypes.md#pilot) that owns it:
the handoff (`gateway_url` — the derived `https://<subdomain>.<region domain>` — plus
`login_url` and `login_url_expires_at`) is read *through* that Pilot, and is present
once the Pilot is `Running`; before then, and for an ordinary (non-bench) VM with no
Pilot, those are `None`. Because a bench `login_url` is a 5-minute single-use admin
session, Central re-mints it on **Open** (`get_bench_link`) when the stored URL has
expired, via the whitelisted `Pilot.regenerate_login_url()` method (which returns the
same VM-shaped payload). This is the only Central→Atlas read; all Central→Atlas
*writes* reuse the existing whitelisted controller methods.

**A self-serve site's Asset resolves the pilot console.** A `create_site` VM is backed
by **both** a `Site` (the customer's Frappe site) and an attached `Pilot` (a bench admin
console at `<subdomain>-pilot.<region>` on the same VM — see
[14-self-serve.md § The attached Pilot admin console](./14-self-serve.md)). The VM→front-door
resolver (`front_door_for_vm`) prefers the Pilot, so the **Asset's `gateway_url` / `login_url`
point at the console** (a 5-min admin JWT), while the customer's site handoff (its 24h
`bench browse` URL + live `url`) is surfaced separately through `get_site` /
`site.*` events. **Open** on a self-serve Asset therefore re-mints via the Pilot exactly
as a bench Asset does. (A Site-backed VM with no Pilot yet — mid-provision, or a directly
created Site — still resolves the Site's handoff as a fallback.)
