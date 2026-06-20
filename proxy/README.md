# Atlas reverse proxy

A TLS-terminating reverse proxy that fronts many Frappe sites. Each site is a
subdomain of a regional wildcard (`*.<region>.frappe.dev`) mapping to one site
VM over public IPv6. The subdomain→VM map is **live and reload-free** — it lives
in an nginx `lua_shared_dict`, written through a unix-socket admin API, and
Atlas reconciles it over SSH-to-the-guest. The full design, rationale, and the
accepted limitations are in the spec chapter,
[`../spec/12-proxy.md`](../spec/12-proxy.md).

**The proxy is an ordinary Atlas Virtual Machine**, not a host service. It is
built *inside* a VM (this directory's `build.sh`, run over SSH) and the built VM
is snapshotted — that snapshot is the reusable proxy image. There is no custom
`Virtual Machine Image`, no exported rootfs, no host service: the Firecracker
jail + per-VM netns + cgroup caps are the sandbox.

The base nginx comes from the **official nginx.org apt repo** (stable), so
`/usr/sbin/nginx` is a genuine, signed, dpkg-owned package and `nginx -V` is
truthful. `build.sh` only **compiles the modules apt cannot supply** — OpenResty
luajit2 plus the Lua / headers-more nginx modules, built as dynamic `.so`s
(`--add-dynamic-module --with-compat`) against the exact installed nginx and
loaded via `load_module` in `nginx.conf`. We own the frozen, mutually-compatible
*module* set; apt owns the base binary and OpenSSL version (`apt-mark hold`ed in
the snapshot).

## Layout

```
conf/nginx.conf            static config (§5): listeners, TLS, the two server blocks
conf/mime.types            asset MIME map
lua/router.lua             request path — subdomain -> upstream via the shared dict (§6.1)
lua/admin.lua              unix-socket admin API: GET/PUT/DELETE /map, POST /sync (§6.2)
lua/persist.lua            dump/load the dict to canonical map.json (§6.3)
html/not_found.html        branded 404/503 page (§5.4)
guest/nginx.service        the guest systemd unit (§8)
guest/tmpfiles.d/          /run/nginx (admin-socket dir) perms
build.sh                   apt-install stock nginx + compile the Lua modules INSIDE the guest (§3.1)
test/                      docker-compose release gate (§9): test_proxy.py + test_build.py
```

Paths mirror the stock Ubuntu `nginx` package — binary `/usr/sbin/nginx`, config
`/etc/nginx/` (with `lua/` alongside), logs `/var/log/nginx/`, pid `/run/nginx.pid`,
state (live `map.json`, `region`, `certs/`, `acme/`) under `/var/lib/nginx/`, the
admin socket at `/run/nginx/admin.sock`, and the unit `nginx.service` — so an
engineer debugging the guest finds everything where `apt install nginx` would put
it.

## Build (the real path: in a guest)

The proxy is built by running `build.sh` inside a freshly-provisioned Ubuntu VM
and snapshotting the result. Atlas drives this from the controller —
`atlas.atlas.proxy.build_proxy(vm)`:

1. Provision an ordinary Atlas VM from the stock Ubuntu image, marked
   `is_proxy` with a `region`.
2. `build_proxy(vm)` SSHes into the guest, uploads this `proxy/` tree, and runs
   `build.sh`. It installs the stock nginx from the nginx.org apt repo at
   `/usr/sbin/nginx`, compiles the OpenResty luajit2 + Lua / headers-more modules
   as dynamic `.so`s against it, installs the config under `/etc/nginx`, the three
   Lua modules, and the guest unit, enables `nginx.service`, writes the region,
   and starts the unit. (Recorded as a `proxy-build` Task row.)
3. Snapshot the VM. That snapshot is the rollable proxy image.

`build.sh` is idempotent: re-running reinstalls the held apt nginx and rebuilds
the modules from the pinned sources. **Every** version is pinned at the top of the
script — the nginx base (`NGINX_VERSION`, an exact nginx.org package version) as
well as the compiled modules — so the base binary and the modules compiled against
it can never drift apart, and two bakes far apart produce the same stack. The
install fails loud if the pinned base can't be served (no silent substitution).
Bumping any pin is a deliberate stack update rolled as a new snapshot.

## Test (the release gate: docker-compose)

The compose harness runs the **same** `build.sh` on plain `ubuntu:24.04` (it adds
the nginx.org repo itself), so a green run exercises the byte-identical stack a
real proxy VM runs — apt base, dynamic-module ABI, cjson cpath, the lot. It
brings up the proxy plus two fake IPv6 upstreams and drives the admin socket.

```sh
cd test
docker compose up --build -d                     # build + start proxy + vm-a + vm-b
python3 -m pytest test_proxy.py test_build.py -v  # behavior + build-shape gate
docker compose down -v
```

Two test files:

- **`test_proxy.py`** — behavior: routing, remap-without-reload, tombstone,
  bulk `/sync` (incl. malformed-body rejection), per-subdomain CRUD, restart
  persistence, HTTP→HTTPS, HTTP/2, socket.io, dead-upstream resilience, TLS floor.
- **`test_build.py`** — build provenance: nginx is the dpkg-owned nginx.org
  package, `apt-mark hold`ed, stable (not mainline), `--with-compat`; the three
  dynamic modules are present *and* loaded at runtime; `cjson.safe` resolves;
  luajit2 is the OpenResty fork; security headers survive the header chain.

The driver reaches the admin socket via `docker compose exec proxy curl
--unix-socket /run/nginx/admin.sock` (from *inside* the container — faithful to
production, where Atlas reaches the socket over SSH-to-the-guest, never a host
mount) and makes HTTPS requests with the wildcard Host/SNI forced onto the local
published port.

## Control plane (Atlas-side)

Atlas owns the map and reconciles each proxy guest from the controller — **not**
via host Tasks but by SSHing directly *into the guest* (`connection_for_guest`).
The implementation is `atlas/atlas/proxy.py` (see
[`../spec/12-proxy.md` § Control plane](../spec/12-proxy.md)):

- `proxy.build_proxy(vm)` — SSH-to-guest, upload this tree, run `build.sh`, write
  the region, start the unit (the build path above).
- `proxy.reconcile_proxy(vm)` / `proxy.reconcile_region(region)` — SSH-to-guest,
  read the live `/map`, byte-compare against the canonical desired map
  (`json.dumps(sort_keys=True, indent=2)`), and bulk `POST /sync` the full
  regional map on drift. One unreachable proxy is recorded as a failed Task and
  skipped; the others still serve.
- `proxy.push_cert(vm, fullchain, privkey)` — SSH-to-guest, drop
  `fullchain.pem`/`privkey.pem` into `/var/lib/nginx/certs/<region>/` (key
  via stdin `tee`, never in argv), reload nginx.

Each guest op records a `Task` row (`proxy-build` / `proxy-sync` /
`proxy-push-cert`). Desired state is the `Subdomain` DocType (`subdomain →
virtual_machine → address`, `region`, `active`); every proxy VM in a region gets
the full `WHERE region = R AND active` map.
