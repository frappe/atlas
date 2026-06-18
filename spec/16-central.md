# Central — the global control plane

Central is a global management dashboard for Frappe Cloud. One Central talks to
*many* Atlas instances, each running its own region and provider. Atlas is the
**client** of Central — the mirror image of the `Provider` relationship, where
Atlas is the client of a vendor (DigitalOcean, Scaleway).

This document describes the Atlas side of that seam. The Central app itself
lives in a different repository; the only contract Atlas depends on is a small
set of whitelisted HTTP methods (see *The wire contract* below).

## What Central does for Atlas

1. **Registration.** After setup, an Atlas registers itself on Central —
   announcing its region, active provider, and host site — and receives an
   `atlas_id` that Central uses to address it from then on.
2. **VM Sizes.** Today each Atlas hardcodes its size catalog
   (`atlas/atlas/sizes.py` `SIZE_PRESETS`). Central becomes the source of truth:
   Atlas **fetches sizes** from Central into a local `Central Size` catalog.
3. **Expected bench images.** Central declares which bench images each Atlas is
   *expected* to offer (V15, V16, Develop…). Atlas **fetches** that list into a
   local `Central Image` catalog. Central sets the *expectation*; Atlas still
   bakes each image with the existing Image Build pipeline
   ([15-image-builder.md](./15-image-builder.md)). `Central Image.bake_status`
   shows expectation-vs-reality per image.
4. **Event reporting.** Atlas reports every Virtual Machine lifecycle event
   (created / status changed / terminated), Snapshot completion, and Server
   state change back to Central, so the global dashboard reflects fleet state in
   near-real time.

## DocTypes

- **Central Settings** (single) — the credentials, this Atlas's identity, and
  the action buttons. Mirrors `DigitalOcean Settings`. Fields: `url`,
  `api_key`, `api_secret` (Password, `set_only_once`), `region`, `enabled`
  (master switch — event reporting is skipped when off), and the read-only
  `atlas_id` / `registered_on` / `last_sync` / `last_event_status` filled by the
  action methods.
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
- **Register** — `register()`; POSTs this Atlas's identity, stores the returned
  `atlas_id`.
- **Fetch Sizes** — `fetch_sizes()`; upserts `Central Size` rows
  (insert / update / disable-missing, same shape as `provider.upsert_catalog`).
- **Fetch Images** — `fetch_images()`; upserts `Central Image` rows.

## Event reporting

Reporting is wired with `doc_events` in `hooks.py` (no controller edits) →
`atlas/atlas/central_report.py`. A status transition on a `Virtual Machine`,
`Virtual Machine Snapshot`, or `Server`, and a VM `after_insert`, enqueue a
background `deliver` job (`enqueue_after_commit=True`, so a rolled-back
transaction is never reported). The job POSTs to Central and records the outcome
in `Central Settings.last_event_status`. Everything is gated on
`Central Settings.enabled`, so a site without Central configured pays nothing,
and a delivery failure is logged to the Error Log — it never blocks a VM
operation.

**Deferred (durable delivery).** v1 is fire-and-forget: an event is lost if
Central is down when its job runs. The planned upgrade is a `Central Event`
outbox DocType (`event_type`, `payload`, `status`, `attempts`, `last_error`)
drained by a minutely `scheduler_events` job for at-least-once delivery.

## The wire contract

Atlas calls Central's whitelisted methods at
`<url>/api/method/central.api.<name>` with
`Authorization: token <api_key>:<api_secret>`. The methods Atlas expects:

| Atlas call | Central method | Returns |
| --- | --- | --- |
| `ping` | `central.api.ping` | `{ label }` |
| `register` | `central.api.register` | `{ atlas_id, label }` |
| `fetch_sizes` | `central.api.sizes` | `[ { slug, title, vcpus, cpu_max_cores, memory_megabytes, disk_gigabytes, monthly_cost_usd } ]` |
| `fetch_images` | `central.api.images` | `[ { image_name, title, series } ]` |
| `post_event` | `central.api.event` | (ignored) |

The route names and payloads are the single external dependency; the whole
contract is absorbed in `atlas/atlas/central.py` (`CentralClient`), so a change
on Central's side is a one-file edit here.
