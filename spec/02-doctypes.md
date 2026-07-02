# DocTypes

Twenty-one DocTypes. Module `Atlas`. None are submittable. All track changes.
Read permission for `System Manager`.

1. [Atlas Settings](#atlas-settings) — vendor-agnostic Atlas config + the active `provider_type` / `tls_provider_type` (Single).
2. [DigitalOcean Settings](#digitalocean-settings) — DO API config (Single).
3. [Scaleway Settings](#scaleway-settings) — Scaleway Elastic Metal API config (Single).
4. [Self-Managed Settings](#self-managed-settings) — Self-Managed config (Single).
5. [Provider Size](#provider-size) — vendor catalog of machine sizes.
6. [Provider Image](#provider-image) — vendor catalog of OS images.
7. [Server](#server)
8. [Virtual Machine](#virtual-machine)
9. [Virtual Machine Image](#virtual-machine-image)
10. [Virtual Machine Snapshot](#virtual-machine-snapshot) — a disk snapshot of a VM.
11. [Reserved IP](#reserved-ip) — a public IPv4 allocated to a Server, optionally attached to a VM.
12. [Subdomain](#subdomain) — a `<subdomain>.<region>.frappe.dev` routing entry pointing at a site VM.
13. [Port Mapping](#port-mapping) — a raw-TCP forwarding entry: a proxy-side port pointing at a tenant VM's service port.
14. [SSH Key](#ssh-key) — a user's public key, chosen when creating a VM.
15. [Task](#task)
16. [Route53 Settings](#route53-settings) — AWS Route 53 API config (Single). The active DNS vendor lives on `Atlas Settings.dns_provider_type`.
17. [Lets Encrypt Settings](#lets-encrypt-settings) — ACME account config (Single); the active TLS issuer is `Atlas Settings.tls_provider_type`.
18. [Root Domain](#root-domain) — one wildcard zone == one region.
19. [TLS Certificate](#tls-certificate) — the issued regional wildcard cert.
20. [Site](#site) — a tenant's self-serve Frappe site at `<subdomain>.<region domain>`, created by Central via `create_site`. See [14-self-serve.md](./14-self-serve.md).

The **Provider abstraction** is a single ABC in
`atlas/atlas/providers/base.py` with one implementation per `provider_type`,
and a registry (`atlas/atlas/providers/__init__.py`) that maps a `provider_type`
straight to its class — there is no `Provider` DocType. The active vendor is
`Atlas Settings.provider_type`; `atlas.get_provider()` reads it and resolves the
class via `for_provider_type`. Every vendor call goes through that interface;
controllers never branch on `provider_type`. The vendor catalogs
([Provider Size](#provider-size) / [Provider Image](#provider-image)) and the
per-vendor Settings Singles round out the layer. See
[provider-abstraction.md](../llm/plan/provider-abstraction.md) for the
implementation plan.

The **TLS & Domain layer** ([13-tls.md](./13-tls.md)) — the producer for the
proxy's `push_cert` — mirrors that shape with two more registries keyed by type:
`atlas/atlas/dns/` (a `DnsProvider` ABC per `dns_provider_type`, the active
one on `Atlas Settings`) and `atlas/atlas/tls/` (a `TlsProvider` ABC per
`tls_provider_type`, also on `Atlas Settings`). Same rule: controllers
resolve an implementation by type and never branch on the vendor.

Each DocType is specified by three sections: **Fields** (the schema), **Form
layout** (the section/column structure of the desk form), and **List view**
(column order and standard filters). Together these are enough to
regenerate the JSON without consulting the implementation.

Notation in the Form layout sections:

- `── <label> ──` is a Section Break with that label.
- `(collapsible)` after a section label means the section is collapsed by
  default.
- `|` is a Column Break inside a section. Fields after `|` lay out in the
  next column.

---

## Atlas Settings

A Single DocType. Holds Atlas-wide configuration that is not vendor-specific:
which `provider_type` is currently active, which `tls_provider_type` issues
certs, and the operator's SSH key (public-key body + on-disk private-key path).
The *vendor's* handle for that key (`ssh_key_id`) is vendor-specific and lives on
the per-vendor Settings instead (see DigitalOcean / Scaleway Settings below).
Every `get_provider()` call in the codebase reads `Atlas Settings.provider_type`
and resolves the implementation via `for_provider_type` — this is the
indirection layer; there is no `Provider` row to load.

### Fields

| Field                  | Type             | Reqd | Notes                                                              |
| ---------------------- | ---------------- | ---- | ------------------------------------------------------------------ |
| `region`               | Data             | Y    | This Atlas instance's single region (e.g. `blr1`, `nyc3`) — the **one source of truth** for region, and the **only** place region is stored. It is the cert-dir scope on every proxy guest, the label that separates this bench's servers in a shared cloud account (`provisioning.provision_region`), the value `Root Domain` denormalizes at insert, and the region announced to Central at Register. `Subdomain` / `Site` / `Port Mapping` / proxy VMs carry **no** denormalized copy — they belong to the one region by definition. Read everywhere through `placement.atlas_region()`, which fails loud when unset. Set by the setup contract (`setup.run` / the Setup Wizard) from the explicit `region` input; the `from_site_config` adapter / `bootstrap.py` derive it from `atlas_tls_region` (else the active vendor's region key). Atlas is single-region, so there is exactly one value. |
| `provider_type`        | Select           | Y    | The currently-active vendor: `DigitalOcean` / `Scaleway` / `Self-Managed` (`Fake` is also an option in `developer_mode` / test builds). `atlas.get_provider()` reads this and calls `for_provider_type` against the registry directly. Guarded in `validate()` (see below). |
| `tls_provider_type`    | Select           |      | The active certificate issuer: `Let's Encrypt` / `ZeroSSL` / `Self-Managed`. Drives the TLS registry (`tls.for_tls_provider_type`); denormalized onto `Root Domain` / `TLS Certificate` at insert. See [13-tls.md](./13-tls.md). |
| `dns_provider_type`    | Select           |      | The active DNS vendor (DNS-01 challenge): `Route53` / `Cloudflare`. Keys the DNS registry (`dns.for_dns_provider_type`); denormalized onto `Root Domain` at insert. Pairs with `Route53 Settings` for the credentials. Only Route53 implemented; Cloudflare reserved. See [13-tls.md](./13-tls.md). |
| `fail_scripts`         | Small Text       |      | Developer-mode fault injection, shown only when `provider_type=Fake`. Comma/newline-separated script names whose Tasks the Fake provider makes FAIL; `*` fails every script. Read by `fake_tasks._configured_failure` (replaces the old per-`Provider`-row field). |
| `default_user_image`   | Link → Virtual Machine Image | | Base image a dashboard user's new machine provisions from when they don't pick one. Disambiguates placement when several images are active. See [11-user-ui.md](./11-user-ui.md). |
| `default_bench_snapshot` | Link → Virtual Machine Snapshot | | The golden bench snapshot a self-serve `Site`'s backing VM is cloned from (the baked bench + MariaDB + Redis, [08-images.md § golden bench image](./08-images.md)). `Site.before_insert` placement resolves it; provisioning clones via `Virtual Machine Snapshot.clone_to_new_vm`. Must be set + `Available` before any Site is created. See [14-self-serve.md](./14-self-serve.md). |
| `overprovision_factor` | Float            |      | Fleet-wide vCPU oversubscription multiplier (default `1`). A host's *effective* vCPU budget — what `default_server` placement and the desk capacity helper check against — is its physical vCPU total times this factor. `1` means no oversubscription. Safe to raise because a VM's `vcpus` is a `cpu.max` *bandwidth* cap, not a pinned core. A host whose size has no known vCPU total (uncatalogued slug or self-managed) is unaffected — it always counts as having room. See [server_capacity.py](../atlas/atlas/api/server_capacity.py) and `placement.py`. |
| `tcp_port_pool`        | Data             |      | Inclusive `LOW-HIGH` range (default `10000-19999`) of proxy-side ports the TCP forwarder allocates to `Port Mapping`s; must match the proxy nginx `listen` range. See [17-tcp-proxy.md](./17-tcp-proxy.md). |
| `ssh_public_key`       | Long Text        |      | OpenSSH public key body. Crosses the provider interface for vendors that upload at provision time. Not required for DO. |
| `ssh_private_key_path` | Data             | Y    | Absolute path on the Atlas host where the matching private key lives. Atlas reads the PEM at SSH-connect time via `secrets.get_ssh_key_from_disk(path)`. `0600`, owned by the Frappe user. |

Why these live on one Single instead of per-vendor rows: the SSH *keypair* (the
public-key body and the on-disk private key), the active `provider_type`, the
active `tls_provider_type`, and any other cross-vendor switch are properties of
the Atlas instance, not of a vendor. The vendor's *handle* for that key
(`ssh_key_id` — DO's key id / fingerprint, Scaleway's IAM id) is the exception:
it is meaningless outside its vendor, so it lives on the per-vendor Settings.
`get_ssh_key()` assembles the two — the cross-vendor public key from this Single,
the `vendor_id` from the active vendor's Single — so callers still see one
`SshKey`. Routing reads through a single helper also lets the storage backend
swap to an external secret store later without touching callers.

### Form layout

```
── Active provider ──
provider_type
| tls_provider_type
  dns_provider_type
  fail_scripts
── User dashboard ──
default_user_image
default_bench_snapshot
── Capacity ──
overprovision_factor
── TCP proxy ──
tcp_port_pool
── SSH key ──
ssh_public_key
| ssh_private_key_path
```

### Buttons

The compute-provider actions live here (there is no `Provider` form):

- **Provision Server** (primary) — opens a dialog. Common field:
  `title` (lowercase + digits + hyphens, max 63 chars; passed through
  to the vendor as the server's name and tag). The remaining inputs
  are produced by the active provider implementation's `discover()`-backed
  dialog schema:
  - **DigitalOcean**: `size` (Link → Provider Size, filtered to
    `provider_type=DigitalOcean, enabled=1`), `image` (Link → Provider
    Image, same filter), defaulting to the `Provider Size` / `Provider
    Image` row marked `is_default` for the provider type. Then
    `confirm_cost` ("Create a billable droplet?") before the DO API call.
  - **Scaleway**: identical to DigitalOcean — `size` / `image` Links
    filtered to `provider_type=Scaleway, enabled=1`, defaulting to the
    `is_default` catalog rows, then `confirm_cost`.
    Scaleway is "a vendor with an API," so it takes the same dialog path as
    DigitalOcean; only the catalog filter and the cost copy differ. Provision
    is async (the Elastic Metal create returns `delivering`; the worker polls
    `describe()` until `ready` + install `completed`, see
    [03-bootstrapping.md](./03-bootstrapping.md)).
  - **Self-Managed**: `ipv4_address`, `ipv6_address`, `ipv6_prefix`,
    `ipv6_virtual_machine_range`. Atlas inserts the `Server` directly
    with the operator-supplied values and runs bootstrap. No API call.
  The whitelisted `provision_server(title, ...)` controller method
  returns the new Server row's UUID `name`. See
  [03-bootstrapping.md](./03-bootstrapping.md).
- **Authenticate** — under `Actions ▾`. Calls `provider.authenticate()`,
  which probes the vendor (DigitalOcean: `GET /account`) and reports
  back account label, rate-limit headers, and `missing_scopes`. Hidden
  for vendors without remote credentials (Self-Managed returns
  `ok=True, account_label="local"` so the form still paints a green
  chip on refresh).
- **Refresh Catalog** — under `Actions ▾`. Calls `provider.discover()`
  and upserts `Provider Size` and `Provider Image` rows (the controller
  method, renamed from `discover_and_upsert`). Slugs the vendor no longer
  returns are flipped to `enabled=0`; historical Server rows keep their Link.
- **Discover Servers** — under `Actions ▾`. Calls `discover_servers()`
  (read-only) to list the vendor's servers — the *unfiltered*
  `provider.list_servers()`, so a box built outside Atlas is found — and
  dedups each against existing `Server.provider_resource_id`. The picker lets
  the operator tick which to adopt; `import_servers(resource_ids)` re-resolves
  each via `describe()` and inserts a `Pending` Server row (already-modeled ids
  are skipped). Self-Managed has no discovery (`list_servers()` returns `()`).
  See [03-bootstrapping.md § Adopting an already-provisioned
  server](./03-bootstrapping.md#adopting-an-already-provisioned-server).

There is no auto-painted credential indicator; the operator clicks
**Authenticate** when they need to verify.

Switching the active vendor is editing `provider_type` and saving;
`atlas.get_provider()` picks up the new value on the next call. There is no
**Archive** — you don't archive your only provider. `validate()` **refuses to
change `provider_type` while any non-Archived `Server` carries a different
`provider_type`**: that is the Single-world equivalent of "switching the active
provider doesn't destroy existing Server rows" — each historical Server keeps
its own `provider_type` (the vendor it was provisioned through), so the active
type can only move once the fleet has been migrated/archived off the old vendor.

---

## DigitalOcean Settings

A Single DocType. Only fields that DigitalOcean's API needs.

### Fields

| Field           | Type                  | Reqd | Notes                                                              |
| --------------- | --------------------- | ---- | ------------------------------------------------------------------ |
| `api_token`     | Password              | Y    | `set_only_once`. DigitalOcean personal access token. Rotate by clearing the field via `db.set_value`, then re-saving. |
| `region`        | Data                  | Y    | DO is multi-region; Atlas is single-region. Pick one (`blr1`, `nyc3`, …). `provision_server` throws if the dialog overrides this. |
| `ssh_key_id`    | Data                  |      | DO's handle for the uploaded SSH key, installed on each new droplet. Accepts the key's numeric id or its SHA-256 fingerprint. Passed through to the provider as `SshKey.vendor_id`; `get_ssh_key()` reads it from here when DO is active. Vendor-specific — meaningless to other providers. |

The default size/image are **not** fields here. The Provision dialog's default
comes from the `Provider Size` / `Provider Image` row marked `is_default` (see
[Provider Size](#provider-size)). `discover()` hints one (`s-2vcpu-4gb-intel` /
`ubuntu-24-04-x64`); the operator's `atlas_do_default_*` config keys override the
hint at setup, and the operator can flip the default on the catalog list anytime.

### Form layout

```
api_token
region
ssh_key_id
```

### Buttons

- **Test Connection** — under `Actions ▾`. Calls
  `DigitalOceanProvider.authenticate()` (same as Atlas Settings'
  Authenticate button, mirrored here for the operator who's mid-credentials).
  Result surfaces via a toast (`OK: <account>` / `Failed: <error>`);
  there is no auto-painted dashboard indicator.

Monthly cost preview for the Provision dialog reads `Provider Size.monthly_cost_usd`
directly. Sizes without a cost render as "—" rather than guess.

---

## Scaleway Settings

A Single DocType. Only fields Scaleway's Elastic Metal (bare-metal) API needs.
Mirrors `DigitalOcean Settings` — same shape, vendor-specific fields.

### Fields

| Field             | Type                  | Reqd | Notes                                                              |
| ----------------- | --------------------- | ---- | ------------------------------------------------------------------ |
| `secret_key`      | Password              | Y    | `set_only_once`. IAM API key **Secret Key** — the `X-Auth-Token` value, an opaque hyphenated string (NOT the Access Key). Rotate by clearing via `db.set_value`, then re-saving. |
| `project_id`      | Data                  | Y    | Scaleway Project UUID every resource is scoped to. The default project's id equals the Organization id; non-default projects differ. |
| `organization_id` | Data                  |      | Optional. Filters `GET /account/v3/projects` and labels the authenticate result. |
| `zone`            | Data                  | Y    | Scaleway is multi-zone; Atlas is single-region per vendor. One Elastic Metal zone (`fr-par-1`, `fr-par-2`, `nl-ams-1`, `nl-ams-2`, `pl-waw-2`, `pl-waw-3` — **not** `pl-waw-1`). |
| `billing`         | Select                |      | `hourly` (default) / `monthly`. Hourly has no upfront fee; monthly is cheaper to run but charges a one-time, non-refundable commitment fee. Hourly and monthly are **distinct offer ids**, so `discover()` filters offers to this mode. |
| `ssh_key_id`      | Data                  |      | Scaleway's IAM SSH key id, installed on each new Elastic Metal server. Left blank, the provider registers `Atlas Settings.ssh_public_key` with IAM at provision time; an operator with a cached IAM id can set it here to reuse the key. Read as `SshKey.vendor_id` by `get_ssh_key()` when Scaleway is active. Vendor-specific. |

The default size/image are **not** fields here — the Provision dialog's default
comes from the `is_default` `Provider Size` / `Provider Image` row, exactly as for
DigitalOcean. `discover()` hints one (cheapest offer / Ubuntu LTS); the operator's
`atlas_scw_default_*` config keys override the hint at setup, and the operator can
flip the default on the catalog list anytime.

### Form layout

```
secret_key
project_id
organization_id
zone
billing
ssh_key_id
```

### Buttons

- **Test Connection** — under `Actions ▾`. Calls `ScalewayProvider.authenticate()`
  (lists projects via `GET /account/v3/projects`). Result surfaces via a toast
  (`OK: <project>` / `Failed: <error>`), no auto-painted indicator — mirrors
  the DigitalOcean Settings form.

The Scaleway networking model (the VM range is a routed **flexible IPv6 `/64`**
the provider allocates — the bundled subnet is on-link, not routed — handed whole
with no DigitalOcean-style `/124` carve, and inbound IPv4 via a routed **Flexible
IP** rather than a DO anchor) is in [06-networking.md](./06-networking.md).

---

## Self-Managed Settings

A Single DocType. Empty stub today — Self-Managed has no vendor-side
configuration (everything Atlas needs for a Self-Managed host lives in
`Atlas Settings` plus the operator-supplied IPs at provision time).
The DocType exists so future Self-Managed-only knobs have a home; the
form ships with a single section break and no fields.

---

## Provider Size

A regular DocType. One row per vendor-advertised machine size that Atlas
is willing to provision. Seeded at first run by `bootstrap.py` and
refreshed via the Atlas Settings form's **Refresh Catalog** button (which
calls `provider.discover()`).

### Fields

| Field               | Type   | Reqd | Read-only | Default | Notes                                                              |
| ------------------- | ------ | ---- | --------- | ------- | ------------------------------------------------------------------ |
| `name`              | Data   | Y    | Y         |         | Primary key. Format: `{provider_type}/{slug}` (e.g. `DigitalOcean/s-2vcpu-4gb-intel`). Assigned in `autoname()`. |
| `provider_type`     | Select | Y    |           |         | Same options as `Atlas Settings.provider_type`. `set_only_once`.   |
| `slug`              | Data   | Y    |           |         | Vendor-native slug — the string sent on the API wire (`s-2vcpu-4gb-intel`). `set_only_once`. |
| `enabled`           | Check  |      |           | 1       | Flipped by `discover()` when the vendor drops a slug. Disabled rows do not appear in the Provision dialog but remain pointable from historical Server rows. |
| `is_default`        | Check  |      |           |         | The size the Provision Server dialog prefills. **At most one per `provider_type`** — the controller's `validate()` clears `is_default` on every sibling when a row sets it. Filled by `discover()`'s hint into an empty slot, overridden by the `atlas_*_default_size` config key at setup, and flippable by the operator anytime. The resolver (`setup_catalog.default_name`) returns the marked row's `name`. |
| `monthly_cost_usd`  | Int    |      |           |         | Hand-maintained for vendors without per-size pricing in the API (DO). Renders as "—" when blank. |
| `provider_metadata` | Code (JSON) |  | Y      |         | Raw vendor response for this size — vCPU count, RAM, disk tier, anything the vendor returns. Read-only on the form. |

### List view

- Columns: `slug`, `provider_type`, `enabled`, `is_default`, `monthly_cost_usd`.
- Standard filters: `provider_type`, `enabled`, `is_default`.

---

## Provider Image

A regular DocType. One row per vendor-advertised OS image that Atlas is
willing to provision a server with. Same lifecycle as `Provider Size`.

Distinct from `Virtual Machine Image`: this is the *server's* base image
(the OS Atlas bootstraps on top of), not the guest rootfs+kernel pair
that runs inside a Firecracker microVM.

### Fields

| Field               | Type   | Reqd | Read-only | Default | Notes                                                              |
| ------------------- | ------ | ---- | --------- | ------- | ------------------------------------------------------------------ |
| `name`              | Data   | Y    | Y         |         | Primary key. Format: `{provider_type}/{slug}` (e.g. `DigitalOcean/ubuntu-24-04-x64`). |
| `provider_type`     | Select | Y    |           |         | `set_only_once`.                                                   |
| `slug`              | Data   | Y    |           |         | Vendor-native slug (DO `ubuntu-24-04-x64`, future AWS `ami-…`). `set_only_once`. |
| `enabled`           | Check  |      |           | 1       | Flipped by `discover()`.                                           |
| `is_default`        | Check  |      |           |         | The image the Provision Server dialog prefills. At most one per `provider_type` — same one-default invariant and precedence as [Provider Size](#provider-size)'s `is_default`. |
| `provider_metadata` | Code (JSON) |  | Y      |         | Raw vendor response — architecture, distribution, release date, …  |

### List view

- Columns: `slug`, `provider_type`, `enabled`, `is_default`.
- Standard filters: `provider_type`, `enabled`, `is_default`.

---

## Server

One row per host. The primary key is a UUID assigned at insert; the
operator-facing label lives in `title` (e.g. `server-blr1-01`).

### Fields

| Field                          | Type                  | Reqd | Read-only | Default | Notes                                                          |
| ------------------------------ | --------------------- | ---- | --------- | ------- | -------------------------------------------------------------- |
| `name`                         | UUID (autoname)       | Y    | Y         |         | Primary key. UUID minted in `Server.autoname()`. Stable for the row's lifetime; no rename UI. |
| `title`                        | Data                  | Y    |           |         | Operator-chosen label. `set_only_once` — first save freezes it. |
| `provider_type`                | Select                | Y    | Y         |         | The vendor this host was provisioned through (`DigitalOcean` / `Scaleway` / `Self-Managed`). Copied from `Atlas Settings.provider_type` at insert and frozen (`set_only_once`); it does **not** follow a later change to the active type — a historical Server keeps its own vendor. `in_list_view`, `in_standard_filter`. |
| `status`                       | Select                | Y    | Y         | Pending | `Pending`, `Bootstrapping`, `Active`, `Draining`, `Broken`, `Archived`. Controllers mutate via `db.set_value`. |
| `provider_resource_id`         | Data                  |      | Y         |         | Vendor's primary key for this host (DigitalOcean droplet id, future AWS instance id, …). Empty for `Self-Managed`. Locked once written. |
| `size`                         | Link → Provider Size  |      | Y         |         | Populated by `provider.describe()` after provision. Empty for `Self-Managed`. |
| `image`                        | Link → Provider Image |      | Y         |         | Server's base OS image. Populated by `provider.describe()`. Empty for `Self-Managed`. |
| `ipv4_address`                 | Data                  |      | Y         |         | The SSH endpoint. Set by `provider.describe()` (DigitalOcean) or by the operator at provision time (Self-Managed). Locked once written. |
| `ipv6_address`                 | Data                  |      | Y         |         | The server's own IPv6. Whatever the host actually answers on. |
| `ipv6_prefix`                  | Data                  |      | Y         |         | The full prefix routed to this server (typically a /64). Informational. |
| `ipv6_virtual_machine_range`   | Data                  |      | Y         |         | The subnet Atlas allocates VM addresses from. Any prefix length: `/64`, `/80`, `/124`, ... Produced by `provider.describe()`. For `DigitalOcean` it's the /124 carved from the /64 (see [06-networking.md](./06-networking.md)); for `Self-Managed` it's the operator-typed value. |
| `provider_metadata`            | Code (JSON)           |      | Y         |         | Raw vendor blob returned by `describe()`. Holds anything the vendor reports that doesn't have a named column (DigitalOcean `created_at`, future AWS placement group, …). Forward-compatibility seam — read-only. |
| `architecture`                 | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `firecracker_version`          | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `jailer_version`               | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `kernel_version`               | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |

Atlas is single-region: there is no `Server.region` column. The
instance's region is `Atlas Settings.region` (the single source of
truth, read through `placement.atlas_region()`). That is distinct from a
vendor's *operating* region — a vendor that operates in multiple regions
stores the API region it provisions into on its own Settings Single
(e.g. `DigitalOcean Settings.region`, `Scaleway Settings.zone`), and one
Atlas instance pins one region per vendor. The two normally hold the same
string; `Atlas Settings.region` answers "which proxy fleet / wildcard
zone", the vendor key answers "which datacenter the API creates the box in".

Immutability is enforced by `Server._validate_immutability()` (lock
once a value is written; allow `None → value` so `finish_provisioning`
can write the IPs, size, image, and `provider_metadata` onto a freshly
inserted Pending row). The framework `set_only_once` flag covers
`title` and `provider_type` because those are populated at insert time and
never legitimately change. Stamping `provider_type` at insert (rather than
linking the active provider) is what preserves the invariant "switching the
active provider doesn't destroy existing Server rows" — see
[Atlas Settings](#atlas-settings).

### Controller methods

- `archive()` — calls `provider.destroy(provider_resource_id)` first
  (no-op for Self-Managed, releases the droplet for DigitalOcean), then
  sets `status = "Archived"` via `db.set_value`. Idempotent (rejects
  if already Archived). Existing FKs from Virtual Machine and Task rows
  are preserved.
- `sync_image(image)` — single-server convenience wrapper around
  `Virtual Machine Image.sync_to_server(self.name)`. Used by the
  Server form's Sync Image action.
- `bootstrap()` / `reboot()` / `get_scripts()` / `run_task_dialog(...)`
  — Task-running entry points; see [04-tasks.md](./04-tasks.md).

The split between `ipv6_prefix` and `ipv6_virtual_machine_range` is
because on DigitalOcean a /64 is advertised but only the first /124 is
actually routable; we hand out addresses inside that /124 only. On
Self-Managed hosts the operator might have an entire extra /64 (or /80,
or /48) routed to the box and so the VM range can be much larger than
/124. Atlas treats `ipv6_virtual_machine_range` as "the subnet I am
allowed to allocate from" and does not try to derive it. Details in
[06-networking.md](./06-networking.md).

### Form layout

Single `Overview` tab. Networking / Host info / Notes are collapsible
sections, not separate tabs.

```
── Overview ──
title
provider_type
| status
── Provider resource ──
provider_resource_id
| size
  image
── Networking (collapsible) ──
ipv4_address
ipv6_address
| ipv6_prefix
  ipv6_virtual_machine_range
── Host info (collapsible) ──
architecture
| firecracker_version
  jailer_version
  kernel_version
── Provider metadata (collapsible) ──
provider_metadata
```

### List view

- Columns (left to right): `title`, `provider_type`, `status`, `size`,
  `ipv4_address`.
- Standard filters: `provider_type`, `status`, `size`.

### Buttons

- **Bootstrap** (primary on `Pending` / `Bootstrapping` / `Broken`;
  folds under `Actions ▾` as **Re-bootstrap** on `Active`) — runs
  [`scripts/bootstrap-server.py`](../scripts/bootstrap-server.py).
  Idempotent.
- **Sync Image** (under `Actions ▾`, on `Active`) — opens a one-field
  dialog (Link to `Virtual Machine Image`) and calls
  `Server.sync_image(image)`. There is no operator-driven "Run Task"
  catch-all on the form; lifecycle scripts that aren't a first-class
  button live on the relevant DocType (VM start/stop on the VM form,
  etc.). The `run_task_dialog` controller method is kept for
  `Task.retry` only.
- **Archive** (under `Actions ▾`, on non-`Archived` rows, danger) —
  confirms via a type-the-title dialog. Calls
  `provider.destroy(provider_resource_id)` first (releases the
  DigitalOcean droplet; no-op for Self-Managed), then sets
  `status = "Archived"`. Archive is the destroy trigger — there is no
  separate Destroy button. The dialog body warns that the vendor
  resource will be released.
- **Reboot** (under `Actions ▾`, danger) — runs
  [`scripts/reboot-server.sh`](../scripts/reboot-server.sh)
  (`systemctl reboot` over SSH). The resulting Task may end in `Failure`
  (SSH drops before the script returns) or `Success` (`systemctl reboot`
  exits before the connection is torn down). Either outcome is normal; the
  meaning is "the server is rebooting." Operators confirm reboot by
  watching for SSH to come back, not by reading the Task status. The
  desk requires the operator to type the server title into a
  text-match dialog before the red button enables — see
  [10-desk-ui.md](./10-desk-ui.md).

Frappe's standard Connections dashboard renders below the form, linking
Virtual Machines and Tasks (under **Operations**) and the Server's
[Reserved IP](#reserved-ip) pool (under **Networking**) via their `server`
field (configured in `server_dashboard.py`). The desk's bespoke "Recent Tasks"
quick_list has been removed — Operations on the Connections dashboard already
exposes the same information.

A Server's **Reserved IP pool** is the set of `Reserved IP` rows whose `server`
is this host — public IPv4 addresses bound to the host (DigitalOcean reserved
IPs), each either unattached (`Allocated`) or attached to one of this Server's
VMs (`Attached`). The host's own SSH endpoint is the separate `ipv4_address`
field above; a Reserved IP is an *additional*, attachable address, not the
host's primary v4.

---

## Virtual Machine

One row per microVM. The primary key is a UUID assigned at insert and never
changes — not even on terminate. Predictable, stable identity that survives
deletion.

### Fields

| Field              | Type                          | Reqd | Read-only | Default | Notes                                                            |
| ------------------ | ----------------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`             | UUID                          | Y    | Y         |         | Primary key. Set in `before_insert` via `uuid.uuid4()`.          |
| `title`            | Data                          | Y    |           |         | Operator-chosen label; `title_field` for the form. `set_only_once`. |
| `server`           | Link → Server                 | Y    |           |         | `set_only_once` (in addition to the controller's `_validate_immutability`). |
| `image`            | Link → Virtual Machine Image  | Y    |           |         | `set_only_once` (in addition to the controller's `_validate_immutability`). |
| `status`           | Select                        | Y    | Y         | Pending | `Pending`, `Running`, `Paused`, `Stopped`, `Failed`, `Terminated`. Driven by lifecycle methods only. |
| `vcpus`            | Int                           | Y    |           | 1       | Guest `vcpu_count` — the number of vCPU threads Firecracker boots. Whole number ≥ 1 (a guest can't boot on a fractional thread count); it is also what server-capacity accounting sums for the thread budget. CPU *bandwidth* below or at one core is set by `cpu_max_cores`, not here. Frozen on ordinary saves; mutable via `resize()` on a Stopped VM. No `set_only_once` (the controller is the gate). |
| `cpu_max_cores`    | Float                         | Y    |           | 1       | The VM's guaranteed CPU bandwidth in whole-core units (see [networking.cgroup_args](../atlas/atlas/networking.py)). Fractional for sub-1 sizes: `0.0625` is 1/16 of a core. Defaults to `vcpus` (whole-core behavior) when a caller sets only `vcpus` — the operator desk path, the bootstrap seed, direct API. The size presets ([sizes.py](../atlas/atlas/sizes.py)) set both. How it is *enforced* depends on `cpu_mode`. It is also the *bandwidth cost* server-capacity accounting sums for oversubscription (unchanged by the mode — see [09 § overprovision](./09-roadmap.md)). Same resize rule as `vcpus`; baked into the per-VM jailer launcher at provision time, so a changed value takes effect on re-provision (see [05 § Resize](./05-virtual-machine-lifecycle.md#resize)). |
| `cpu_mode`         | Select                        | Y    |           | Hard cap | How `cpu_max_cores` is enforced. `Hard cap` (default, the original behavior): `cpu_max_cores` is a hard cgroup `cpu.max` ceiling — the VM never exceeds its share, even on an idle host. `Relaxed`: `cpu_max_cores` becomes a guaranteed share via cgroup `cpu.weight` (the proportional floor *under contention*) and `cpu.max` is loosened to `vcpus` whole cores, so the VM **bursts into spare host CPU when the host is idle** and degrades to its share when busy. The loose ceiling keeps a single busy VM from monopolizing the host and keeps capacity accounting honest (it still bills the `cpu_max_cores` share). Same resize rule as `vcpus`; takes effect on re-provision. This is the spec's earlier "hybrid (model 2)" CPU-burst plan, now shipped — see [09 § CPU bursting](./09-roadmap.md). |
| `memory_megabytes` | Int                           | Y    |           | 512     | Same resize rule as `vcpus`.                                     |
| `disk_gigabytes`   | Int                           | Y    |           | 4       | Same resize rule. Resize may only grow it.                       |
| `data_disk_gigabytes` | Int                        |      |           | 0       | Optional second writable disk (the guest's `/dev/vdb`). `0` = none. Set at create; resize may only **grow** it (0→N is not a resize — recreate the VM). A first-class peer of the root disk: snapshotted, restored, cloned, terminated alongside it. |
| `data_disk_format_and_mount` | Check               |      |           | 1       | Format the data disk `ext4` (label `atlas-data`) and mount it at the mount point. Uncheck to attach a raw, unformatted/unmounted block device. Takes effect when the disk is first created. `depends_on: data_disk_gigabytes`. |
| `data_disk_mount_point` | Data                    |      |           | /home   | Where the data disk mounts inside the guest, via an `/etc/fstab` `LABEL=atlas-data` line. `depends_on: data_disk_gigabytes && data_disk_format_and_mount`. |
| `ssh_public_key`   | Long Text                     | Y    |           |         | `set_only_once`. Injected into the rootfs.                       |
| `stop_protection`  | Check                         |      |           | 0       | When set, `stop()` refuses to stop the VM (and therefore `restart()`, which stops first). Off by default. The operator unchecks and saves before stopping — a deliberate two-step guard, the same shape as the immutability throws. Independent of `termination_protection`. |
| `termination_protection` | Check                   |      |           | 0       | When set, `terminate()` refuses to terminate the VM. Off by default. Unchecked + saved before terminate. Independent of `stop_protection` (terminate does not go through `stop()`). |
| `memory_snapshot_on_stop` | Check                  |      |           | 0       | Opt in: `stop()` captures the VM's full memory state so the next Start resumes in milliseconds instead of cold-booting. **Restart then power-cycles back to the saved state rather than rebooting the guest** (`restart(cold=True)` for a true reboot). Off by default — the plain stop and cold boot remain the default path. Falls back to the plain stop on any snapshot failure. See [05 § Memory snapshots](./05-virtual-machine-lifecycle.md#memory-snapshots-fast-stop--start). |
| `has_memory_snapshot` | Check                      |      | Y         | 0       | The last stop captured a complete memory snapshot; the next Start resumes from it. Bookkeeping, not authority — the on-host `READY` marker decides at start time. Cleared when the snapshot is consumed (start) or invalidated (rebuild, resize, host-key rotation). |
| `clone_source_rootfs` | Data                       |      | Y         |         | Internal, hidden. On-host snapshot rootfs to seed this VM's disk from (clone). Empty for a normal image-backed VM. `set_only_once`, `no_copy`. |
| `clone_source_data_rootfs` | Data                  |      | Y         |         | Internal, hidden. On-host data-disk snapshot to seed this VM's data disk from (clone). Empty for a normal VM. `set_only_once`, `no_copy`. |
| `warm_snapshot`    | Link → Virtual Machine Snapshot |    | Y         |         | Internal, hidden. The `Warm` snapshot this VM restores from (provision stages the memory pair + MMDS identity; see [05 § Warm snapshot fan-out](./05-virtual-machine-lifecycle.md#warm-snapshot-fan-out-one-golden-n-restored-clones)). Empty for every ordinary VM. `set_only_once`, `no_copy`. |
| `build_mode`       | Select (`site`/`admin`)       |      | Y         |         | Internal, hidden. The bench bake mode this VM should deploy in — carried build VM → snapshot → clone, OR inherited from the base image when the VM is created from a promoted bench golden (`set_build_mode_default`) — so first-boot `deploy_site` maps the FQDN to the baked site (`site`) or the admin console (`admin`). Empty for an ordinary image-backed VM (treated as `site`). `set_only_once`, `no_copy`. See [08-images.md § golden bench image](./08-images.md#the-golden-bench-image-self-serve). |
| `ipv6_address`     | Data                          |      | Y         |         | From the server's /124. Set in `before_insert`.                  |
| `public_ipv4`      | Data                          |      | Y         |         | The attached public IPv4, denormalized from the `Reserved IP` row whose `virtual_machine` points here. Empty until one is attached. Maintained by `Reserved IP.attach()` / `detach()` (and cleared on terminate); never hand-edited. See [Reserved IP](#reserved-ip) and [06-networking.md](./06-networking.md). |
| `mac_address`      | Data                          |      | Y         |         | Derived from `name`. Set in `before_validate`.                   |
| `tap_device`       | Data                          |      | Y         |         | Derived from `name`. Set in `before_validate`.                   |
| `is_proxy`         | Check                         |      |           | 0       | Marks this VM as a reverse-proxy node. A proxy VM fronts the fleet's [Subdomain](#subdomain)s and is reconciled by the proxy control plane. It is an *ordinary* operator-owned VM (no infra tier) running the proxy image with an attached `public_ipv4`. The region it serves is this Atlas's single `Atlas Settings.region` — proxy VMs carry no denormalized `region`. See [12-proxy.md](./12-proxy.md). |
| `last_started`     | Datetime                      |      | Y         |         |                                                                  |
| `last_stopped`     | Datetime                      |      | Y         |         |                                                                  |

Because the name is a UUID, the operator needs `title` to recognize a
VM in lists. The framework's `title_field` points at it; the browser
tab, breadcrumb, and list-view subject all read `title`.

`status` is read-only on the form because it is only ever set by lifecycle
methods (Provision/Start/Stop/Restart/Terminate); see
[05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md).

`ssh_public_key` is the key injected into the *guest's*
`/root/.ssh/authorized_keys` — it is how the operator SSHes into the
VM, not into the host. The host key lives on `Atlas Settings`
(`ssh_public_key`, `ssh_private_key_path`); the vendor's handle for it
(`ssh_key_id`) lives on the active vendor's Settings.

### Auto-provision contract

`Virtual Machine.after_insert` enqueues
`atlas.atlas.doctype.virtual_machine.virtual_machine.auto_provision`
on the `long` queue; the worker resolves the VM by name, checks
`status == "Pending"`, and calls `provision()`. The operator clicks
**Save**, not **Provision** — the form's Pending state no longer
carries a primary action. A failed auto-provision flips the VM to
`Failed`, at which point the form's **Provision** primary returns as
a retry. See [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md).

### Form layout

A single `Overview` Tab Break with the rest as collapsible Section
Breaks (the old `Networking` / `Activity` tabs folded in):

```
title
server
image
| status
── Resources ──
vcpus
cpu_max_cores
cpu_mode
| memory_megabytes
| disk_gigabytes
data_disk_gigabytes
data_disk_format_and_mount
data_disk_mount_point
── Security ── (collapsible)
ssh_public_key
| stop_protection
  termination_protection
  memory_snapshot_on_stop
── Networking ── (collapsible)
ipv6_address
public_ipv4
| mac_address
  tap_device
── Proxy ── (collapsible)
is_proxy
| region
── Activity ── (collapsible)
last_started
| last_stopped
  has_memory_snapshot
```

### List view

- Columns (left to right): `title`, `server`, `image`, `status`,
  `ipv6_address`.
- Standard filters: `server`, `image`, `status`.

### Buttons

Tiering is keyed off `status` — see [10-desk-ui.md § Virtual Machine](./10-desk-ui.md#virtual-machine):

- **Pending** — no primary; `after_insert` already enqueued provision.
- **Provision** (primary on `Failed`) — manual retry after an
  auto-provision failure. Runs
  [`scripts/provision-vm.py`](../scripts/provision-vm.py).
- **Start** (primary on `Stopped`) — `Stopped` → `Running`.
- **Stop** (primary on `Running`) — `Running` → `Stopped`. Also offered
  (secondary) on `Paused`. Refused while `stop_protection` is set (the
  controller throws; the operator unchecks + saves first). On a VM opted into
  `memory_snapshot_on_stop` it runs the memory-capturing
  `snapshot-stop-vm.py`; the default is the plain `stop-vm.py`. See
  [05 § Memory snapshots](./05-virtual-machine-lifecycle.md#memory-snapshots-fast-stop--start).
- **Stop (memory snapshot)** (under `Actions ▾` on `Running` / `Paused`) —
  the one-off fast stop: calls `stop` with `{memory_snapshot: true}` without
  touching the per-VM flag. The next Start resumes the captured state in
  milliseconds.
- **Resume** (primary on `Paused`) — `Paused` → `Running`.
- **Restart** (secondary on `Stopped` / `Running`) → `Running`. On an
  opted-in VM this is a state-preserving power cycle, not a guest reboot;
  `restart(cold=True)` is the true-reboot path.
- **Pause** (secondary on `Running`) — `Running` → `Paused` via the API
  socket. Runs [`scripts/pause-vm.py`](../scripts/pause-vm.py).
- **Snapshot / Rebuild / Resize** (secondaries on `Stopped`, each opens a
  dialog) — disk and size operations; see
  [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md). They
  appear only while Stopped, which is the deterrent against resizing or
  snapshotting a live VM (the controllers also enforce it). The Snapshot
  dialog's name is optional and pre-filled with `<vm title> — <timestamp>`;
  `snapshot(title)` defaults to the same when `title` is omitted, so a caller
  need not invent a name.
- **Terminate** (under `Actions ▾`, danger; available until
  `Terminated`) — runs
  [`scripts/terminate-vm.py`](../scripts/terminate-vm.py), sets
  `status = Terminated`, detaches the VM's `Reserved IP` (if any) back to the
  Server pool, and deletes the VM's snapshot rows. The UUID does not change.
  Refused while `termination_protection` is set (the controller throws). The
  desk requires the operator to type the VM's `title` into a
  `confirm_destructive` dialog before the red button enables; the dialog body
  is empty — typing the title is the entire deterrent.

---

## Virtual Machine Snapshot

A snapshot of one VM at a point in time. The default `kind=Cold` is a **disk**
snapshot — **both** disks: the root `rootfs.ext4` (LV `atlas-snap-<id>`) and,
when the VM has one, the data disk (LV `atlas-datasnap-<id>`, same snapshot
UUID) — created from a Stopped VM; the snapshot LVs live in the thin pool
(`/dev/atlas/`), independent of the VM directory. `kind=Warm` pairs the disk
LV with the guest's **frozen memory state** captured at one paused instant of
a Running pre-warmed VM (the fan-out golden — clones *resume* it; produced
only by the Image Builder's warm bake, never by the Snapshot button). Restore
and clone recreate the disks; see
[05 § Snapshot/Restore + Warm snapshot fan-out](./05-virtual-machine-lifecycle.md).

### Fields

| Field             | Type                          | Reqd | Read-only | Default | Notes                                                            |
| ----------------- | ----------------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`            | UUID (autoname `hash`)        | Y    | Y         |         | Primary key; names the on-host snapshot directory.               |
| `title`           | Data                          | Y    |           |         | Operator label. `title_field`. `set_only_once`.                  |
| `virtual_machine` | Link → Virtual Machine        | Y    |           |         | `set_only_once`. The VM this snapshot is of.                     |
| `server`          | Link → Server                 |      | Y         |         | Denormalized from the VM so the snapshot is locatable without loading it. |
| `status`          | Select                        | Y    | Y         | Pending | `Pending`, `Available`, `Failed`. Set by the controller after the copy Task. |
| `source_image`    | Link → Virtual Machine Image  |      | Y         |         | The image the VM ran when snapshotted (provenance; the clone's kernel comes from it). |
| `build_mode`      | Select (`site`/`admin`)       |      | Y         |         | For a golden bench snapshot, the bench bake mode the build VM was in — copied from the VM and carried onto a clone, where `deploy-site.py` reads it (`site` → rename the baked site to the FQDN; `admin` → map the FQDN to the admin console). Empty for a non-bench snapshot. `set_only_once`. See [08-images.md](./08-images.md#the-golden-bench-image-self-serve). |
| `disk_gigabytes`  | Int                           |      | Y         |         | Disk size captured, so restore/clone restore the right size.     |
| `data_disk_gigabytes` | Int                       |      | Y         | 0       | Data-disk size captured (`0` if the VM had no data disk).        |
| `data_disk_mount_point` | Data                    |      | Y         |         | The data disk's mount point at snapshot time, carried so a clone reconstructs it faithfully. |
| `data_disk_format_and_mount` | Check              |      | Y         | 0       | Whether the captured data disk was formatted+mounted.            |
| `size_bytes`      | Long Int                      |      | Y         |         | Actual on-host bytes of the copied rootfs (from the Task output). `Long Int` (bigint) — a 32-bit `Int` overflows on a multi-GB rootfs. |
| `rootfs_path`     | Data                          |      | Y         |         | Absolute on-host path to the snapshot rootfs.                    |
| `data_size_bytes` | Long Int                      |      | Y         | 0       | Bytes of the data-disk snapshot (`0` if none). `Long Int`.       |
| `data_rootfs_path`| Data                          |      | Y         |         | On-host device path of the data-disk snapshot LV (`atlas-datasnap-<id>`); empty if the VM had no data disk. |
| `kind`            | Select                        | Y    | Y         | Cold    | `Cold` (disk-only) or `Warm` (disk + frozen memory pair; clones resume). |
| `memory_directory`| Data                          |      | Y         |         | Warm only: the durable on-host `/var/lib/atlas/snapshots/<name>/` holding `vmstate.bin`/`mem.bin`/`host-signature.json`. Host-local, never synced; removed by `on_trash`. |
| `memory_bytes`    | Long Int                      |      | Y         |         | Warm only: on-disk size of the captured memory file.             |
| `vcpus`           | Int                           |      | Y         |         | Warm only: captured machine config — a warm clone must restore at exactly this size (the vmstate encodes it). |
| `memory_megabytes`| Int                           |      | Y         |         | Warm only: captured memory size; same pinning rule as `vcpus`.   |
| `tap_device`      | Data                          |      | Y         |         | Warm only: the golden's in-netns tap name. The vmstate binds the tap by name, so every warm clone's netns recreates it verbatim (netns-scoped; no collision). |
| `host_signature`  | Small Text                    |      | Y         |         | Warm only: CPU model/flags hash/microcode + host kernel + Firecracker version at capture (JSON). `vm-restore.py` cold-boots a clone when the live host differs. |

### Form layout

```
── Overview ──
title
virtual_machine
server
| status
── Disk ──
source_image
disk_gigabytes
data_disk_gigabytes
data_disk_mount_point
data_disk_format_and_mount
| size_bytes
  rootfs_path
  data_size_bytes
  data_rootfs_path
── Warm Snapshot ── (fields shown only when kind=Warm)
kind
memory_directory
memory_bytes
| vcpus
  memory_megabytes
  tap_device
  host_signature
```

### List view

- Columns: `title`, `virtual_machine`, `status`.
- Standard filters: `virtual_machine`, `status`, `server`.

### Controller methods

- `restore_to_vm()` — restore this snapshot onto its own VM (rollback in
  place). Thin wrapper around `Virtual Machine.rebuild("snapshot", self.name)`
  so the Stopped-state guard lives in one place. Returns the Task name.
- `clone_to_new_vm(title, ssh_public_key, vcpus?, memory_megabytes?,
  disk_gigabytes?)` — create a new VM seeded from this snapshot (fresh
  identity). Disk defaults to the snapshot's size and can only grow. On a
  `Warm` snapshot the clone *restores* instead: vcpus/memory/disk are pinned
  to the captured values (mismatched overrides are rejected; the host-side
  cgroup CPU settings — `cpu_max_cores`, `cpu_mode` — are free), and the clone
  carries `warm_snapshot` +
  the golden's `tap_device` so provision stages the memory pair + MMDS
  identity.
- `on_trash` — runs [`delete-snapshot-vm.py`](../scripts/delete-snapshot-vm.py)
  to delete the on-host files, skipped when the VM is already Terminated
  (its directory is gone). A `Warm` row also removes its durable
  `memory_directory` (clone jails hold hard links, so already-provisioned
  clones are unaffected).

### Buttons

Shown only on `Available` snapshots:

- **Clone to new VM** (primary) — dialog for new title + SSH key; creates a
  fresh VM and routes to it.
- **Restore to VM** (secondary) — confirm, then `restore_to_vm()`. The VM
  must be Stopped (the underlying `rebuild` enforces it).
- **Delete** (danger) — `confirm_destructive`; deletes the row, which
  cascades the on-host file delete.

---

## Virtual Machine Image

A kernel + rootfs pair, identified by a name.

### Fields

| Field                    | Type   | Reqd | Read-only | Default | Notes                                                |
| ------------------------ | ------ | ---- | --------- | ------- | ---------------------------------------------------- |
| `image_name`             | Data   | Y    |           |         | Primary key. Unique. `set_only_once`. e.g. `ubuntu-24.04`. A promoted image's name is also its LVM LV name (`atlas-image-<name>`), so it is restricted to lowercase letters/digits/dots/dashes. |
| `title`                  | Data   |      |           |         | Operator-chosen label; `title_field` for the form. `set_only_once`. |
| `is_active`              | Check  |      |           | 1       |                                                      |
| `default_disk_gigabytes` | Int    | Y    |           | 4       | `set_only_once`. Size of the pristine ext4 (per-VM disk grows from this). |
| `build_mode`             | Select (`site`/`admin`) |  |       |         | `set_only_once`. The bench bake mode a promoted bench golden carries (`promote_to_image` copies it from the snapshot). A VM created from this image inherits it (`VirtualMachine.set_build_mode_default`), so its first-boot `deploy_site` maps the FQDN to the baked site (`site`) or the admin console (`admin`). Empty for an ordinary base image (→ `site`). See [08-images.md](./08-images.md#the-golden-bench-image-self-serve). |
| `kernel_url`             | Data   |      |           |         | `set_only_once`. HTTPS URL of the uncompressed `vmlinux`. **Empty for a local image** (promoted from a snapshot — kernel reused from the snapshot's source image). |
| `kernel_filename`        | Data   | Y    |           |         | `set_only_once`. Filename on the server.             |
| `kernel_sha256`          | Data   |      |           |         | `set_only_once`. Hex digest of the kernel. Empty for a local image. |
| `rootfs_url`             | Data   |      |           |         | `set_only_once`. HTTPS URL of the source squashfs. **Empty for a local image** (its rootfs is the promoted `atlas-image-<name>` LV, already on the server). The empty/non-empty rootfs URL is the `is_local` discriminator. |
| `rootfs_filename`        | Data   | Y    |           |         | `set_only_once`. Filename of the resulting ext4 on the server. For a local image this is the base LV name (`atlas-image-<name>`) and the on-disk file is a presence sentinel. |
| `rootfs_sha256`          | Data   |      |           |         | `set_only_once`. Hex digest of the source squashfs. Empty for a local image. |

The URL/SHA fields are **not required** because a `Virtual Machine Image` has two
origins (see [08-images.md](./08-images.md#two-origins-for-a-base-image-a-url-or-a-snapshot-promote)):
**from a URL** (`sync-image.py` downloads + builds, the URL/SHA fields are set) or
**from a snapshot** (`Virtual Machine Snapshot.promote_to_image` `dd`s a baked
snapshot LV into the base-image LV, leaving the URL/SHA fields empty). `validate`
still enforces `https://` on any URL that *is* present.
`VirtualMachineImage.is_local` (no rootfs URL) drives the **non-syncable** rule: a
local image's `after_insert` skips the sync fan-out and `sync_to_server` throws —
its bytes are an LV on one server, with nothing to download.

Every non-`is_active` field is immutable from `after_insert` onward —
the framework `set_only_once` flag paints them read-only on the form,
and the controller's `_validate_immutability` is the
defense-in-depth check.

### Form layout

A single `Overview` Tab Break with the image-data fields under a
collapsible Section Break:

```
image_name
title
| is_active
  default_disk_gigabytes
── Image data ── (collapsible)
kernel_url
kernel_filename
| kernel_sha256
rootfs_url
rootfs_filename
| rootfs_sha256
```

### List view

- Columns (left to right): `name` (the `image_name` autoname),
  `title`, `default_disk_gigabytes`, `is_active`. The legacy
  `image_name` column is dropped from `in_list_view` — the framework
  always renders the autoname as the ID column, so an extra
  `image_name` column was redundant.
- Standard filters: `is_active`.

A first-time operator does not need to invent any of these values. The
Ubuntu 24.04 cloud image constants live in
[`atlas/bootstrap.py`](../atlas/bootstrap.py) as `DEFAULT_IMAGE` (server)
and `MINIMAL_IMAGE` (minimal), mirrored in
[`atlas/tests/e2e/_config.py`](../atlas/tests/e2e/_config.py). Copy one
into the form, or run `atlas.bootstrap.run` which inserts the server row
for you. See [08-images.md](./08-images.md).

### Auto-sync contract

`Virtual Machine Image.after_insert` fans out to every `Server` with
`status = Active`: for each one it calls `self.sync_to_server(server)`,
which enqueues a `sync-image.py` Task. The operator does *not* press
**Sync to Server** for the initial fan-out — saving the image is the
trigger. A **local image** (`is_local`, promoted from a snapshot) is
skipped: its rootfs LV lives only on the server it was promoted on, and
`sync_to_server` throws for it rather than enqueue a download Task that
would fail. Per-attempt tracking happens via the resulting Task rows
(filter the Task list by `script = sync-image.py`); a dedicated
`Virtual Machine Image Sync` tracking DocType was scoped in the plan
but deferred for the PoC.

The `sync_to_server` and `sync_to_all_servers` whitelisted methods
survive for use by `bootstrap.py` and the e2e harness, but no
operator-facing buttons surface them — the form is effectively
read-only after creation, and the field lock is enforced by both
`set_only_once` and `_validate_immutability`.

### Buttons

- **Archive** (under `Actions ▾`, shown only while `is_active = 1`).
  Calls `archive()` to flip `is_active = 0`. Idempotent.

No primary action, no Sync Status panel, no Sync-to-Server picker on
the form. Initial sync is automatic on save; ad-hoc per-server sync
goes through **Sync Image** on the target Server's `Actions ▾` menu.

---

## Reserved IP

A public IPv4 address that belongs to a `Server` and may be attached to one of
that Server's VMs. The address is the unit of allocation: it is **allocated to
the Server** (the vendor binds a reserved IP to the droplet — see
[06-networking.md](./06-networking.md)) and exists whether or not a VM is using
it. Attaching it to a VM is a separate, reversible step.

This is what makes a VM reachable on IPv4. Atlas VMs are otherwise
inbound-IPv6-only (`spec/06`: one public `/128`, IPv4 egress-only via host
NAT44). A Reserved IP, attached to a VM and 1:1-NATed by the host to the guest's
private `/30`, gives that one VM inbound IPv4. Today this is used by the reverse
proxy (an operator-owned VM); the same mechanism generalizes to tenant VMs
later.

A Server owns a **pool** of Reserved IPs: the set of `Reserved IP` rows whose
`server` points at it. There is no child-grid embedding — like
`Virtual Machine Snapshot`, a Reserved IP is a standalone DocType linked to its
Server (and surfaced in the Server's Connections dashboard), so it has its own
allocate/attach/detach/release lifecycle and is independently queryable
("which VM has this IP?", "is there a free IP on this Server?").

### Fields

| Field                  | Type                  | Reqd | Read-only | Default   | Notes                                                            |
| ---------------------- | --------------------- | ---- | --------- | --------- | ---------------------------------------------------------------- |
| `name`                 | UUID (autoname `hash`) | Y   | Y         |           | Primary key. |
| `ip_address`           | Data                  | Y    | Y         |           | The public IPv4. `unique`. `title_field`. Locked once written. |
| `server`               | Link → Server         |      | Y         |           | The host this IP is currently bound to. The IP belongs to the Server even with no VM attached. **Not immutable** — the vendor can reassign the IP to another droplet, so `reassign()` repoints this field (migration uses it — [19](./19-vm-migration.md)). Empty when the IP rests allocated-on-the-vendor with no Server (a valid resting/handoff state). |
| `status`               | Select                | Y    | Y         | Allocated | `Allocated` (on the Server, no VM) or `Attached` (bound to a VM). Derived in `validate()` from `virtual_machine` — never set by hand. |
| `virtual_machine`      | Link → Virtual Machine |     | Y         |           | The attached VM, or empty when unattached. Only a VM on the **same Server** may be attached. Maintained by `attach()` / `detach()`. |
| `provider_resource_id` | Data                  |      | Y         |           | Vendor's handle for the reserved IP (DigitalOcean reserved-IP id). Empty for Self-Managed. Locked once written. |

Immutability follows the `Server` idiom: `ip_address` and `provider_resource_id`
lock once they carry a value (`None → value` allowed for initial population) — a
Reserved IP is bound to its address and vendor handle for life. `server` is
**deliberately not** in that set: the vendor can move the IP to a different
droplet, so the row's `server` is a mutable pointer that follows it
(`reassign()`), and an IP may rest with **no Server** (allocated-on-the-vendor).
`status` is always derived from `virtual_machine`, so it is never an independent
input.

### Controller methods

The pool's vendor side (reserve / discover / release) goes through the provider
abstraction (`allocate_reserved_ip` / `list_reserved_ips` / `release_reserved_ip`,
see [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)); the
attach/detach pair drives the vendor bind, the host 1:1-NAT Task, and the Frappe
invariant together (in failure-safe order).

- `allocate(server)` *(module function)* — reserve a fresh public IPv4 at the
  vendor (in its single region, unassigned) and write an `Allocated` `Reserved
  IP` row for `server`. Returns the new row name. Binding to the droplet + the
  host 1:1-NAT happen on `attach()`, not here.
- `discover(server)` *(module function)* — list the vendor's reserved IPs and
  import any bound to `server`'s droplet that Atlas doesn't yet model, creating
  an `Allocated` row per new one (existing ones skipped). A vendor → Frappe
  reconcile, mapped by droplet id; returns the names created. Throws if the
  Server has no `provider_resource_id`.
- `attach(virtual_machine)` — bind this `Allocated` IP to a VM on the same
  Server. Throws if the IP is already attached, if the VM is on a different
  Server, or if the VM already has a `public_ipv4`. In failure-safe order: (1)
  `assign_reserved_ip` binds the IP to the Server's droplet at the vendor
  (idempotent; Self-Managed no-op); (2) `vm-reserved-ip.py` runs the host 1:1-NAT
  and writes `RESERVED_IPV4` into the VM's `network.env`; (3) only then sets
  `virtual_machine` (→ `status = Attached`) and denormalizes `ip_address` onto
  the VM's `public_ipv4`. The vendor bind and the Task both raise on failure, so
  a half-applied attach never leaves a row claiming an attachment the host lacks.
  Owns the invariant — **one IP, one VM, same Server**. See
  [06-networking.md](./06-networking.md#what-the-host-does).
- `detach()` — release the IP from its VM back to the Server pool (→ `status =
  Allocated`) and clear the VM's `public_ipv4`. Tears down the host NAT via
  `vm-reserved-ip.py` and unbinds the IP at the vendor first, then clears the
  invariant. **Skips the host Task for a Terminated VM** (terminate already
  removed the host networking and `rm -rf`'d the env). Guards a missing VM row.
  Called automatically by `Virtual Machine.terminate()` so a terminated VM
  returns its address to the pool.
- `reassign(target_server)` — move this **detached** IP from its current Server
  to `target_server` at the vendor and repoint `server`. The address and vendor
  handle never change; only which droplet the IP is bound to (and so which
  Server's pool it sits in) does. Same-provider only. Idempotent (a no-op if
  already there). Self-Managed has no vendor bind, so it only repoints the row
  (the operator re-routes the address). This is the path that lets a customer's
  inbound v4 **survive a VM migration** ([19](./19-vm-migration.md)): detach,
  `reassign` to the target droplet, repoint, re-attach to the migrated VM.
- `release()` — destroy the vendor reserved IP and delete this row, returning
  the address to the vendor pool. Refuses while the IP is attached. **Explicit,
  like `Server.archive()`** — destroying the vendor resource is never a side
  effect of deleting the Frappe row (`on_trash` only blocks deleting an attached
  IP; it does not touch the vendor).

### Form layout

```
── Overview ──
ip_address
server
| status
  virtual_machine
── Provider ──
provider_resource_id
```

### List view

- Columns: `ip_address`, `server`, `status`, `virtual_machine`.
- Standard filters: `server`, `status`, `virtual_machine`.

### Buttons

On the **Reserved IP** form (status-gated):

- **Attach** (primary, shown while `status = Allocated`) — opens a one-field
  dialog (Link → Virtual Machine, filtered to the IP's Server and to VMs without
  a `public_ipv4`) and calls `attach(virtual_machine)`.
- **Release** (under `Actions ▾`, danger, shown while `status = Allocated`) —
  type-to-confirm; calls `release()` to destroy the vendor IP and delete the row.
- **Detach** (under `Actions ▾`, danger, shown while `status = Attached`) —
  calls `detach()`.

On the **Server** form (the pool entry points, `Actions ▾`, while `Active`):

- **Allocate Reserved IP** — cost-confirm; calls `allocate(server)` to reserve a
  new vendor IP and routes to the new row.
- **Discover Reserved IPs** — calls `discover(server)` to import vendor reserved
  IPs bound to this droplet; reports the count. See
  [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip). The Server's
  Connections panel surfaces the pool under **Networking → Reserved IP**.

---

## Subdomain

One routing entry for the reverse proxy: a `<subdomain>.<region>.frappe.dev`
name that points at exactly one site VM. The set of **active** Subdomain rows is
the **desired map** every proxy VM serves — the proxy control plane
(`atlas/atlas/proxy.py`) reconciles each proxy guest's live `lua_shared_dict` to
it over SSH. See [12-proxy.md](./12-proxy.md) for the proxy and the reconcile
loop.

Standalone and linked (the `Reserved IP` / `Virtual Machine Snapshot` idiom), not
a child grid on a proxy doctype: every proxy VM holds the **whole** map, so a
child-of-proxy model would fight that — the map is owned by the fleet, not per
proxy. The row is independently queryable ("which VM does `acme` point at?").
Atlas is single-region (`Atlas Settings.region`), so the row carries no
denormalized `region`: every active subdomain belongs to the one region by
definition.

### Fields

| Field             | Type                   | Reqd | Read-only | Default | Notes                                                            |
| ----------------- | ---------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`            | = `subdomain` (autoname `field:subdomain`) | Y | Y |    | Primary key is the subdomain label itself. |
| `subdomain`       | Data                   | Y    |           |         | The bare label, `unique` fleet-wide. Reachable at `<subdomain>.<region>.frappe.dev`. The proxy's routing key. `title_field`. Immutable after insert. |
| `active`          | Check                  |      |           | 1       | Inactive rows are excluded from the served map (kept for history). Toggle off to take a site off the front door without deleting the row. |
| `virtual_machine` | Link → Virtual Machine | Y    |           |         | The site VM this subdomain points at. The proxy dials its public IPv6 `:80` (plaintext) over the v6 internet. Immutable after insert. |
| `address`         | Data                   | Y    | Y         |         | The target VM's public IPv6 `/128`, denormalized so the desired-map query is join-free. Kept in sync with the VM's `ipv6_address` on save. The literal the proxy dials. |

Immutability: `subdomain` and `virtual_machine` lock after insert — repointing a
live subdomain at a different VM is a delete-and-recreate, so the proxy map change
is explicit, never a silent in-place edit. The one mutable field is `active`
(toggles the mapping in/out of the served map). `address` is always derived from
the linked VM, never hand-edited.

### Controller methods

- `validate()` — denormalizes `address` from the target VM's `ipv6_address`
  (throws if the VM has none — an unaddressable target can't be a route) and
  enforces the immutability above.
- `subdomain_map()` *(module function)* — returns `{subdomain: address}` for every
  **active** subdomain. This is the full map every proxy VM serves; the reconcile
  loop serializes it canonically (`json.dumps(sort_keys=True, indent=2)` + newline,
  byte-identical to the guest's `persist.lua`) and byte-compares it against each
  proxy guest's live `/map`. See [12-proxy.md](./12-proxy.md).

### Form layout

```
── Overview ──
subdomain
| active
── Target ──
virtual_machine
| address
```

### List view

- Columns: `subdomain`, `active`, `virtual_machine`, `address`.
- Standard filters: `active`, `virtual_machine`.

---

## Port Mapping

One forwarding entry for the **TCP proxy** ([17-tcp-proxy.md](./17-tcp-proxy.md)):
a proxy-side port that forwards raw TCP to a tenant VM's service port (SSH `:22`,
MariaDB `:3306`, anything L4). The set of **active** Port Mapping rows is the
**desired port map** every proxy VM serves — the TCP control plane
(`atlas/atlas/tcp_proxy.py`) reconciles each proxy guest's stream-side
`lua_shared_dict` to it over SSH, exactly as `Subdomain` / `atlas/atlas/proxy.py`
does for HTTP.

This is the L4 sibling of [Subdomain](#subdomain) and follows it field-for-field:
standalone and linked (the `Reserved IP` / `Subdomain` idiom), **not** a child
grid on a proxy — every proxy VM holds the **whole** map, so the map is owned by
the fleet. Atlas is single-region (`Atlas Settings.region`), so the row carries no
denormalized `region`. The one field with no `Subdomain` analogue is `public_port`:
the routing key is a *port number Atlas allocates*, not a label the user picks,
because raw TCP carries no application-layer routing key (the local port is the
only thing the proxy can route on — see [17-tcp-proxy.md](./17-tcp-proxy.md)).

### Fields

| Field             | Type                   | Reqd | Read-only | Default | Notes                                                            |
| ----------------- | ---------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`            | = `<protocol>-<public_port>` (autoname `format:{protocol}-{public_port}`) | Y | Y |    | `public_port` is unique fleet-wide; the protocol prefix keeps the name readable. |
| `public_port`     | Int                    | Y    | Y         |         | The proxy-side port the tenant connects to. **Allocated by Atlas** on insert (lowest free in the pool); `unique` fleet-wide; read-only. The routing key. |
| `active`          | Check                  |      |           | 1       | Inactive rows are excluded from the served map (kept for history, and the row still owns its port so toggling back on never collides). |
| `virtual_machine` | Link → Virtual Machine | Y    |           |         | The tenant VM this port forwards to. The proxy dials its public IPv6 `:target_port` over the v6 internet. Immutable after insert. |
| `address`         | Data                   | Y    | Y         |         | The target VM's public IPv6 `/128`, denormalized so the desired-map query is join-free. Kept in sync with the VM's `ipv6_address` on save. |
| `target_port`     | Int                    | Y    |           |         | The service port **inside the guest** (22 SSH, 3306 MariaDB). Immutable after insert. |
| `protocol`        | Select (`tcp`/`ssh`/`mariadb`) |  |       | `tcp`   | A label only — the forwarder is protocol-agnostic L4. For the operator and the future dashboard. |

The desired map is `port_map()` =
`{ "<public_port>": "[<address>]:<target_port>" }` for every **active** mapping.
The value is a ready-to-dial bracketed-v6 `host:port` literal so the guest does no
formatting.

Immutability: `virtual_machine` and `target_port` lock after insert (`public_port`
is read-only and allocated). The one mutable field is `active`. `address` is always
derived from the linked VM, never hand-edited.

### Controller methods

- `validate()` — denormalizes `address` from the target VM's `ipv6_address`
  (throws if the VM has none — an unaddressable target can't be a route) and
  enforces the immutability above.
- `before_insert()` — allocates `public_port`: the lowest port in
  `Atlas Settings.tcp_port_pool` (default `10000-19999`) not already held by an
  active *or inactive* mapping, under a row-lock. Pool exhaustion is a typed throw,
  never a silent wrap.
- `port_map()` *(module function)* — returns
  `{public_port: "[address]:target_port"}` for every **active** mapping. This is
  the full map every proxy VM serves; the reconcile loop serializes it canonically
  (the same `json.dumps(sort_keys=True, indent=2)` + newline as `Subdomain`,
  byte-identical to the guest's `stream-persist.lua`) and byte-compares it against
  each proxy guest's live map. See [17-tcp-proxy.md](./17-tcp-proxy.md).

### Form layout

```
── Overview ──
public_port
| active
protocol
── Target ──
virtual_machine
target_port
| address
```

### List view

- Columns: `public_port`, `active`, `virtual_machine`, `target_port`, `protocol`.
- Standard filters: `active`, `virtual_machine`, `protocol`.

---

## SSH Key

A public SSH key a dashboard user registers once and chooses when creating a
Virtual Machine. Per-user owned (Frappe's built-in `owner`), like
`Virtual Machine` — a user sees and manages only their own keys, enforced at
the permission layer (see [11-user-ui.md](./11-user-ui.md)). It is pure data:
no Tasks, no lifecycle methods.

The VM's own `ssh_public_key` (immutable, injected into the rootfs) stays the
source of truth for provisioning. The dashboard copies the chosen key's
`public_key` body into the VM on create — so this DocType adds nothing to the
provisioning path; it is a user-facing convenience over the existing field.

### Fields

| Field         | Type      | Reqd | Read-only | Notes                                                            |
| ------------- | --------- | ---- | --------- | ---------------------------------------------------------------- |
| `name`        | (autoname `hash`) | Y | Y     |           | Primary key. 10-char random hex.                                 |
| `key_name`    | Data      | Y    |           | `title_field`. User-chosen label (e.g. `laptop`).                |
| `public_key`  | Long Text | Y    |           | `set_only_once`. OpenSSH public-key body. `validate()` strips it and rejects anything whose first token isn't a known key type (`ssh-ed25519`, `ssh-rsa`, `ecdsa-*`, `sk-*`). |
| `fingerprint` | Data      |      | Y         | Derived in `validate()` from `public_key` — the standard `SHA256:<base64nopad>` form `ssh-keygen -lf` prints. A recognizable key identity without echoing the whole blob. |

### Form layout

```
── Overview ──
key_name
fingerprint
── Key ──
public_key
```

### List view

- Columns: `key_name`, `fingerprint`.
- No standard filters (a user's key list is short).

### Permissions

System Manager only (operator/Central-facing). No end-user role or row-level
scoping.

### Buttons

None. The form is data-entry only; the key body is copied into a VM's immutable
`ssh_public_key` at provision.

---

## Tenant

The unit of ownership/grouping for Atlas resources. A tenant is created and
managed by **Central** — the external system that owns end-users and talks to
Atlas as the operator. The tenant's `name` **is** the Central `Team.name`:
Central passes that id as `team` on every provisioning call, and the Tenant is
named by it. There is no translation table — the primary key carries the mapping,
so the `tenant` link stamped on a resource already *is* the Central team. Central
also sets
the immutable `email` once at creation, then stamps the optional, set-only-once
`tenant` link on the resources it provisions (`Virtual Machine`, `Virtual
Machine Image`, `Virtual Machine Snapshot`).

This is operator/Central-facing only (System Manager permission). It is pure data
plus list helpers — no Tasks, no lifecycle.

> **Tenancy is the attribution model.** Atlas no longer has end-user `owner`
> scoping or an `Atlas User` role (that boundary was removed when self-serve
> signup moved to Central, [11-user-ui.md](./11-user-ui.md)). The `tenant` link on
> a `Virtual Machine` / `Site` (and `Virtual Machine Image` / `Snapshot`) is how a
> resource is tied back to a Central team — set once at provision by the Central
> API (`create_vm` / `create_site`).

### Fields

| Field    | Type                  | Reqd | Read-only | Notes                                                                              |
| -------- | --------------------- | ---- | --------- | ---------------------------------------------------------------------------------- |
| `name`   | (autoname, set by user) | Y    | Y       | Primary key — **is** the Central `Team.name`. `autoname()` sets it from the `team` create kwarg (not a stored field); throws if absent. Reusing a team get-or-creates the same row; a fresh collision is a DB duplicate-key error. |
| `title`  | Data                  |      |           | `title_field`. Human label for lists. `before_insert` defaults it to `email` (or the `name` / team) when Central omits it, so Desk lists show a readable name. Still editable. |
| `email`  | Data (`Email`)        | Y    |           | `set_only_once`, `unique`. Central sets once. Lowercased in `validate()`.          |

The Central `Team.name` is supplied as the `team` kwarg on create (`ensure_tenant`
/ the provisioning APIs) and becomes the row's `name` — it is not persisted as a
separate column. Immutability of `email` is enforced both by the JSON
`set_only_once` and by a controller `IMMUTABLE_AFTER_INSERT` guard — the same
belt-and-suspenders pattern as `Virtual Machine` and `Virtual Machine Image`.
`name` is immutable by virtue of being the primary key. Uniqueness of `email` is
a DB unique index from the JSON `unique` flag; the team is unique because it is
the primary key.

### The `tenant` link on resources

`Virtual Machine`, `Virtual Machine Image`, and `Virtual Machine Snapshot` each
gain an optional, `set_only_once` `tenant` Link (empty for operator-created
resources; Central stamps it once). `tenant` is added to each controller's
`IMMUTABLE_AFTER_INSERT` tuple so a re-stamp is rejected (the Snapshot relies on
the framework's `set_only_once` alone — it has no immutability tuple).

### Controller methods

- `autoname()` — sets `name` from the `team` create kwarg, so the tenant's
  primary key *is* its Central `Team.name`; throws if absent.
- `before_insert()` — defaults `title` to `email` (or the `name` / team) when
  Central omits it, so a tenant never shows up in Desk lists as a bare team id.
- `virtual_machines()` / `images()` / `snapshots()` (whitelisted) — the rows of
  each resource type stamped with this tenant, newest first.
- `resources()` (whitelisted) — all three in one round-trip as
  `{"virtual_machines": [...], "images": [...], "snapshots": [...]}`; reuses the
  individual helpers so there is one source of truth for fields/filters.

### Form layout

```
── Overview ──
title
email
```

The `name` (the Central reference) shows as the document id; there is no separate
field for it.

### List view

- Columns: `title`, `email`.

### Permissions

System Manager only (all perms). Tenant is reached by Central/operator only.

### Buttons

None. Central drives creation and the `tenant` stamping through the standard
endpoints / whitelisted methods.

---

## Task

One row per shell script execution against a server. Append-only: every field
is read-only on the form. The system writes the row at insert and again when
the run finishes.

### Fields

| Field                   | Type                   | Reqd | Read-only | Default | Notes                                       |
| ----------------------- | ---------------------- | ---- | --------- | ------- | ------------------------------------------- |
| `name`                  | (autoname `hash`)      | Y    | Y         |         | 10-char random hex (Frappe `autoname = "hash"`). |
| `subject`               | Data                   |      | Y         |         | Set in `before_insert` from `SCRIPT_LABELS[script]` (see [04-tasks.md § Task subject](./04-tasks.md#task-subject)). Verb-only when operating on an existing object (`Reboot`, `Start`, `Sync`), verb-noun when creating one (`Bootstrap Server`, `Create Virtual Machine`, `Sync Image`). `title_field` so the form breadcrumb reads it instead of the hash. Indexed. |
| `server`                | Link → Server          |      | Y         |         | Indexed.                                    |
| `virtual_machine`       | Link → Virtual Machine |      | Y         |         | Set when the task is for one VM. Indexed.   |
| `script`                | Data                   | Y    | Y         |         | Path under `atlas/scripts/`, e.g. `provision-vm.py`. Indexed. |
| `triggered_by`          | Link → User            | Y    | Y         |         | `Administrator` for scheduled jobs.         |
| `status`                | Select                 | Y    | Y         | Pending | `Pending`, `Running`, `Success`, `Failure`. Indexed. |
| `exit_code`             | Int                    |      | Y         |         |                                             |
| `duration_milliseconds` | Int                    |      | Y         |         | Indexed. For sortable list views.           |
| `started`               | Datetime               |      | Y         |         |                                             |
| `ended`                 | Datetime               |      | Y         |         |                                             |
| `variables`             | Long Text (JSON)       | Y    | Y         |         | The env-var dictionary passed to the script.|
| `stdout`                | Code                   |      | Y         |         |                                             |
| `stderr`                | Code                   |      | Y         |         |                                             |

Every operator-visible field is read-only on the form; the table column is
the contract for what the row holds, not for what an operator can type.

`variables` stores the inputs so a task can be replayed by reading the row.
Secrets are not put in `variables`. If a task needs a secret, the secret is
read from another DocType at execution time and not echoed into the Task
record.

### Form layout

A single `Overview` Tab Break with the Output section folded
underneath as a collapsible Section Break (the old `Output` tab is
gone):

```
status
| exit_code
  duration_milliseconds
subject
server
virtual_machine
script
triggered_by
── Timing ──
started
| ended
── Inputs ──
variables
── Output ── (collapsible)
stdout
stderr
```

The client script overlays this with a status-coloured dashboard
headline and a Retry button on Failure. The header `chips` (Server /
VM / Triggered by) and the **Sibling Tasks** quick_list are gone —
both surfaced data already in the form body or the Connections
dashboard. See [10-desk-ui.md § Task](./10-desk-ui.md#task) for the
full behavior.

The controller publishes a `task_update` realtime event (scoped to
the Task's document room) from `after_insert` and `on_update`, with
`{name, status, exit_code, duration_milliseconds, server, virtual_machine, subject}`.
The Task form subscribes and reloads on each tick — long-running
Tasks aren't a black box.

### List view

- Columns (left to right): `subject`, `server`, `virtual_machine`,
  `script`, `status`, `duration_milliseconds`, `started`.
  (Frappe orders list columns by their position in the field schema.
  `started` lives in the Timing section, after the header, so it lands at
  the end of the row. Putting it first would require moving the field
  ahead of the header, which would break the form layout. Operators can
  still sort the list by `started`.)
- Standard filters: `server`, `virtual_machine`, `script`, `status`.

---

## Route53 Settings

A Single. AWS Route 53 credentials, the twin of
[DigitalOcean Settings](#digitalocean-settings). Read by `Route53DnsProvider`;
the secret comes out via `atlas.atlas.secrets.get_secret`. The *active* DNS vendor
is not stored here — it lives on `Atlas Settings.dns_provider_type` (the DNS
registry keys off it, `dns.for_dns_provider_type`), since it is an
Atlas-instance switch, not a Route 53 credential. There is no separate
`Domain Provider` DocType.

### Fields

| Field                  | Type     | Reqd | Notes                                                          |
| ---------------------- | -------- | ---- | -------------------------------------------------------------- |
| `access_key_id`        | Data     | Y    | AWS IAM access key id with `route53:*` on the zone. `set_only_once`. |
| `secret_access_key`    | Password | Y    | AWS IAM secret. Rotate by clearing via `db.set_value`, then re-saving. |
| `region`               | Data     |      | AWS API region for signing (default `us-east-1`; Route 53 is global). |

No zone-id field: `certbot-dns-route53` discovers the hosted zone from the domain
name at issue time.

### Form layout

```
── AWS credentials ──
access_key_id
secret_access_key
region
```

### Buttons

- **Test Connection** — `dns.for_dns_provider_type(Atlas Settings.dns_provider_type).authenticate()`
  (Route 53 lists hosted zones). Result surfaces via a toast; no auto-painted
  indicator.

---

## Lets Encrypt Settings

A Single. ACME account config read by `LetsEncryptProvider`. The active TLS
issuer is not stored here — it lives on `Atlas Settings.tls_provider_type` (the
TLS registry keys off it, `tls.for_tls_provider_type`), since there is no single
"TLS Settings" and no `TLS Provider` DocType. The DocType name drops the
apostrophe in "Let's Encrypt" because Frappe scrubs a DocType name into a Python
module path and `Let's Encrypt Settings` would scrub to the unimportable
`let's_encrypt_settings`; the `Atlas Settings.tls_provider_type` Select value
keeps the apostrophe (`Let's Encrypt`) since that is data, not a module.

### Fields

| Field               | Type | Reqd | Default | Notes                                                  |
| ------------------- | ---- | ---- | ------- | ------------------------------------------------------ |
| `acme_directory_url`| Data | Y    | LE production directory | Use the staging URL while testing. |
| `account_email`     | Data | Y    |         | ACME registration / expiry-notice email. `set_only_once`. |

There is no agree-to-ToS field: certbot is always invoked with `--agree-tos`
([scripts/lib/atlas/certs.py](../scripts/lib/atlas/certs.py)), so registering the
ACME account agrees to the terms — a separate checkbox could only ever hold one
valid value.

### Form layout

```
acme_directory_url
account_email
```

### Buttons

- **Test Connection** — `tls.for_tls_provider_type(Atlas Settings.tls_provider_type).authenticate()`.
  Result surfaces via a toast; no auto-painted indicator.

---

## Root Domain

One wildcard zone == one region. A row `blr1.frappe.dev` owns the regional
wildcard cert `*.blr1.frappe.dev` that fronts the proxy fleet. `region` is frozen
on the row at insert (denormalized from `Atlas Settings.region`) so a later
Settings change can't re-point an already-issued domain. See
[13-tls.md](./13-tls.md).

### Fields

| Field             | Type                  | Reqd | Notes                                                              |
| ----------------- | --------------------- | ---- | ------------------------------------------------------------------ |
| `name`            | = `domain` (autoname `field:domain`) | Y | Primary key is the domain itself. |
| `domain`          | Data                  | Y    | The wildcard zone, e.g. `blr1.frappe.dev`. `unique`, `set_only_once`. The cert is `*.<domain>`. |
| `region`          | Data                  |      | The region this domain's wildcard fronts. Read-only; denormalized from `Atlas Settings.region` (the single source of truth) at insert — the operator does not type it. Frozen so a later Settings change can't re-point an issued domain. `set_only_once`. |
| `is_active`       | Check                 |      | Default 1. |
| `dns_provider_type`    | Select           |      | The DNS vendor that owns the zone (DNS-01). Read-only; denormalized from `Atlas Settings.dns_provider_type` at insert. |
| `tls_provider_type`    | Select           |      | The issuer that produces the cert. Read-only; denormalized from `Atlas Settings.tls_provider_type` at insert. |

`domain` and `region` lock after insert. `common_name` (`*.<domain>`) is a derived
property, not a stored field. The two vendor-type fields are denormalized once at
insert (read-only thereafter), so a later change to the active vendors does not
silently re-point an already-issued domain.

### Controller methods

- `issue_certificate()` — **Issue / Renew Certificate** button. Finds or creates
  the domain's single `TLS Certificate` (one cert per domain) and delegates to its
  `issue()`.

### Form layout

```
domain
region
| is_active
── Providers ──
dns_provider_type
tls_provider_type
```

### List view

- Columns: `domain`, `region`, `is_active`.
- Standard filters: `region`, `is_active`.

---

## TLS Certificate

The issued regional wildcard cert, and the wiring that lands it on every proxy VM
in the domain's region — the producer the proxy's `push_cert` was missing. One per
`Root Domain`. See [13-tls.md](./13-tls.md).

### Fields

| Field            | Type                | Reqd | Read-only | Notes                                                       |
| ---------------- | ------------------- | ---- | --------- | ----------------------------------------------------------- |
| `name`           | UUID (`hash`)       | Y    | Y         | Primary key. |
| `root_domain`    | Link → Root Domain  | Y    |           | `set_only_once`. |
| `common_name`    | Data                |      | Y         | `*.<domain>`, derived from the Root Domain. `title_field`. |
| `status`         | Select              |      | Y         | `Pending` / `Active` / `Expiring` / `Failed`. Set by issue/renew + the scheduler. |
| `tls_provider_type` | Select           |      | Y         | The issuer vendor used. Denormalized from the domain's `tls_provider_type` (itself denormalized from `Atlas Settings`). |
| `issued_on`      | Datetime            |      | Y         | Parsed from the issued cert. |
| `expires_on`     | Datetime            |      | Y         | Parsed from the issued cert; drives `renew_expiring`. |
| `fullchain_path` | Data                |      | Y         | Path to `fullchain.pem` on the controller. Bytes stay out of the DB. |
| `privkey_path`   | Data                |      | Y         | Path to `privkey.pem` on the controller (`0600`, Frappe-user owned). |

### Controller methods

- `issue()` / `renew()` — run the domain's `TlsProvider.issue` (the controller-local
  `issue-cert.py` Task; see [13-tls.md](./13-tls.md)), record paths + dates, set
  `Active`, then `_push_to_proxies()`. On failure, flip `Failed` and re-raise.
- `push_to_proxies()` — **Push to Proxies** button. Read the PEMs off disk and call
  `atlas.atlas.proxy.push_cert(vm, fullchain, privkey)` for every `is_proxy` VM in
  the domain's region. One unreachable proxy is logged and skipped.
- `renew_expiring()` *(module function)* — the `daily` scheduler entry point. Renew
  every `Active` cert whose `expires_on` is within 30 days (re-issue **and**
  re-push).

Buttons: **Issue/Renew** (primary), **Push to Proxies**.

### Form layout

```
root_domain
common_name
status
| tls_provider_type
issued_on
expires_on
── On-disk PEM paths (controller) ──
fullchain_path
privkey_path
```

### List view

- Columns: `root_domain`, `common_name`, `status`.
- Standard filters: `status`, `expires_on`.

## Site

The user-facing self-serve resource: "my Frappe site at `acme.blr1.frappe.dev`".
A `Site` is the user-owned aggregate that ties the one routing identity
(Contract A) to the backing VM it clones from the golden bench snapshot and the
readiness state (Contract B). It is **not** the [Subdomain](#subdomain) (the
proxy map it creates once serving) and **not** the
[Virtual Machine](#virtual-machine) (which it owns/creates). Full lifecycle in
[14-self-serve.md](./14-self-serve.md).

### Fields

| Field           | Type                   | Reqd | Read-only | Notes                                                       |
| --------------- | ---------------------- | ---- | --------- | ----------------------------------------------------------- |
| `name`          | the FQDN               | Y    | Y         | Primary key, built in `autoname()` as `<subdomain>.<region domain>` — the one routing string (Contract A): proxy Host header == this key. The `<region domain>` suffix is read from the active `Root Domain`; the Site stores no `region` of its own (Atlas is single-region). The routing identity, never written on disk (the baked site stays `site.local`). Never transformed. |
| `subdomain`     | Data                   | Y    |           | The bare DNS label the user chose (`acme`). `set_only_once`. A single label, no dots, lowercase `[a-z0-9-]`, ≤63 chars, no leading/trailing hyphen; not in the reserved denylist. |
| `tenant`        | Link → Tenant          |      | Y         | `set_only_once`. The Central team this site belongs to, stamped by `create_site` (the attribution key; [16-central.md](./16-central.md)). Operator/e2e sites created directly may leave it empty. |
| `status`        | Select                 |      | Y         | `Pending` → `Provisioning` → `Deploying` → `Running` / `Failed` / `Terminated`. Controller-written. `Running` is reached **only** on an observed HTTP 200 from the guest `:80` (Contract B), not when the backing VM boots. |
| `virtual_machine` | Link → Virtual Machine |    | Y         | `set_only_once`. The backing VM, cloned from the golden bench snapshot by the background job (the user never picks it). |
| `subdomain_doc` | Link → Subdomain       |      | Y         | The proxy-map row the site created once it began serving. Deleting it (or the Site) takes the site off the front door. |
| `admin_password` | Password              |      | Y         | The Frappe Administrator password handed to the tenant — the **shared baked throwaway** (`Site.BAKED_ADMIN_PASSWORD`, in lockstep with build.sh; the deploy no longer resets it per VM, and the tenant rotates it after first login). The db root password is baked + shared too. Stored encrypted; surfaced to Central (the `site.status_changed` Running event + `get_site` poll) so the tenant can sign in. Controller-written. |

The `tenant` link is the attribution key (Central, [16-central.md](./16-central.md));
Atlas stamps no end-user `owner` scoping.

### Validation

- **Single label, no dots** + DNS-label rules — a dot would escape the one
  regional wildcard the proxy terminates (would need its own cert; deferred).
- **Reserved denylist** — `www admin api proxy app dashboard mail ns root`
  (module-level `RESERVED_SUBDOMAINS`). Anything else already taken is caught by
  the FQDN-key uniqueness check, which throws a clean *"subdomain taken"* (the
  create_site race, [14-self-serve.md](./14-self-serve.md)).
- **Immutability** — `subdomain`, `virtual_machine` are frozen after insert
  (`IMMUTABLE_AFTER_INSERT`, guarded in `validate()`).

### Controller methods & lifecycle

- `before_insert()` — validate the label, set `status = Pending`. (The owning
  `tenant` is set by `create_site`; Atlas stamps no end-user `owner`.)
- `autoname()` — build the FQDN key from `subdomain` + the region domain.
- `after_insert()` — enqueue `auto_provision` (`queue="long"`, it SSHes).
- `auto_provision(site_name)` *(module function)* — the background entrypoint:
  clone the backing VM from `Atlas Settings.default_bench_snapshot` →
  `wait_for_ssh` → run `deploy-site.py` in the guest ([14-self-serve.md](./14-self-serve.md)) → `wait_for_http`
  for the 200 → create the `Subdomain` row → `status = Running`. Any
  failure flips `Failed` and re-raises (fail loud). No-op past `Pending`.
- `terminate()` — delete the `Subdomain` (proxy stops routing), terminate the
  backing VM, set `Terminated`. Mirrors `VirtualMachine.terminate()`'s
  cleanup-then-mark shape.

### Permissions

`System Manager` only (operator/Central-facing; Central calls `create_site` with
the operator token). No end-user role or row-level scoping.

### List view

- Columns: `subdomain`, `status`.
- Standard filters: `status`.

## Image Build

One row per bake run of the [Image Builder](./15-image-builder.md): provision a
scratch VM, run a recipe's `build.sh` in it over guest-SSH, snapshot the result,
optionally register the snapshot. The `recipe` (from the code-defined
[`image_recipes.RECIPES`](../atlas/atlas/image_recipes.py)) decides *what* is
baked; this row is the operator-facing record of *a* bake — its status, its
artifacts, its audit. The produced [Virtual Machine Snapshot](#virtual-machine-snapshot)
is the durable output; the build VM is scratch.

### Fields

| Field | Type | Reqd | Read-only | Notes |
| ----- | ---- | ---- | --------- | ----- |
| `name` | series | Y | Y | `IMG-BUILD-#####` (`autoname: Expression`). A recipe is re-baked many times, so the name isn't the recipe. |
| `recipe` | Select | Y |  | `bench-v16` / `bench-v15` / `bench-nightly` / `proxy` — the [recipe registry](../atlas/atlas/image_recipes.py) key (kept in lockstep with `recipe_names()`). The back-compat `bench` alias (→ `bench-v16`) is not an option. `set_only_once`. |
| `title` | Data |  | Y | Denormalized from the recipe (e.g. "Golden bench image") for the list view. |
| `server` | Link → Server | Y |  | The Active server the scratch build VM is provisioned on (no scheduler — principle #4). `set_only_once`. |
| `base_image` | Link → Virtual Machine Image |  |  | The stock Ubuntu base the build VM boots. Defaults from `placement.default_image()`. `set_only_once`. |
| `status` | Select |  | Y | `Draft` → `Provisioning` → `Building` → `Snapshotting` → `Available` / `Failed`. The single source of truth for the live checklist. Controller-written. |
| `build_virtual_machine` | Link → Virtual Machine |  | Y | The scratch VM this build provisioned + baked. |
| `snapshot` | Link → Virtual Machine Snapshot |  | Y | **The output** — what site/proxy VMs clone from. |
| `build_task` | Link → Task |  | Y | The guest `build.sh` run's Task row (stdout/stderr/exit). Linked even on a failed build. |
| `build_inputs` | Code (JSON) |  | Y | The resolved input commits (frappe / erpnext / bench-cli SHAs) the bake actually built from, harvested from the build Task's `ATLAS_BUILD_*=` stamp. Matters most for `bench-nightly` (its `develop` branches float). |
| `auto_register` | Check |  |  | Default on. If the recipe has a `registers_as`, wire the snapshot into that Atlas Settings field on success (bench → `default_bench_snapshot`). Ignored by recipes with nothing to register (proxy). |
| `warm` | Check |  |  | Default off. Bake a **warm** golden: after the build, run the recipe's `warm_entrypoint` (production stack up + pre-warm + freshen unit), then capture memory + disk at one paused instant into a `kind=Warm` snapshot clones *resume*. Per-server; supersedes the server's previous warm row. Rejected for recipes without a `warm_entrypoint`. `set_only_once`. See [15 § The warm bake](./15-image-builder.md#the-warm-bake-warm). |
| `terminate_build_vm` | Check |  |  | Default off. If set, terminate the scratch build VM after a successful snapshot. Off leaves it Stopped for re-bake / inspection (the snapshot is durable and outlives it — [14-self-serve.md](./14-self-serve.md)). |
| `error` | Small Text |  | Y | The stderr tail on `Failed` (full log in the Build Task). |

The identity tuple (`recipe`, `server`, `base_image`) is immutable after insert —
re-baking with different inputs is a new row, guarded in `validate()`.

### Controller methods & lifecycle

- `before_insert()` — resolve the recipe, copy `title`, default `base_image`,
  set `status = Draft`. A proxy recipe needs no region input — the region it serves
  is read from `Atlas Settings.region` at finalize time.
- `after_insert()` — enqueue `run` on `queue="long"` (it SSHes + waits ~10–20 min).
- `run(image_build_name)` — the bake orchestration (provision → build → stop +
  snapshot → register → optional terminate), committing + pushing
  `image_build_progress` realtime on each transition. Fail-loud (`status = Failed`
  + `error`, re-raise). No-op if not `Draft`. Full step table in
  [15-image-builder.md](./15-image-builder.md).
- `rebake()` — reset an Available/Failed row to `Draft` and re-enqueue (idempotent
  retry).

### Permissions

`System Manager` only (operator/Central-facing), like `Server` / `Task`.

### List view

- Columns: `recipe`, `status`, `snapshot`.
- Standard filters: `recipe`, `status`, `server`.

### Buttons

- **Bake Image** lives on the `Server` form (`Actions ▾`, parity with **Sync
  Image**) and inserts an `Image Build` on that server.
- **Re-bake** on an Available/Failed `Image Build` form re-runs the pipeline.
