# The TCP proxy

A reload-free TCP forwarder that lets a tenant expose a raw service — SSH,
MariaDB, anything that speaks TCP — on a VM that has **no public IPv4**. The
tenant connects to the proxy fleet's shared reserved IPv4 on a **unique port**;
the proxy forwards that connection to the tenant VM's `/128` on the real service
port over the v6 internet. The port→backend map changes constantly and must
update **without reloading nginx**, exactly like the HTTP subdomain map. Atlas is
the source of truth and reconciles each proxy's live map over SSH-to-the-guest.

This is the L4 sibling of the HTTP reverse proxy ([12-proxy.md](./12-proxy.md)).
It runs **on the same proxy VMs**, in the **same nginx process**, reconciled by
**the same controller** over the same SSH-to-the-guest path. Read 12-proxy first;
this chapter only states where TCP differs and why.

## Why this is not "the HTTP proxy, but for TCP"

The HTTP proxy is reload-free because the routing key travels **inside the
request**: the `Host` header. One `:443 default_server` block fronts every site;
`access_by_lua` reads `ngx.var.host`, looks it up in the `sites` shared dict, and
`proxy_pass $vm_upstream` dials the answer ([router.lua](../proxy/lua/router.lua)).
Adding a site is a dict write; nginx config never changes.

Raw TCP carries **no application-layer routing key**. A `mysql` client opening a
socket sends MySQL protocol bytes, not "I want tenant X". The only key the proxy
has is **the local port the connection landed on**. That single fact forces the
whole design and splits it cleanly in two:

| What changes | Reload? | How |
| --- | --- | --- |
| **Which backend a port forwards to** | **No reload** — the dynamic part | `stream {}` + `ngx_stream_lua_module`: `preread_by_lua` reads `$server_port`, looks it up in a `lua_shared_dict`, `balancer_by_lua` dials it |
| **Whether a port is *listening* at all** | **Reload (avoided by design)** | nginx must `listen` on a port for the kernel to accept; a new bind needs `nginx -s reload` |

The trick that makes the second row a non-issue: **pre-open a fixed port range
once**, baked into the config (`listen 10000-19999;`). Allocating a port to a
tenant is then a pure dict write — zero reload, identical in cost to creating a
`Subdomain`. nginx reloads only if the operator *grows the range*, which is a
deliberate stack change rolled as a new proxy snapshot — the same model as
bumping the nginx version.

## The shape

- **No new VM, no new image tier.** The TCP forwarder is more `stream {}`
  config + Lua loaded into the **same** proxy nginx (the stream-lua `.so` is one
  more dynamic module on the stock apt binary). A proxy VM serves HTTP on
  `:443`/`:80` **and** TCP on its pre-opened port range, from one process. The
  proxy is still an ordinary operator-owned `Virtual Machine` marked `is_proxy`
  ([12-proxy.md](./12-proxy.md)).
- **Ingress is the proxy's reserved IPv4.** A tenant with no v4 reaches the
  proxy's attached `public_ipv4` (the inbound-v4 primitive,
  [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)) — the same
  reserved IP the proxy already carries for `:443`. The TCP ports ride that IP.
  The v6 listener is the proxy's `/128` for tenants who *do* have v6 but still
  want the proxy to multiplex (rare; v4 is the point).
- **The shared port pool is the scarce resource.** Each proxy holds the whole
  port→backend map and listens on the whole pool. `listen 10000-19999;`
  is **one listen socket per port** under the hood (nginx 1.15.10+ port-range
  syntax is sugar for 10000 sockets, 10000 fds per worker), so the pool size is a
  real resource decision, not free — every port in the range is `bind()`+`listen()`'d
  at startup and re-opened on every reload, and carries a kernel socket struct per
  worker. The proxy's systemd drop-in already raises `LimitNOFILE` to 1048576
  ([nginx.service.d/atlas.conf](../proxy/guest/nginx.service.d/atlas.conf)); a
  10000-port pool per worker is comfortably within that. **`worker_connections` IS affected**:
  nginx counts every listening socket against it, not just live connections, so it
  must clear the listener count (≈20000 for the v4+v6 pool, plus the http listeners
  and the two admin sockets) with headroom for real traffic — set to `65536`. (The
  compose release gate caught this: an earlier draft of this chapter wrongly said
  listeners don't count, and `nginx -t` failed "`worker_connections are not enough
  for 20006 listening sockets`" — exactly the kind of host fact the gate exists to
  surface.) The pool defaults to `10000-19999` (not starting at `1024`)
  to leave the registered-service ports below it free for any future co-located
  proxy sidecar; the south hop to the guest is over IPv6 so the v4 listen pool
  never contends with anything.
- **The live map** is a `lua_shared_dict ports` inside the **`stream {}`**
  block, dumped to `stream-map.json`, read only at start. A change is an atomic
  dict write — **zero reload**, exactly like the HTTP `sites` dict.

## Desired state: the Port Mapping DocType

One [`Port Mapping`](./02-doctypes.md#port-mapping) row per exposed port,
standalone and linked exactly the way [`Subdomain`](./02-doctypes.md#subdomain)
is — **not** a child grid on a proxy, because every proxy holds the whole map.
Atlas is single-region, so there is no region field on the row; every active
mapping belongs to the one region by definition.

| Field | Meaning |
| --- | --- |
| `public_port` | The proxy-side port the tenant connects to. **Allocated by Atlas** on insert (first free in the pool), unique fleet-wide, read-only. The routing key. The row's name is `<protocol>-<public_port>` (`autoname format:{protocol}-{public_port}`). |
| `virtual_machine` | The tenant VM this port forwards to. Immutable. |
| `address` | The VM's public IPv6 `/128`, **denormalized** on save (copied from the VM's `ipv6_address`), so the desired map is one query with no join — the same trick `Subdomain.address` uses. The proxy dials a literal; it never resolves a VM. |
| `target_port` | The service port **inside the guest** (22 for SSH, 3306 for MariaDB). Immutable. |
| `protocol` | A label only (`ssh` / `mariadb` / `tcp`) for the operator and the future dashboard — the forwarder is protocol-agnostic L4. |
| `active` | Inactive rows are excluded from the served map (history kept), same as `Subdomain.active`. |

The desired map is `port_map()` =
`{ "<public_port>": "[<address>]:<target_port>" }` for every active mapping.
The value is a ready-to-dial `host:port` string (bracketed v6
literal) so the guest does no formatting. Every proxy VM serves that same full
map.

`public_port` allocation is the one piece with no `Subdomain` analogue: on
insert, the controller picks the lowest port in `Atlas Settings.tcp_port_pool`
(default `10000-19999`) not already taken by an active *or inactive* mapping
(an inactive row still owns its port — toggling it back on must not
collide), under the same row-lock idiom the rest of Atlas uses. Pool exhaustion
is a typed throw, not a silent wrap.

## Control plane: Atlas → guest (the second dict)

The decisive constraint, confirmed against the OpenResty docs: **`http{}` and
`stream{}` `lua_shared_dict`s are separate address spaces.** A dict declared in
`http{}` is invisible to stream Lua and vice versa (OpenResty tracks
cross-subsystem sharing as an unimplemented feature). So the HTTP admin API
(`/run/nginx/admin.sock`, which writes the `sites` dict) **cannot** write
the TCP `ports` dict. The TCP map needs its **own** admin surface inside
`stream{}`.

That surface is a **second admin server in the `stream{}` block**, on its own
unix socket `/run/nginx/stream-admin.sock`, served by `content_by_lua`
(`ngx_stream_lua_module` supports a unix `listen` + `content_by_lua` request/
response server — verified). It speaks a **minimal line protocol** rather than
HTTP, because the stream content phase reads raw bytes off `ngx.req.socket()`,
not a parsed HTTP request — and a 3-verb line protocol is less code than an
HTTP parser and just as auditable:

```
GET\n                       -> the whole map as canonical JSON
SYNC\n<canonical-json>\n     -> bulk declarative replace (add + remove), then dump
DUMP\n                      -> force an immediate persist
```

`atlas/atlas/tcp_proxy.py` is the controller side. It is the **exact mirror** of
[`proxy.py`](../atlas/atlas/proxy.py), reusing every idiom:

- **`canonical_json(map)`** — the **same** `json.dumps(sort_keys=True,
  indent=2) + "\n"` helper, reused verbatim from `proxy.py`, byte-identical to
  the guest's `stream-persist.lua` output, so the "in sync?" check is a string
  compare.
- **`reconcile_proxy(vm)` / `reconcile_proxies()`** — for each proxy VM
  (enumerated by reusing `proxy._proxy_vms()`), read the live map (`GET\n` over
  the stream-admin socket via
  SSH-to-guest), byte-compare against the canonical desired map, and `SYNC\n…` the
  full map on drift. Idempotent, self-healing, rebuild-safe; one unreachable
  proxy is a failed Task and skipped, never wedging the loop — same guarantees as
  the HTTP reconcile.
- **`build`** — there is **no separate build verb**. The TCP `stream{}` config +
  Lua are part of the **same** `proxy/` tree and `build.sh`, installed by the
  same `build_proxy(vm)`. A proxy is HTTP+TCP from one build.

There is no per-mapping cert push: TCP forwarding is **L4 passthrough** — the
proxy never terminates TLS for these ports. SSH does its own crypto; MariaDB's
TLS (if the tenant enables it) terminates **at the guest**, end-to-end through
the forwarder. The proxy is a dumb pipe, which is exactly the security property
you want for a tenant's database port.

Each guest op is recorded as a `Task` (`script` = `tcp-proxy-sync`, with the
proxy VM), the same audit-row shape as `proxy-sync`.

`Port Mapping`'s lifecycle hooks mirror `Subdomain`'s exactly: `after_insert`,
`active`-toggle `on_update`, and `on_trash` each enqueue a **deduplicated**
`port_mapping.tcp_reconcile` job (one reconcile no matter how many mappings
changed — the same `deduplicate=True` + a constant `job_id`
(`tcp_reconcile_ports`) that stopped the
4000-redundant-reconcile pileup for subdomains, whose own constant is
`auto_reconcile_subdomains` enqueuing `subdomain.auto_reconcile`).

## The request path in the guest

```
stream {
    log_format stream_basic '...';      # stream{} has NO built-in log format
    lua_shared_dict ports 16m;          # the live map (stream-only address space)

    # No region load: TCP routes purely by the landed port, never by hostname, so
    # unlike the http side there is no ".<region>.frappe.dev" suffix to strip.
    init_worker_by_lua_block { require("stream_persist").load() }

    # The pre-opened pool. One server block, the whole range.
    server {
        listen 10000-19999;             # v4 lands here via the reserved-IP 1:1 NAT
        listen [::]:10000-19999;        # v6 lands here on the proxy /128
        preread_by_lua_file  /etc/nginx/lua/stream_router.lua;
        set $tcp_upstream "";
        proxy_pass $tcp_upstream;       # $tcp_upstream set in preread
    }

    # Admin: unix socket only, never TCP. File perms are the gate. (mirrors http)
    server {
        listen unix:/run/nginx/stream-admin.sock;
        content_by_lua_file /etc/nginx/lua/stream_admin.lua;
    }
}
```

`stream_router.lua` runs in `preread`: read `ngx.var.server_port`, look it up in
`ports`, and on a hit set `ngx.var.tcp_upstream` to the dialable `host:port`
value (the server block's `proxy_pass $tcp_upstream` then dials it — no resolver
and no `upstream{}` block, because `address` is always an IP literal so
`proxy_pass` treats it as a literal, never a name); on a miss, close the
connection (there is no branded-404 for raw TCP — `ngx.exit(ngx.ERROR)` drops
it). The guest never resolves DNS.

`stream_persist.lua` is the byte-for-byte twin of
[persist.lua](../proxy/lua/persist.lua), pointed at `stream-map.json` and the
`ports` dict.

## Build: one stack, one new dynamic module

The proxy is **stock nginx from the nginx.org apt repo + the Lua/headers-more
modules compiled as dynamic `.so`s** against it (see [12-proxy.md](./12-proxy.md)
and [`build.sh`](../proxy/build.sh)); the TCP forwarder rides the same binary —
`--with-stream` and `--with-stream_ssl_preread_module` are already in the stock
nginx.org package. [`build.sh`](../proxy/build.sh) gains, as a deliberate
pinned-version stack bump rolled into a new proxy snapshot:

- `--add-dynamic-module=…/stream-lua-nginx-module` on the module `./configure`
  line — **a separate module** from `lua-nginx-module`; both are built as dynamic
  `.so`s and `load_module`'d by the apt binary in `nginx.conf`
  (`ngx_stream_lua_module.so` alongside `ngx_http_lua_module.so`). It depends on
  the same NDK, luajit2, and **lua-resty-core** already built for the HTTP Lua
  side.
- the stdlib-only `stream-admin` line-protocol client installed on `PATH`
  (`/usr/local/bin/stream-admin`), plus `python3` to run it — the L4 analogue of
  `curl --unix-socket` for the http admin.

Everything else — luajit2, NDK, lua-resty-core/lrucache, lua-cjson, the rpath —
is already present for the HTTP path and is reused unchanged. The three stream
Lua files install into `/etc/nginx/lua` next to the http trio; the stream map
persists to `/var/lib/nginx/stream-map.json`.

One config gotcha: `stream{}` is a **separate Lua subsystem** from `http{}`, so
`lua_package_path` *and* `lua_package_cpath` set in `http{}` do **not** carry
into it. Both must be re-declared inside the `stream{}` block — the cpath
especially, since `stream_admin.lua`/`stream_persist.lua` `require("cjson.safe")`
and the stock-apt-nginx default cpath isn't guaranteed to include the compiled
`cjson.so`. Omit it and the first `stream-admin` call crashes loading cjson.

## Host-bound facts — extend the `proxy_vm` e2e

The TCP path is proven on the **same** real droplet as the HTTP proxy, added to
[`proxy_vm.py`](../atlas/tests/e2e/use_cases/proxy_vm.py) (not a new use case —
no new operator button; a `Port Mapping` is the same shape as a `Subdomain`).
The host facts only a droplet can prove:

- **TCP forward end-to-end over the reserved v4** — create a `Port Mapping` for a
  stand-in VM's `:22`, reconcile, then from **off the droplet** (the controller,
  over the public v4 internet) open a TCP connection to `<reserved-v4>:<public_port>`
  and reach the guest's SSH banner through the forwarder. This is the L4 mirror of
  the `:443` reachability fact, and it proves the reserved-IP 1:1 NAT carries
  arbitrary ports, not just `:443` (it was previously proven for `:22` direct and
  `:443` via the proxy — never for a *forwarded* arbitrary port).
- **No-reload remap** — repoint an existing `public_port` at a different backend
  via `SYNC`, assert the new backend serves with **no nginx reload** (check the
  master PID is unchanged), exactly the HTTP remap-no-reload gate.
- **map sync byte-equality** — read the live map back over the stream-admin
  socket and assert it equals the canonical desired map byte-for-byte.

The compose release gate ([proxy/test/](../proxy/test)) gains a TCP case: bring
up a raw-TCP fake upstream alongside the HTTP ones, `SYNC` a port→upstream map
through the stream-admin socket, connect to a published proxy port, assert the
bytes round-trip, then remap and assert no reload.

## Why these decisions

1. **Shared proxy IP + unique port, not one reserved IP per tenant.** The chosen
   model multiplexes thousands of tenants behind the proxy fleet's existing
   reserved IPv4 — no new billable v4 per tenant, no dependency on the unbuilt
   "general tenant inbound v4" roadmap item ([09-roadmap.md](./09-roadmap.md)).
   The cost is a less-pretty endpoint (`v4:23306` instead of `v4:3306`) and a
   bounded port pool. The rejected per-tenant-reserved-IP model gives the real
   port but burns a billable IP each and needs the v4-attach primitive opened to
   tenants first — strictly more expensive and more work.
2. **Pre-open a fixed range; never reload per mapping.** The one thing Lua cannot
   do is make nginx bind a new port. Pre-binding `10000-19999` turns every
   allocation into a dict write. Growing the pool is the only reload, and it is a
   rare, deliberate snapshot roll — the same cadence as any stack change. The
   rejected "reload on every new port" model would make port creation as slow and
   risky as an nginx restart and forfeit the whole reload-free property the HTTP
   side worked to get.
3. **A second admin surface in `stream{}`, not a bridge from the http admin.**
   Forced by the http/stream dict isolation: the http admin physically cannot
   write the stream dict. Two sockets, two dicts, one reconcile pattern. The
   rejected alternatives — a file the stream side re-reads per connection, or an
   out-of-band IPC bridge — are slower and add a moving part for no gain.
4. **L4 passthrough, no TLS termination.** The proxy is a dumb pipe for these
   ports. SSH and MariaDB carry their own transport security end-to-end to the
   guest; terminating TLS at the proxy would put a tenant's DB credentials
   through the proxy's trust boundary for no benefit. (TLS-SNI routing on `:443`
   was considered and rejected: it needs the client to speak SNI, which plain
   `ssh`/`mysql` clients do not.)
5. **Reuse the HTTP proxy's machinery wholesale.** Same VM, same nginx process,
   same build, same controller reconcile shape, same Task audit rows, same
   DocType idioms (standalone+linked, denormalized address, immutable key,
   deduplicated reconcile). The TCP proxy adds a port pool and a second
   dict; it invents no new infrastructure.

## Accepted limitations

- **The forward→guest hop is over the public IPv6 internet** — identical to the
  HTTP south hop ([12-proxy.md](./12-proxy.md)). The guest's service port (`:22`,
  `:3306`) is reachable by anyone on the v6 internet, not just the proxy, until
  the south-side per-VM firewall ([09-roadmap.md](./09-roadmap.md)) scopes it.
  For a database port this is a sharper exposure than `:80` — the south-side
  firewall is the matching release gate before TCP exposure ships to tenants.
- **Bounded port pool.** `10000-19999` = 10000 concurrent mappings fleet-wide.
  Sized for the iteration; growing it is a snapshot roll. The next block,
  `20000-60000`, is reserved as the documented growth path (≈50000 more ports,
  clear of the registered-service ports below `10000`) — it is **not** pre-opened,
  to keep startup, reload, and per-worker socket memory proportional to the pool
  actually in use. The fleet only grows into it if the `10000-19999` pool
  approaches exhaustion, and the grow is the same deliberate snapshot roll as any
  stack change.
- **No graceful-reload guarantee across a pool grow for *open* sessions.** nginx
  reload is graceful (old workers drain), but a long-lived SSH/DB session pins an
  old worker until it closes or `worker_shutdown_timeout` fires. Pool grows are
  rare and operator-scheduled, so this is acceptable; left unset, sessions are
  never force-dropped.
- **One reserved IP per host, for now** — inherited from the proxy's reserved-IP
  primitive ([12-proxy.md](./12-proxy.md)).

## Release-gate risk — RETIRED by the compose gate

`ngx_stream_lua_module`'s version is **not free to pick**, and the compose gate
proved why. The pinned `lua-resty-core` (0.1.32, already required by the HTTP Lua
side) asserts an **exact** subsystem version at nginx startup: its `base.lua`
requires `ngx_stream_lua_module == 0.0.17` **and** `ngx_http_lua_module ==
0.10.29` — not `>=`. So the stream tag is forced to **`v0.0.17`**, the one that
matches the already-pinned resty-core + lua-nginx-module 0.10.29 set. A newer
stream-lua (e.g. `0.0.19rc4`, the newest published tag) **compiles fine** against
nginx 1.30.2 but nginx then ALERTs `failed to load the 'resty.core' module …
ngx_stream_lua_module 0.0.17 required` and refuses to start.

The compose gate caught this directly — it is the proof the four-way set (nginx
**1.30.2** + lua-nginx-module **0.10.29** + stream-lua **0.0.17** + lua-resty-core
**0.1.32**) builds and runs together; the `proxy_vm` e2e is the second proof, on a
real kernel. Bumping any one of the four is a coordinated stack update rolled as a
new proxy snapshot, the same discipline as every other pin. The documented
fallback if a future nginx bump breaks the set — drop to a matched OpenResty
bundle for the whole stack — was not needed: the matched vanilla pins work.

The gate also corrected two of this chapter's earlier assumptions, both now fixed
in [`conf/nginx.conf`](../proxy/conf/nginx.conf): the `stream{}` block needs an
explicit `log_format` (it has no built-in default like `http{}`), and
`worker_connections` **does** count listening sockets, so it must clear the ~20000
pool listeners (raised to `65536`).
