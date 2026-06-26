# The reverse proxy

A TLS-terminating reverse proxy that fronts many Frappe sites. Each site is a
subdomain of a regional wildcard (`*.<region>.frappe.dev`); each subdomain maps
to exactly one site VM, dialed over public IPv6 on port 80 (plaintext). The map
changes constantly and must update **without reloading nginx**. Atlas is the
source of truth and reconciles each proxy's live map over SSH.

## The shape

- **The proxy is an ordinary Atlas Virtual Machine** — operator-owned, marked
  `is_proxy` ([02-doctypes.md](./02-doctypes.md#virtual-machine)).
  No infrastructure-VM tier: it is invisible to the user SPA by ownership, and
  inherits the standard Firecracker jail + per-VM netns + cgroup caps as its
  sandbox. It runs the self-built nginx + Lua stack ([`proxy/`](../proxy)) and
  carries an attached `public_ipv4` (the inbound-v4 primitive,
  [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)) so it can
  terminate v4 **and** v6 on `:443`. Bootstrap titles a proxy VM
  `proxy.{region}.{domain}` (e.g. `proxy.blr1.frappe.dev`), for parity with the
  FQDN titles Site VMs carry — so Desk lists read a recognizable name, not a
  UUID.
- **2–3 proxy VMs per region** behind the one regional wildcard (DNS
  round-robin over their v4 + v6), for resiliency and zero-downtime rolling
  updates. Each proxy is independent and holds the **whole** map.
- **The live map** is a `lua_shared_dict` inside each proxy guest (the in-process
  source of truth), dumped to a sorted, pretty-printed `map.json` read only at
  start. A map change is an atomic dict write — **zero reload**.

## Desired state: the Subdomain DocType

One [`Subdomain`](./02-doctypes.md#subdomain) row per routing entry: `subdomain`
(unique) → `virtual_machine` (the site VM) → `address` (the VM's `/128`,
denormalized) → `active`. Standalone and linked (the Reserved IP
idiom), **not** a child grid on a proxy — every proxy holds the whole map.
Atlas is single-region, so there is no region field on the row; every active
subdomain belongs to the one region by definition.

The desired map is `subdomain_map()` = `{subdomain: address}`
for every active subdomain. Every proxy VM serves that same full map.

## Control plane: Atlas → guest

`atlas/atlas/proxy.py` is the controller side. It is **not** a host Task (which
stages a script onto a Server and runs it there): it runs on the controller and
SSHes **into the guest** — the second SSH target type,
`connection_for_guest(vm)` ([04-tasks.md](./04-tasks.md#how-it-runs)),
reaching the VM's `/128` as `root` with the same Atlas key already in the guest's
`authorized_keys`. The guest's admin API is a **unix socket only**
(`/run/nginx/admin.sock`); SSH-to-the-guest is the only way to reach it,
and the socket's file permissions are the gate.

- **`canonical_json(map)`** — the one canonical serialization: sorted keys,
  2-space indent, one key per line, trailing newline. **Byte-identical** to the
  guest's `persist.lua` output, so the reconcile "in sync?" check is a plain
  string compare, not a semantic diff.
- **`reconcile_proxy(vm)` / `reconcile_proxies()`** — for each proxy VM
  (enumerated by `_proxy_vms()`), read its live `/map` over the admin socket,
  byte-compare against the canonical
  desired map, and bulk-declarative `POST /sync` the full map (streamed to the
  guest `curl --data-binary @-` over SSH stdin) on drift. Idempotent,
  self-healing, **rebuild-safe** (a fresh proxy's empty dict refills on the next
  reconcile). A proxy that can't be reached is recorded as a failed Task and
  **skipped** — one wedged guest never wedges the loop; the others still serve.
- **`push_cert(vm, fullchain, privkey)`** — drop the regional wildcard
  cert/key into the guest's per-region cert dir (`{CERT_DIRECTORY}/{region}`,
  the region from `Atlas Settings.region` via `atlas_region()`, not a VM field;
  private key via `tee` from
  stdin, never in an argv) and reload nginx. Cert pushes are rare, so a reload is
  fine here (unlike map changes). The cert is pushed, never baked into the image,
  so one proxy image serves any region and a renewal is a re-push, not a rebuild.
  The per-region cert-dir layout is kept deliberately so a future customer-domain
  feature can serve more than one cert per proxy.

- **`build_proxy(vm)`** — turn a freshly-provisioned Ubuntu guest into a proxy:
  upload the committed [`proxy/`](../proxy) tree over the same guest-SSH path and
  run [`proxy/build.sh`](../proxy/build.sh) **inside** the guest (compiling nginx +
  Lua from pinned sources), then start the unit. This
  is the controller side of "build inside the guest" (*Build & roll = VM
  lifecycle* below) and the same
  byte-identical stack the compose release gate exercises (the gate's Dockerfile
  runs the same `build.sh`). Idempotent, so it doubles as the re-bake verb. The
  operator snapshots the built VM; that snapshot is the rollable proxy image.

Each guest operation is recorded as a `Task` row (`script` = `proxy-build` /
`proxy-sync` / `proxy-push-cert`, with the proxy VM) for the operator's audit
trail, the same row shape as every host Task.

## Build & roll = VM lifecycle

The proxy is built the Atlas-native way (no custom rootfs, no host service):
provision an ordinary VM from stock Ubuntu, then `build_proxy(vm)` SSHes in,
uploads the [`proxy/`](../proxy) tree, and runs
[`proxy/build.sh`](../proxy/build.sh) inside the guest (`apt install`s the
signed, `apt-mark hold`-frozen nginx.org stock nginx 1.30.3, compiles **only**
the modules apt cannot supply — OpenResty `luajit2` + `lua-nginx-module` +
stream-lua + NDK + resty-core/lrucache + lua-cjson + headers-more — as dynamic
`.so`s against that exact binary, and installs the config, the six Lua modules,
and a thin `nginx.service.d/atlas.conf` drop-in over the package's own unit),
then **snapshot** it — that snapshot is the reusable "proxy
image". Install / update / roll / rollback are the existing VM lifecycle verbs
(provision / rebuild / snapshot / clone), rolled one proxy at a time so DNS keeps
the others serving — a zero-downtime rolling update.

The nginx image's behavior is the **image-level release gate**: the
docker-compose harness under [`proxy/test/`](../proxy/test) exercises the same
`conf/` + `lua/` the in-guest build installs. Beyond the happy path (routing,
remap-no-reload, branded 404, bulk `/sync`, canonical-JSON byte-match,
restart-reload-from-`map.json`, HTTP→HTTPS, HTTP/2, socket.io upgrade) it pins the
subtler behaviors and failure modes — forwarded-header/query fidelity, security
headers on the branded page, the admin method/route matrix, bad-address and
misbehaving-upstream fail-clean, corrupt-`map.json` boot, the dump debounce + its
durability window, concurrent-read atomicity — plus latency/timing/scale guards
(routing overhead, streaming first-byte, TLS resumption, a concurrency soak, a
10k-entry map). `test_proxy.py` + `test_build.py` + `test_latency.py`, all green.
Nothing is installed on the dev host.

## Host-bound facts — the `proxy_vm` Atlas e2e

These prove what only a real droplet can. They are wired as a single e2e use
case, [`atlas/tests/e2e/use_cases/proxy_vm.py`](../atlas/tests/e2e/use_cases/proxy_vm.py),
registered in `run_all` / `run_all_smoke` and run on the shared bootstrapped
droplet. The controller logic underneath each is unit-covered in milliseconds
(`atlas/atlas/test_proxy.py`: canonical JSON, the reconcile diff, the proxy-tree
enumeration; `scripts/lib/atlas/test_reserved_ip_nat.py`: the host NAT math). The
e2e itself needs a billable droplet + a real reserved IP, so it is run on the
operator's turn (`bench --site atlas.tests.local execute
atlas.tests.e2e.use_cases.proxy_vm.run_smoke`), not in the unit suite.

- **Build inside the guest** — `build_proxy` SSHes a fresh Ubuntu VM, uploads the
  `proxy/` tree, and runs `build.sh`; nginx + Lua compiles and the unit comes up.
  (A proxy guest is reached two ways — host-side probes carry the e2e ephemeral
  key, the control plane reaches it via `connection_for_guest` with the
  Atlas-settings key — so the e2e provisions the proxy trusting **both** keys. In
  production the proxy image bakes the Atlas key, the same as every VM.)
- **guest-SSH map sync end-to-end** — `reconcile_proxy` syncs the live map over
  SSH-to-the-guest; the e2e reads `/map` back and asserts it equals the canonical
  desired map byte-for-byte.
- **inbound-:80 to a site from the proxy's vantage** — the public-v6 south-side
  release gate that had never been tested
  ([06-networking.md](./06-networking.md), and the public-v6 hop under
  *Accepted limitations* below): from inside
  the proxy guest, reach a stand-in site VM's `[v6]:80` (the exact
  `proxy_pass http://[<site-v6>]:80` hop). A site's `:80` is reachable by anyone
  on the v6 internet; a future per-VM firewall must scope it to the proxies and
  must not drop the proxy hop.
- **inbound-:443 reachability** — attach a real reserved IPv4 to the proxy, push
  the wildcard cert, and from **off the droplet** (the controller, over the public
  v4 internet) hit `https://<sub>.<region>.frappe.dev` (`--resolve` to the
  reserved v4, `-k` for the self-signed test cert) and get the site's response
  back through the proxy. `:443` is the proxy's first real listener (the attach
  primitive's e2e was previously proven only for SSH/`:22`).
- **rolling rebuild** — stop the proxy, snapshot it, rebuild from that snapshot,
  re-push cert, re-sync map, and confirm it serves again. In production DNS keeps
  the other 2–3 proxies serving while one rolls; the e2e rolls the single proxy
  and re-verifies the front door.

TLS **grade** (A+) is the one image-gate row not automated (needs a real cert /
`testssl.sh`), so it is a manual/D check.

## Why these decisions

The spec records *what* is true; these are the structural choices and the
alternatives they beat, kept so a future change knows what it is overturning.

1. **The proxy runs inside an Atlas VM, not a host service.** An earlier draft
   ran it as a host-level service on a dedicated proxy *node* (its own exported
   rootfs, `RootDirectory=` chroot, systemd hardening drop-ins). Superseded: the
   VM is the universal building block, so the proxy inherits Atlas's lifecycle,
   jailer, cgroup, image/rebuild, and snapshot machinery for free. The VM **is**
   the sandbox; there is no bespoke hardening stack. (The old host-service
   `systemd/` + `install.py`/`update.py` were never built.)
2. **2–3 proxy VMs per region — dedicated, not co-located per host.** Drivers:
   resiliency, rollover, rolling update. The rejected alternative — co-locating a
   proxy with the sites it fronts to make the south hop host-local — would have
   retired the public-v6 caveat (*Accepted limitations* below) but lost the
   dedicated-fleet resiliency. We took the caveat.
3. **Inbound is the real goal — a VM can attach one public IPv4.** This is the
   inbound mirror of the existing egress NAT44, gated to Atlas-owned VMs today.
   On DO it is a reserved IP attached to the *droplet* and host-side 1:1-NATed to
   the guest (DNAT in, SNAT out, same `inet atlas` table) — *not* routed the way
   v6 is, because DO delivers the reserved IP via an **anchor IP** and never ARPs
   for the reserved IP on the link, so the v6 proxy-NDP + `/32`-route recipe has
   nothing to bind to ([06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)).
   The proxy is the primitive's first user; general tenant inbound v4 is a
   deliberate later step ([09-roadmap.md](./09-roadmap.md)).
4. **No infrastructure-VM tier.** The proxy holds the wildcard private key and
   terminates TLS for the region — a higher trust tier than a tenant site — but
   we deliberately do **not** model that as a new DocType. It is an ordinary
   operator-owned `Virtual Machine`, invisible to the user SPA by ownership.
   Accepted risk: it can be Terminated from Desk like any VM (mitigated by
   running 2–3; a terminate-guard is an additive follow-up, see
   [09-roadmap.md](./09-roadmap.md)).
5. **Atlas SSHes into the guest.** A second SSH target type (guest, reaching the
   VM's `/128`) alongside the existing host-root path, used for both map sync and
   cert push. The guest admin API is a unix socket only — SSH-to-the-guest is the
   only way to reach it; socket file perms are the gate. No agent on the guest.
6. **The map is bulk-declarative reconcile, not event sourcing.** Atlas is the
   source of truth; each proxy's dict is a cache. Both sides emit the *same*
   canonical JSON (sorted keys, 2-space indent), so "in sync?" is a byte compare.
   Per-entry PUT/DELETE exist for low-latency single changes; the periodic full
   `/sync` is the backstop.
7. **Stock nginx unless a custom part is *absolutely* necessary.** The base binary
   is genuinely stock — `apt install`'d from the signed nginx.org repo,
   dpkg-owned, `apt-mark hold`-frozen at 1.30.3, at stock paths
   (`/usr/sbin/nginx`, `/etc/nginx`, `/var/log/nginx`, `/run/nginx.pid`), with the
   L4 `stream` core taken straight from it. The build was audited for every place
   it diverged from stock, and four reducible divergences were collapsed back:
   the custom `mime.types` (deleted — `include /etc/nginx/mime.types` reads the
   package conffile), the full `nginx.service` shadow + the standalone `tmpfiles.d`
   file (replaced by a thin [`nginx.service.d/atlas.conf`](../proxy/guest/nginx.service.d/atlas.conf)
   drop-in over the package unit, so `apt upgrade nginx` keeps shipping base-unit
   fixes), and the hand-deleted `conf.d/default.conf` (left in place — deleting a
   dpkg conffile desyncs dpkg; our `server_name _` default_server owns the slot
   and `nginx.conf` simply never `include`s `conf.d`, guarded by a test). The
   drop-in carries only the deltas with **no** stock equivalent:
   `After=atlas-network.service` (order after the guest's static /128 v6 is up),
   `ExecStartPre=nginx -t` (the nginx.org package unit ships *no* config precheck —
   needed so a bad config refuses to start instead of restart-looping under
   `Restart=on-failure`), `LimitNOFILE=1048576` (the ~20000-listener pool), and
   `RuntimeDirectory=nginx`/`RuntimeDirectoryMode=0750` (the 0750-root
   `/run/nginx` admin-socket dir). What **must** stay custom, judged and recorded
   so a future change knows it is not gratuitous:
   - **The `nginx.conf` overwrite** — `load_module`, the top-level `stream{}`
     block, and `worker_connections 65536` cannot be injected by any stock drop-in
     on this base. (`worker_connections 65536`: the gate empirically proved stock
     1024 fails `nginx -t` against the ~20006 pre-opened listeners.)
   - **The four dynamic modules** (lua, stream-lua, headers-more, NDK) — stream-lua
     and headers-more are in no apt repo; Ubuntu's `libnginx-mod-http-lua` is
     ABI-pinned to nginx 1.24 and the wrong version for resty-core 0.1.32's startup
     assert. **luajit2** is the OpenResty fork (not upstream LuaJIT, no apt repo).
     **lua-resty-core + lrucache** — nginx refuses to start without resty-core once
     the Lua module loads; only in the OpenResty distribution. **lua-cjson** — stock
     ships none and the apt one is the wrong interpreter ABI; it must be built
     against the luajit2 fork.
   - **The Lua app** (router/admin/persist × http+stream) — this *is* the
     reload-free product. The stream trio is a forced second copy
     ([17-tcp-proxy.md](./17-tcp-proxy.md): http/stream `lua_shared_dict`s are
     separate address spaces, zone names globally unique across both).
   - **The placeholder cert + region flat-symlink layout** — forced: a single
     `443 ssl default_server` needs a readable cert for `nginx -t`/first boot
     before Atlas pushes the DNS-01 wildcard, and `ssl_certificate` can't
     interpolate `$atlas_region` ([13-tls.md](./13-tls.md)). Only the cert
     *content* is substitutable; the layout must not be touched.
   - **The `stream-admin` client + python3** — the stream admin is a non-HTTP line
     protocol read off the raw socket; curl can't drive it, so the stdlib-only
     client is required.
   - **No logrotate snippet shipped** — the stock `/etc/logrotate.d/nginx` globs
     `*.log` and already covers `admin.log` / `stream-access.log`; a redundant
     snippet would be the gratuitous-custom we are avoiding.

   The systemd drop-in is **host-boot-verifiable only** — the compose gate
   foreground-runs nginx and never loads systemd. One open tightening, a weak win
   deferred: swapping the placeholder cert *content* for the Debian `ssl-cert`
   snakeoil.

8. **Workers drop privilege; the master stays root.** `nginx.conf` sets
   `user nginx;` (the nginx.org `.deb`'s stock locked/nologin account), so the
   master keeps root only to bind `:80`/`:443` + the `10000-19999` stream pool and
   setuid the workers — the workers that parse untrusted internet bytes, run the
   routing Lua, and L4-forward raw tenant TCP run unprivileged. The trade is a
   handful of `build.sh §7` ownership facts the worker write path depends on:
   `/var/lib/nginx` is `root:nginx 0770` because the worker writes `map.json` /
   `stream-map.json` from a timer (rename needs write+exec on the parent), `acme/`
   is `root:nginx 0750` (worker read of the `.well-known` root), while `certs/` +
   the wildcard privkey stay **root-only** — they're read by the master at config
   parse, never by a worker, so the key never needs to be group-readable. No
   systemd `User=` / `NoNewPrivileges` / `ProtectSystem=strict` is added: each
   would break the master's low-port bind or the master→worker setuid; the
   `user nginx;` directive is the correct mechanism. The privilege drop is a
   **runtime** fact `nginx -t` can't see — a non-group-writable state dir would
   pass `-t` and fail silently at the first worker map dump — so `test_build.py`
   carries three assertions for it (master-is-root/workers-are-nginx, a forced
   `POST /dump` re-read, privkey-stays-root-only) and the compose gate exercises
   the nginx-user worker end to end. A host bake is the final pre-ship
   confirmation.

9. **Baseline DoS + recon hardening lives in `nginx.conf`, not a WAF.** The
   front-end carries the cheap, always-safe CIS hardening directly: slow-read /
   slowloris caps (`client_header_timeout` / `client_body_timeout` /
   `send_timeout` = 15s, `keepalive_timeout 10`), an explicit
   `client_max_body_size 50m` matched to the tenant bench's own nginx
   ([bench.toml](../bench/bench.toml) `[nginx]`) so the proxy is never the tighter
   limit and never silently 413s a legitimate upload, `proxy_hide_header Server` /
   `X-Powered-By` so each tenant's gunicorn/werkzeug stack + version doesn't leak
   (nginx re-emits only its own `server_tokens off` bare `Server: nginx`), and
   `error_log … notice` on both planes to capture upstream-connect failures and
   rejected requests the default `error` level drops. Per-IP `limit_conn` /
   `limit_req` are deliberately **not** set: all v4 tenants share one reserved-IPv4
   1:1 NAT, so `$binary_remote_addr` collapses toward a single key and a per-IP
   limit would punish everyone at once (Frappe throttles auth at the backend). The
   remaining CIS gaps (the plaintext south hop → WireGuard, TLS-1.3-only as a
   policy call, a JSON access `log_format`, a rebake-on-CVE SLA) are tracked in
   [llm/references/cis-nginx-benchmark-gaps.md](../llm/references/cis-nginx-benchmark-gaps.md).

## Accepted limitations

Carried into the release gate, true today:

- **The proxy→site south hop is over the public IPv6 internet** (proxies and
  sites are generally on different hosts; there is no private fabric). A site's
  `:80` is therefore reachable by anyone on the v6 internet, not just the proxy.
  Scoping that exposure is an active security gap — the south-side firewall in
  [09-roadmap.md](./09-roadmap.md). The proxy is path-agnostic, so a future
  private fabric (ULA `fc00::/7`) changes only the address in the map.
- **One reserved IP per host, for now** — the DO anchor is per-droplet, so the L3
  DNAT can't distinguish two reserved IPs on one host. Fine at one proxy VM per
  host; multi-reserved-IP is a later step.
