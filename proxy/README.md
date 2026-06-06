# Atlas reverse proxy

A TLS-terminating reverse proxy that fronts many Frappe sites. Each site is a
subdomain of a regional wildcard (`*.<region>.frappe.dev`) mapping to one site
VM over public IPv6. The subdomain→VM map is **live and reload-free** — it lives
in an nginx `lua_shared_dict`, written through a unix-socket admin API, and
Atlas reconciles it over SSH-to-the-guest. The full design and rationale are in
[`../llm/proxy-design.md`](../llm/proxy-design.md); the spec chapter is
[`../spec/12-proxy.md`](../spec/12-proxy.md).

**The proxy is an ordinary Atlas Virtual Machine**, not a host service. It is
built *inside* a VM (this directory's `build.sh`, run over SSH) and the built VM
is snapshotted — that snapshot is the reusable proxy image. There is no custom
`Virtual Machine Image`, no exported rootfs, no host service: the Firecracker
jail + per-VM netns + cgroup caps are the sandbox.

## Layout

```
conf/nginx.conf            static config (§5): listeners, TLS, the two server blocks
conf/mime.types            asset MIME map
lua/router.lua             request path — subdomain -> upstream via the shared dict (§6.1)
lua/admin.lua              unix-socket admin API: GET/PUT/DELETE /map, POST /sync (§6.2)
lua/persist.lua            dump/load the dict to canonical map.json (§6.3)
html/not_found.html        branded 404/503 page (§5.4)
guest/atlas-proxy.service  the guest systemd unit (§8)
guest/tmpfiles.d/          /run/atlas-proxy perms
build.sh                   compile nginx+Lua INSIDE the guest, install the stack (§3.1)
test/                      docker-compose release gate (§9)
```

## Build (the real path: in a guest)

The proxy is built by running `build.sh` inside a freshly-provisioned Ubuntu VM
and snapshotting the result. Atlas drives this from the controller —
`atlas.atlas.proxy.build_proxy(vm)`:

1. Provision an ordinary Atlas VM from the stock Ubuntu image, marked
   `is_proxy` with a `region`.
2. `build_proxy(vm)` SSHes into the guest, uploads this `proxy/` tree, and runs
   `build.sh`. It compiles vanilla nginx + OpenResty luajit2 + lua-nginx-module
   from pinned sources, installs `/opt/atlas-proxy`, the config, the three Lua
   modules, and the guest unit, enables `atlas-proxy.service`, writes the region,
   and starts the unit. (Recorded as a `proxy-build` Task row.)
3. Snapshot the VM. That snapshot is the rollable proxy image.

`build.sh` is idempotent: re-running rebuilds from the pinned sources. The
pinned versions live at the top of the script; bumping one is a deliberate stack
update rolled as a new snapshot.

## Test (the release gate: docker-compose)

The compose harness runs the **same** `build.sh`, so a green run exercises the
byte-identical stack a real proxy VM runs. It brings up the proxy plus two fake
IPv6 upstreams and drives the admin socket.

```sh
cd test
docker compose up --build -d          # build + start proxy + vm-a + vm-b
python3 -m pytest test_proxy.py -v    # routing, remap-no-reload, /sync, restart,
                                      #   HTTP->HTTPS, HTTP/2, socket.io, canonical JSON
docker compose down -v
```

The driver talks to the admin socket via `curl --unix-socket test/run/admin.sock`
(bind-mounted out of the container) and makes HTTPS requests with the wildcard
Host/SNI forced onto the local published port.

## Control plane (Atlas-side)

Atlas owns the map and reconciles each proxy guest from the controller — **not**
via host Tasks but by SSHing directly *into the guest* (`connection_for_guest`).
The implementation is `atlas/atlas/proxy.py` (see
[`../llm/proxy-design.md`](../llm/proxy-design.md) §7 and
[`../spec/12-proxy.md`](../spec/12-proxy.md)):

- `proxy.build_proxy(vm)` — SSH-to-guest, upload this tree, run `build.sh`, write
  the region, start the unit (the build path above).
- `proxy.reconcile_proxy(vm)` / `proxy.reconcile_region(region)` — SSH-to-guest,
  read the live `/map`, byte-compare against the canonical desired map
  (`json.dumps(sort_keys=True, indent=2)`), and bulk `POST /sync` the full
  regional map on drift. One unreachable proxy is recorded as a failed Task and
  skipped; the others still serve.
- `proxy.push_cert(vm, fullchain, privkey)` — SSH-to-guest, drop
  `fullchain.pem`/`privkey.pem` into `/var/lib/atlas-proxy/certs/<region>/` (key
  via stdin `tee`, never in argv), reload nginx.

Each guest op records a `Task` row (`proxy-build` / `proxy-sync` /
`proxy-push-cert`). Desired state is the `Subdomain` DocType (`subdomain →
virtual_machine → address`, `region`, `active`); every proxy VM in a region gets
the full `WHERE region = R AND active` map.
