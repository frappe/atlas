# Atlas — Specification

Atlas is a Frappe app for managing Firecracker virtual machines on servers.
It is the lowest layer of a Frappe hosting platform. Sites, benches, IAM, and
billing live in separate apps on top.

The spec describes the system as it is and is the source of truth. When the
spec and code disagree, the spec is authoritative: update the code to match,
unless the spec is wrong — in which case update the spec deliberately and
keep it the source of truth.

## Goals

- Track servers (the hosts that run Firecracker), whether provisioned through
  a cloud API or registered after the operator built them by hand.
- Track virtual machines that run on those servers.
- Bootstrap a fresh server so it can host virtual machines.
- Provision, start, stop, and delete Ubuntu 24.04 Firecracker virtual machines.
- Drive everything from Atlas over SSH; record every task.
- Give each virtual machine a public IPv6 address.
- Give each virtual machine outbound IPv4 reachability via host NAT44.

## Non-goals (this iteration)

- No sites, benches, apps, databases, or workloads.
- No teams, billing, or quotas **enforced inside Atlas**. Those policies live
  in **Central**, the global front door that drives Atlas (see
  [16-central.md](./16-central.md)): Central owns end-users and teams, and
  **pre-checks capability, billing, and quota** before it asks Atlas to act.
  Atlas stays policy-unaware — it attributes each resource to a `Tenant`
  ([02-doctypes.md § Tenant](./02-doctypes.md#tenant)) for grouping and enforces
  only **physical capacity**. Atlas is **operator/Central-facing only** (System
  Manager): there is no end-user role or owner-scoping, and no in-app end-user
  UI. The frappe-ui dashboard SPA and the self-serve signup on-ramp (and their
  `Atlas User` role) were retired now that Central fronts the user experience and
  drives site/VM creation via `create_site` / `create_vm`
  (see [11-user-ui.md](./11-user-ui.md), [14-self-serve.md](./14-self-serve.md)).
- No CLI. We will build one later on top of the same Frappe APIs.
- No inbound IPv4 to the guest and no per-VM public IPv4 (outbound v4 is via
  host NAT44). Private networking between VMs is now a **shipped** capability —
  a WireGuard **host mesh** carries a per-tenant `fdaa::` private plane
  ([25-private-networking.md](./25-private-networking.md)); Phase 1 (universal
  private addressing + host-nftables isolation) is wired, later phases (the proxy
  dialing dark VMs, fully-dark VMs, `.internal` DNS) are deferred there.
- No SELinux or AppArmor profile yet. Atlas connects to the host as **root**
  over SSH to run Tasks, and the host *is* hardened at bootstrap (CIS sysctls,
  an sshd drop-in, a kernel-module blocklist, unattended security updates,
  KSM/swap off — see [03-bootstrapping.md § Host hardening](./03-bootstrapping.md)).
  Each **Firecracker process is jailed**: started via the `jailer` binary, it
  runs under a per-VM uid/gid, chrooted into the VM's own jail, with per-VM
  cgroup-v2 memory/CPU caps and its own network namespace. Still deferred (see
  [09-roadmap.md](./09-roadmap.md)): dropping the root SSH transport, an
  AppArmor profile, CPU *pinning* (we cap CPU bandwidth, not affinity), a new
  PID namespace per VM, custom seccomp filters.
- No image build pipeline. We download Ubuntu cloud images and use them.
- No live migration, no high availability. Firecracker memory-state
  snapshots exist for exactly one internal purpose — the opt-in fast
  stop/start path on the same host (an opted-in stop captures RAM, the next
  start resumes it in milliseconds; see
  [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md)) —
  and are never operator-facing artifacts or transportable between hosts.
  (Disk snapshots — instant copy-on-write LVM thin snapshots of the VM's
  disk — are supported; same doc.)
- No autoscaling or scheduling. The operator picks the server in Desk; an
  owner-scoped user creating a machine gets the first Active server with room (a
  default, not a scheduler — see [11-user-ui.md](./11-user-ui.md)). "Room" is
  oversubscribable: a host's effective vCPU budget is its physical total times
  `Atlas Settings.overprovision_factor` (default 1), and a host whose size we
  can't price counts as unlimited.
- No metrics or alerting. `journalctl` is enough.
- One in-app UI, one audience. **Operators** use Desk (`/app/atlas`) — the whole
  fleet, providers, servers, image sync, ad-hoc tasks. There is no end-user
  audience in Atlas: the frappe-ui `/dashboard` SPA and the self-serve signup
  on-ramp (and the `Atlas User` role + owner-scoping they relied on) were
  **retired** now that Central is the front door. See
  [11-user-ui.md](./11-user-ui.md).
- **Central is the front door.** Above Atlas sits **Central**
  ([16-central.md](./16-central.md)) — the global control plane that owns
  identity, teams, and billing and is the face of all customer actions. Central
  drives a regional Atlas by **logging in as a single service user and calling
  whitelisted methods**: the lifecycle methods, plus the dedicated
  `create_vm` / `create_site` endpoints that get-or-create the `Tenant` and insert
  the resource. Atlas exposes no separate command API; Central is just an
  authenticated client of the existing Frappe endpoints. The operator Desk
  continues to work; Central's console is the customer-facing surface.

## Operating principles

1. **Desk is the operator UI.** Every *operator* operation is a DocType, a
   button on a DocType, or a server method on a DocType, rendered in Desk — no
   custom operator pages. There is no end-user UI in Atlas: Central is the
   customer-facing front door ([11-user-ui.md](./11-user-ui.md),
   [16-central.md](./16-central.md)), driving creation via `create_vm` /
   `create_site` and reading state back via events / polls.
2. **The Frappe site is the source of truth.** A server is a cache; we can
   rebuild its on-disk state from the Frappe database. We do not scrape state
   back from the server.
3. **One task, one script.** Atlas uploads one script to a server over SSH and
   runs it. The script is the unit of work; we do not chain per-step SSH calls.
   Scripts are typed Python (`--kebab-case` flags in, one `ATLAS_RESULT=` JSON
   line out); a couple of trivial shell scripts (e.g. `reboot-server.sh`)
   remain. See [04-tasks.md](./04-tasks.md).
4. **One virtual machine per server slot.** The operator picks the server
   when provisioning in Desk. No scheduler. An owner-scoped *user* creating a
   machine does not pick a server — the controller fills the first Active server
   with room and the default image ([11-user-ui.md](./11-user-ui.md),
   `placement.py`); the operator still owns which servers are Active. That is a
   default, not a scheduler.
5. **Few dependencies.** Frappe + standard library + the system `ssh` command.
   On the server: a uv-managed virtualenv on CPython 3.14 that bootstrap creates
   and `uv pip install`s the `atlas` package into (the task scripts run under it,
   not the host's stock `python3`; see
   [03-bootstrapping.md § The Atlas interpreter and CLI](./03-bootstrapping.md)),
   plus `firecracker`, `systemd`, `iproute2`, `nftables`, `curl`, `jq`,
   `e2fsprogs`, `squashfs-tools`, `lvm2`, `thin-provisioning-tools`, and —
   for VM migration ([24-vm-migration.md](./24-vm-migration.md)) — `qemu-utils`
   (the `qemu-nbd` disk export), `nbd-client`, `socat` (the §2.1 tunnel), and
   the `nbd` + `dm_clone` kernel modules from `linux-modules-extra-$(uname -r)`.
   The package is stdlib-only today, but installing it the standard way means a
   real dependency is fine.
   No agent runs on the server.
6. **Don't import — copy.** If a third-party library has a good idea (pyinfra,
   zx), reimplement the small subset we need. We avoid library coupling on a
   foundational layer.
7. **Names are full words.** `Server`, `Task`, `Virtual Machine`,
   `Virtual Machine Image`, `Reserved IP`, `Atlas Settings`. No `VM`,
   `Cmd`, or `Metal Node`.

## Read this in order

1. [Architecture](./01-architecture.md)
2. [DocTypes](./02-doctypes.md)
3. [Bootstrapping a server](./03-bootstrapping.md)
4. [Tasks: the SSH execution model](./04-tasks.md)
5. [Virtual machine lifecycle](./05-virtual-machine-lifecycle.md)
6. [Networking](./06-networking.md)
7. [Filesystem layout on the server](./07-filesystem-layout.md)
8. [Images](./08-images.md)
9. [Roadmap and deferred decisions](./09-roadmap.md)
10. [Desk UI (operator)](./10-desk-ui.md)
11. [User UI — the owner-scoped end-user boundary](./11-user-ui.md)
12. [The reverse proxy](./12-proxy.md)
13. [TLS & domain layer](./13-tls.md)
14. [Self-serve sites](./14-self-serve.md)
15. [Image builder](./15-image-builder.md)
16. [Central — the global control plane](./16-central.md)
17. [The TCP proxy](./17-tcp-proxy.md)
18. [Self-service subdomain routing (bench-admin sites)](./18-bench-self-routing.md)
19. [The customer gateway (WireGuard dial-in)](./19-vpn-broker.md) — *superseded; now a gateway VM on the [mesh](./25-private-networking.md)*
20. [The per-VM public firewall](./20-firewall.md)
21. [The Central-managed tunnel (management-plane lockdown)](./21-tunnel.md)
22. [Observability — making long-running tasks legible](./22-observability.md)
23. [Supply chain — the external artefacts Atlas pulls](./23-supply-chain.md)
24. [VM migration between hosts](./24-vm-migration.md)
25. [Private networking (the WireGuard host mesh)](./25-private-networking.md)
26. [Docker compatibility (`docker run` against microVMs)](./27-docker-compat.md) — *design / proposal*
27. [The core ↔ service boundary and the `satellite` app](./28-core-service-boundary.md) — *how service logic (proxy, gateway, mesh, bench/site) leaves core for a separate app via an explicit VM-lifecycle seam*

## First run on a fresh site

Configuration is the **explicit setup contract** in
[`atlas/setup.py`](../atlas/setup.py): one typed entry point that writes every
value Atlas needs (the Settings Singles, the `Root Domain`) and **never reads
`frappe.conf` at runtime**. It has three front-ends, all driving the same
Layer-1 `setup()` setters:

- The **Frappe Setup Wizard** — the operator-facing first-run path. On a fresh
  site the wizard collects the provider type, SSH key, region, vendor
  credentials, and the optional TLS block, then `setup.get_setup_stages`
  applies them (`atlas/public/js/setup_wizard.js` + the `setup_wizard_*` hooks).
  A **Test Connection** button (`setup.wizard_discover`) probes the vendor with
  the just-typed credentials and turns the SSH-key / project boxes into pick-lists
  before anything is saved. The default size/image are **not** asked: `setup()`
  adopts the provider's `discover()` default into the catalog (the
  `atlas_*_default_*` config keys override it), and the operator can flip the
  `is_default` `Provider Size` / `Provider Image` row anytime.
- `setup.run(config)` — the scripted path: a plain `{provider, tls?}` dict, one
  JSON document per environment. CI / E2E / fast-deploy call this.
- `setup.from_site_config()` — the **back-compat adapter** that reads the legacy
  `atlas_*` site-config keys one place and builds the `config` dict. Existing
  benches keep their keys; the `seed_settings_from_site_config` patch reads them
  once at `bench migrate` and seeds the Singles, after which they are no longer
  read. New benches use the wizard or `setup.run` — they do **not** set
  `atlas_*` keys.

`setup.run` only **configures**; it never provisions, bakes, or issues (a
re-runnable config step must not strand billable infra). After config, the
operator-visible build order on the desk is:

1. **Atlas Settings / per-vendor Settings** — already populated by the wizard
   (or `setup.run`): active `provider_type`, the SSH key, region, and the
   vendor's API token / `ssh_key_id` / default size + image (skip the vendor
   Single for `Self-Managed`).
2. **Server** — provisioned by clicking **Provision Server** on
   **Atlas Settings**.
3. **Virtual Machine Image** — the kernel + rootfs pair to install.
4. **Virtual Machine** — created against a Server and an Image, then
   **Provision**ed.

To skip the clicking and stand up server → image → VM in one
shot, run [`atlas/bootstrap.py`](../atlas/bootstrap.py):

```
bench --site <site> execute atlas.bootstrap.run
```

`bootstrap.run` is the scripted config-plus-provisioning path: it calls
`setup.from_site_config()` → `setup.run()` to configure (reading the `atlas_*`
keys), then layers the provisioning on top — provisions a Server, seeds the
`Provider Size` / `Provider Image` catalogs, registers + syncs the base image,
and provisions one VM, using only the whitelisted methods the desk buttons
call. Requires a `bench worker` running because `provision_server` and
`sync_to_server` both enqueue background jobs. The file's docstring lists every
`atlas_*` config key.

To put a site behind a real cert, layer the proxy ([12-proxy.md](./12-proxy.md))
and TLS ([13-tls.md](./13-tls.md)) setup on top:

6. **Route53 Settings** — the DNS account (DNS-01); pick the active DNS vendor
   on `Atlas Settings.dns_provider_type` (`Route53`).
7. **Lets Encrypt Settings** — the ACME account (directory URL, account
   email); the active issuer is `Atlas Settings.tls_provider_type`.
8. **Root Domain** — one row per region (`<region>.frappe.dev`, `region`);
   click **Issue / Renew Certificate** to issue the regional wildcard and push
   it onto every proxy VM in the region. The domain/TLS vendor types are
   denormalized onto the row from the active vendors at insert.

To script steps 6-8, run [`atlas/bootstrap.py`](../atlas/bootstrap.py)'s TLS
tail instead of clicking:

```
bench --site <site> execute atlas.bootstrap.run_with_proxy
```

`run_with_proxy` is `run` plus the TLS tail: it does the compute bootstrap, then
— only if the `atlas_tls_domain` + Route 53 + ACME config keys are present —
writes the DNS/TLS vendor types (`Atlas Settings.dns_provider_type`,
`Atlas Settings.tls_provider_type`), the two per-vendor Settings, and the
`Root Domain` row, then issues the regional wildcard (defaulting to
Let's Encrypt **staging** so an unattended run never burns production quota; set
`atlas_acme_directory_url` for a trusted cert). Absent those keys it skips the
tail and behaves like `run`. The file's docstring lists every config key.

The TLS layer has a **controller-host dependency**: `certbot`,
`certbot-dns-route53`, `openssl`, and `boto3` must be installed on the Atlas
controller (issuance runs there, not over SSH — see [13-tls.md](./13-tls.md)).

## Operator use cases

Everything Atlas does for an operator falls into one of the use cases
below. The list is the spec's index of operator-visible behavior; the
e2e suite mirrors it (one module per use case, though a closely-related
operation may ride along in a sibling module — Promote rides along in
the snapshot module — see
[`atlas/tests/e2e/use_cases/`](../atlas/tests/e2e/use_cases)). New
operator-facing features add to this list; new tests follow it.

| Use case                       | Operator action                                         | Spec |
| ------------------------------ | ------------------------------------------------------- | ---- |
| Provision a server             | `Atlas Settings` → **Provision Server**                 | [03-bootstrapping.md](./03-bootstrapping.md) |
| Adopt an existing server       | `Atlas Settings` → **Discover Servers** (preview the vendor's servers, tick which to import as `Pending` rows; Bootstrap takes each to Active) | [03-bootstrapping.md](./03-bootstrapping.md#adopting-an-already-provisioned-server) |
| Sync an image to a server      | `Virtual Machine Image` → **Sync to Server / All**      | [08-images.md](./08-images.md) |
| Bake an image                  | `Image Build` → **New / Re-bake**, or `Server` → **Bake Image** | [15-image-builder.md](./15-image-builder.md) |
| Provision a virtual machine    | `Virtual Machine` → **Provision**                       | [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) |
| Operate a virtual machine      | `Virtual Machine` → **Start / Stop / Restart / Pause / Resume / Terminate** | [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) |
| Manage a VM's disk and size    | `Virtual Machine` → **Snapshot / Rebuild / Resize**; `Virtual Machine Snapshot` → **Restore to VM / Clone to new VM / Delete** | [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) |
| Promote a snapshot to an image | `Virtual Machine Snapshot` → **Promote to image** (or `Image Build` → **Promote to image**): same-server base image new VMs pick via the `image` field | [08-images.md](./08-images.md#two-origins-for-a-base-image-a-url-or-a-snapshot-promote) |
| Attach a public IPv4 to a VM   | `Reserved IP` → **Attach / Detach** (the inbound-v4 primitive: DNAT in, SNAT out) | [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip) |
| Broker a VPN tunnel to a VM    | (user/Central-driven) `request_vpc_access` / `revoke` dials the owner in as a peer on their tenant `/48` via the **customer gateway VM** on the mesh — one shared `wg0`, one client `/128` (supersedes the host-terminated broker) | [25-private-networking.md](./25-private-networking.md#the-customer-gateway--external-dial-in-to-the-mesh), [19-vpn-broker.md](./19-vpn-broker.md) |
| Issue a TLS cert for a region  | `Root Domain` → **Issue / Renew Certificate**; `TLS Certificate` → **Issue/Renew / Push to Proxies**; `Route53 Settings` / `Lets Encrypt Settings` → **Test Connection** | [13-tls.md](./13-tls.md) |
| Route guest-created bench sites | (guest-driven, no operator action) the in-guest `bench-domain-provider register`/`deregister` POSTs reserve/remove a `Subdomain` the controller arbitrates (uniqueness, brand denylist, per-VM cap, own-VM scoping by source `/128`); the `wildcard-domains`/`proxy-servers` queries answer pilot's host-level questions; every call audited; `terminate()` is the only controller-side teardown | [18-bench-self-routing.md](./18-bench-self-routing.md) |
| Run an ad-hoc task / reboot    | `Server` → **Run Task / Reboot**                        | [04-tasks.md](./04-tasks.md) |
| Run an ad-hoc command on hosts/guests | `SSH Console` → **Execute** (fan one command across Servers and/or Virtual Machines; per-target output streams back; every run recorded as an `SSH Command Log`), or `Server` / `Virtual Machine` → **Run Command** (pre-targets the console at one row) | [04-tasks.md § The SSH Console](./04-tasks.md#the-ssh-console-ad-hoc-commands) |
| Click any button on the desk   | every form button driven through `run_doc_method`       | (this section, *Desk-button coverage*) |
| Talk to DigitalOcean           | (internal) verify the DO HTTP client                    | [01-architecture.md](./01-architecture.md) |
| Run a script over SSH directly | (internal) `run_task(connection=…)` before a `Server` row exists | [04-tasks.md](./04-tasks.md) |

The last two are internal contracts the higher use cases depend on; they
have their own e2e modules because they fail in different ways than the
operator-facing flows do. *Desk-button coverage* sits between them and
the operator flows: it re-runs every button through the HTTP layer the
desk uses, catching failures the direct-Python use cases miss.

### Desk-button coverage

The five operator-facing use cases above call controller methods
directly: `provider.provision_server(name)`, `vm.start()`,
`image.sync_to_server(server)`. That covers the methods but skips the
layer the desk actually hits — Frappe's
`/api/method/run_doc_method` endpoint that `frm.call(...)` posts to.
Two failure shapes only show up at that layer:

1. **Failures from DigitalOcean.** When the operator's DO token is
   expired or scoped wrong, `provision_server` raises `DigitalOceanError`
   before any `Server` row is inserted. Direct Python calls in tests
   don't exercise the "row must not leak when the API rejects us" branch.
2. **Argument-shape failures from dialogs.** The `Run Task` Code field
   posts `variables` as a JSON *string*, not a dict. The `Sync to
   Server` Link field and `Provision Server` Data field post strings.
   Direct Python calls pass typed Python values and skip the
   string-decode paths — so malformed JSON in the `Run Task` dialog
   surfaces to the operator as an opaque 500 instead of a clean
   ValidationError.

The [`desk_buttons`](../atlas/tests/e2e/use_cases/desk_buttons.py) use
case drives every button through `frappe.handler.run_doc_method` with
the exact argument shape the desk sends, plus the negative paths an
operator can trigger by hand: bad DO token, malformed JSON, duplicate
server name, wrong-state lifecycle button, unknown script. It piggybacks
on the shared bootstrapped server; the bad-token path uses a throwaway
provider so no droplet is ever created.

When a button is added to a form, its happy path and the dialog's
operator-visible negative paths go here, not in the
corresponding direct-Python use case.

## Testing

E2E tests are grouped by the use cases above, not by implementation
detail. The mapping is one module per use case under
[`atlas/tests/e2e/use_cases/`](../atlas/tests/e2e/use_cases); the
filenames mirror the table above (`server_provisioning.py`,
`image_sync.py`, `virtual_machine_provisioning.py`,
`virtual_machine_lifecycle.py`, `virtual_machine_snapshot.py`,
`reserved_ip_inbound.py`, `proxy_vm.py`, `tls_issuance.py`,
`self_serve_site.py`, `warm_restore.py`, `run_task.py`,
`desk_buttons.py`, `digitalocean_client.py`, `ssh_primitive.py`).

Each use-case module is the **single source of truth** for that
operation's end-to-end coverage. It owns:

1. The happy path — one full pass against a real server.
2. The operator-visible negative paths for that use case (e.g. the
   image-missing path lives in `virtual_machine_provisioning` because
   that's where the operator triggers it).
3. The DocType-level validation throws that guard the same method —
   immutability, required fields, JSON shape, state-machine guards.
4. Synchronous-path coverage for the background jobs the use case
   normally enqueues (e.g. `image_sync` calls `execute_task` directly,
   not only via `frappe.enqueue`).

Bias toward adding a check to an existing use case. Add a new use-case
module only when the operator gets a new button.

### Host facts vs. unit-covered logic

Within each module, checks fall into two classes, and the cost gap
between them is enormous:

- **Host facts** — what only a real droplet, boot, or live API can prove.
  These are why e2e exists. They are also where the wall-clock goes:
  fresh droplet provision + bootstrap (~10 min, paid by any run that has
  no reusable Active server), the image-sync pipeline (download + sha256
  + unsquash + mkfs, up to 900s), each VM boot to Running (60–120s), each
  guest SSH probe — identity, IPv4 egress (180s), and the reboot
  drop-and-reconnect in `run_task` (30s + up to 300s poll). The
  **inbound-v4** primitive (`reserved_ip_inbound`) is a host fact with a
  twist: it allocates a *real* DO reserved IP and proves both halves of the
  host 1:1-NAT — **inbound DNAT** by reaching the reserved v4 from **off the
  droplet** (the controller, the only honest vantage: a host-local packet
  skips PREROUTING), and **egress SNAT** by a guest-side `cdn-cgi/trace`
  asserting the source is the reserved IP. Teardown releases the (billable)
  IP in a `finally`. The **reverse proxy** (`proxy_vm`) builds on it: it
  compiles the nginx+Lua stack *inside* a freshly-provisioned guest
  (`atlas.atlas.proxy.build_proxy`), routes a stand-in site through it, and
  proves the four facts only a droplet can — the public-v6 south hop
  (inbound `:80` to the site from the **proxy's own vantage**, the §2.1
  release gate that had never been tested), the guest-SSH live-map sync
  (`reconcile_proxy`, read back byte-for-byte), the inbound `:443`
  reachability (a reserved v4 attached to the proxy, the pushed wildcard
  cert, an off-droplet HTTPS request that routes through the proxy to the
  site), and a **rolling rebuild** (rebuild from the proxy's own snapshot,
  re-push cert, re-sync, serve again). Same `finally` teardown: release the
  reserved IP, terminate both VMs.
- **Unit-covered logic** — validation throws, state-machine guards, pure
  helpers (networking math, DO response parsing, JSON-shape checks). Every
  one is also covered by a `test_*.py` unit test that runs in
  milliseconds with no host. They live in the e2e module too, so a full
  run records them under one umbrella, but they do not need a host.

Each module exposes both a full `run()` (host facts **and** the
unit-covered logic) and a **`run_smoke()`** that runs only the host
facts. `vm-lifecycle` and `vm-snapshot` smoke equals their full run —
every step there is an on-host probe, so there is nothing to trim. The
host-only `run_smoke` paths are what the development loop uses; the
logic is left to the unit suite (`bench --site atlas.tests.local
run-tests --app atlas`, seconds).

### Entry points

Each use-case module separates the **host facts** (what only a real
droplet, boot, or live API can prove) from the **validation throws and
pure helpers** that the unit suite already covers in milliseconds. Two
runner families fall out of that split:

- `bench --site atlas.tests.local execute atlas.tests.e2e.run_all_smoke` —
  the development loop. Every use case's `run_smoke` against **one shared
  bootstrapped droplet**: boot, sync pipeline, guest identity, IPv4
  egress, the desk HTTP wrapper. Skips everything the unit suite owns, so
  pair it with `bench --site atlas.tests.local run-tests --app atlas`
  (seconds). Reboot is excluded; pass
  `run_task.run_smoke(reboot=True)` when you touched the reconnect path.
- `bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.<use_case>.run_smoke`
  — the host-only path for **one** use case. Run only the slice you
  touched during development.
- `bench --site atlas.tests.local execute atlas.tests.e2e.run_all` — the
  full regression: host facts **plus** the unit-redundant validation
  throws, against one shared droplet, deleted at the end.
- `bench --site atlas.tests.local execute atlas.tests.e2e.run_all_coverage` —
  `run_all` plus the dedicated-droplet use cases
  (`digitalocean_client.run`, `server_provisioning.run`). Cost: three
  billable droplets.
- `bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.<use_case>.run`
  — happy-path-plus-validation for one use case. Use cases that need
  the shared bootstrapped server expose `run_against_shared(reuse=True,
  keep=True)` as well; they use the `phase()` context manager from
  `_droplets.py`.

The dedicated-droplet host facts — fresh provision
(`server_provisioning.run`) and the DO round trip
(`digitalocean_client.run_smoke`) — own their own droplets and are
invoked directly, not folded into `run_all_smoke`. The TLS issuance use
case (`tls_issuance.run_smoke`) is the same shape: it needs a live AWS
Route 53 zone and a real ACME round trip on top of the proxy infra, so it
is invoked directly and skips cleanly (raising `MissingConfig` before any
billable provision) on a site without the `atlas_tls_*` config keys. It is
the only e2e that exercises the real producer chain (Let's Encrypt →
DNS-01 → certbot → `_push_to_proxies`); `proxy_vm` uses a self-signed
stand-in cert. It needs the controller-host deps (certbot,
certbot-dns-route53, openssl, boto3) and fails its preflight with a clear
message if they are absent.

The **self-serve site** use case (`self_serve_site.run_smoke`) is the
superset of all of the above: it drives the real `create_site` →
golden-image site VM → deploy → HTTP 200 → subdomain → off-droplet HTTPS
flow on **both IPv4 and IPv6**. It reuses `proxy_vm`'s proxy + reserved-IP
helpers, `tls_issuance`'s real LE-staging producer chain, and `bench_image`'s
golden-snapshot bake, so it has the same preconditions (the `atlas_tls_*`
config keys + controller-host deps) and skips cleanly on a bare site. It
needs a golden bench snapshot: it uses `Atlas Settings.default_bench_snapshot`
if set + Available, else bakes one inline before any billable site provision.
It also asserts the mirror row Central reflects and the `Tenant` stamp on the
real path. Like `tls_issuance`, it is
invoked directly (not folded into `run_all_smoke`); its `auto_provision`
chain is driven by the **background worker** (the same worker the VM
provisioning e2e relies on), so the worker must be up. It also rides the
**bench self-routing** host fact (spec/18, one-way push): on the same running site
VM (a bench VM), the real in-guest `bench-domain-provider` binary *register*s a name over
IPv6 (the controller resolves the VM from its v6 source `/128`), the proxy serves it, a
forced-create-failure rollback leaves no stray, drop+deregister stops it, the host-level
`wildcard-domains`/`proxy-servers` queries answer, and a direct VM terminate leaves no
stale `Subdomain` — none of which Atlas was asked to do.

The **warm restore** use case (`warm_restore.run_smoke`) covers the warm
snapshot fan-out ([05](./05-virtual-machine-lifecycle.md), [15](./15-image-builder.md)):
a warm bake (`Image Build`, `warm=1`) on the shared droplet, then two clones
restored from the one golden — asserting per-clone identity (distinct
hostname / machine-id / SSH host key, `/etc/atlas-vm-uuid` adopted), the
shared `boot_id` that proves a restore rather than a boot, warm serving of the
baked `site.local` with no deploy step, the real `deploy_site` (admin-password
reset on the baked `site.local`) on a warm clone, and the cold-boot fallback when
the captured host signature is tampered. Heavy (a full bench bake on first run) and so invoked directly, not
folded into `run_all_smoke`; re-runs reuse the server's Available warm golden.
It needs the background worker (clone auto-provision).

Every e2e-created droplet is tagged `atlas-e2e`. The harness pre-sweep
prints droplets older than 30 minutes so the operator can delete them
by hand (the DO account also hosts production).

### Shared helpers

`_config.py`, `_droplets.py`, `_image.py`, `_tasks.py` (and the
`_shared.py` re-export shim) under [`atlas/tests/e2e/`](../atlas/tests/e2e)
are the substrate. Add helpers there when at least two use cases would
benefit; single-use helpers stay private to their module.

Every e2e input — DO/Scaleway credentials, the SSH key, the TLS account, the
test region/size/image — comes from **one explicit JSON fixture**, not
`frappe.conf`. Its path is `$ATLAS_E2E_CONFIG` (default
`~/.cache/atlas-e2e/config.json`); fill it once per dev box. This is the
test-side mirror of `setup.run(config)`: the harness drives the same explicit
contract instead of reading site config, and `bootstrap.restore_credentials`
re-applies this fixture through the same Layer-1 setters. A missing file or
absent required key raises `MissingConfig` naming what to add, so a fixture
without a `tls` / `scaleway` block skips that e2e cleanly. The fixture shape is
documented in [`_config.py`](../atlas/tests/e2e/_config.py).
