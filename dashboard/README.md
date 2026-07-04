# Atlas host dashboard

A read-only visual `ls`/`cat` for one Firecracker host. It makes the host's
state legible so the operator knows what to do next on the Controller or over
SSH — it takes **no actions**. Refresh the page to see updates; there is no
realtime.

Two parts:

- **Frontend** — a Vite + Vue 3 SPA styled with [frappe-ui]'s design tokens
  (the neutral `--ink-gray` / `--surface` scale + Inter). Monochrome, flat, no
  modals. Built to static files in `dist/`.
- **Backend** — one stdlib-only Python file (`backend/server.py`). It reads the
  host's state from `/var/lib/atlas` and a handful of read-only host commands
  (`ip`, `nft`, `systemctl`, `lvs`), serves it at `/api/state`, and serves the
  static `dist/` next to it. No Frappe, no dependencies, no write routes.

## What it shows

Host facts (Firecracker / jailer / kernel / Linux / Python / arch, Atlas-managed
package versions, thin-pool usage) and one section each for: virtual machines,
images, snapshots, reserved IPs, addresses, interfaces, routes, proxy-NDP
entries, IP rules, nftables ruleset, and the Atlas systemd units (per-VM
Firecracker units, the pool oneshot, migration-forwarders, the firewall).

Fidelity notes (the current state shape, demonstrated by the generated fixtures
`mock/state-ordinary.json` / `mock/state-scale.json`, built by
`mock/generate.mjs`):

- **Firewall rules read like `nft list`.** `nft -j` carries a structured expr
  tree, not pretty text; the collector walks it and re-emits nft's human syntax
  (`ip daddr X dnat ip to Y`) instead of dumping JSON.
- **Overprovision is cap-vs-actual.** Each VM carries its committed cgroup caps
  (`cgroup_cpu_max` / `cgroup_memory_max`) AND its live actual usage
  (`mem_used_mib` / `cpu_pct`), read from the VM's cgroup. The gap is the
  "how overprovisioned is this host" story: Σ caps ≫ physical, actual ≪ caps.
- **Storage is layered.** Beyond the single flat `pool`, `storage` carries the
  full LVM stack with real byte sizes at each layer:
  `{ pvs[], vgs[], thin_pools[], volumes[] }`. The pool's fill is the thin
  pool's true `data_percent`.
- **Proxy maps are live.** `proxy_maps` is read from the proxy guest's nginx
  admin socket over SSH (`ATLAS_PROXY_GUEST` names the guest ssh dest), the same
  read the controller's reconciler makes — the routes the proxy is actually
  serving, not a stale on-disk mirror.

It also samples **live host metrics** (`metrics.series`: CPU / memory / disk IO /
network / pool) from `/proc` + `lvs` on each request, keeping a small in-memory
ring so successive refreshes accumulate a real time-series window (spec/24 § Host
level). This is a lightweight sampler, not the full on-host `metrics.db`
subsystem — it needs no store and no setup.

Every section is **best-effort**: a missing binary or path yields an empty
section, never an error — so the same backend runs on a real host and on a dev
box (where only the on-disk sections populate).

## Develop

```
npm install
npm run dev        # http://localhost:5173, /api/state mocked from mock/
```

The dev server serves checked-in fixtures at `/api/state` (see the mock plugin
in `vite.config.js`), so the UI runs with no host. Three sources are listed in
`mock/sources.json`, all the same shape, differing only in cardinality — pick one
with `?src=<id>`:

| `?src=` | source      | shape                                             |
| ------- | ----------- | ------------------------------------------------- |
| `ordinary` (default) | **Ordinary** | ~24 VMs — a realistically busy host.     |
| `scale`  | **Scale**     | ~1000 VMs — production-ish volume for paging/render cost. |
| `real`   | **Real Host** | a live `--collect` capture off a real host (f2-aditya-blr3). |

Regenerate the two synthetic fixtures with `node mock/generate.mjs` (both) or
`node mock/generate.mjs ordinary` / `scale` (one). Re-capture Real Host with
`node mock/collect-real.mjs` (SSHes the host twice; see the script header for the
`HOST` / `SSH_KEY` / `KNOWN_HOSTS` env). Every fixture is deterministic, so it
diffs cleanly. Edit any fixture and refresh — the whole app is refresh-to-update.

### Against real hosts (no install) — the SSH proxy

To iterate on the UI against **live** data from real hosts without installing
the backend on them, run the dev proxy (`backend/proxy.py`). It pushes
`server.py` to each host over `ssh` in `--collect` mode, reads the JSON the host
prints, and serves it at the same `/api/state`. Nothing lands on the host: the
collector runs from a pipe (`python3 -`) and exits.

**One command** (`setup.sh`) does the whole thing — discover the real hosts from
the bench, start the proxy + Vite, open the dashboard:

```
./setup.sh                       # reads Servers from scaleway.local, runs it all
./setup.sh root@1.2.3.4 f2-alias # skip discovery; use these ssh dests directly
```

Discovery uses only built-in Frappe calls (`bench --site … execute
frappe.get_all`), so nothing is deployed to the bench. It reaches each host as
`root@<ipv4_address>` with the Atlas SSH key + `~/.atlas/known_hosts` — exactly
how Atlas Tasks reach it. Knobs: `ATLAS_SITE` (default `scaleway.local`),
`BENCH_DIR`, `PROXY_PORT` (8080), `NO_VITE=1`, `NO_OPEN=1`.

Or drive the proxy directly:

```
python3 backend/proxy.py f2-aditya-blr3 f1-aditya-blr3   # http://localhost:8080
VITE_API_BASE=http://localhost:8080 npm run dev          # point the SPA at it
```

Each bare argument is whatever your local `ssh` understands — a `~/.ssh/config`
alias, `user@host`, or a bare hostname; `--hosts-json <file>` takes a manifest of
`{id,label,dest}` instead. Multiple hosts populate the `?src=<id>` selector
(`/api/state/sources`). The proxy caches each host for `ATLAS_PROXY_CACHE`
seconds (default 10) and derives the live metric rate series across its own
polls, so refreshing accumulates real history.

Environment: `ATLAS_PROXY_PORT` (8080), `ATLAS_PROXY_BIND` (127.0.0.1),
`ATLAS_PROXY_CACHE` (10s), `ATLAS_PROXY_SSH_TIMEOUT` (25s),
`ATLAS_PROXY_SSH_KEY`, `ATLAS_PROXY_KNOWN_HOSTS`.

## Build & ship

```
npm run build      # -> dist/
```

Copy `dist/` and `backend/server.py` onto the host, then:

```
python3 backend/server.py            # 0.0.0.0:8080, static from ./dist, root /var/lib/atlas
```

Point it elsewhere for local testing against a fixture directory:

```
ATLAS_ROOT=./backend/fixture ATLAS_DIST=./dist ATLAS_PORT=8092 python3 backend/server.py
```

Environment: `ATLAS_ROOT` (default `/var/lib/atlas`), `ATLAS_DIST` (default
`./dist` next to the server), `ATLAS_BIND` (default `0.0.0.0`), `ATLAS_PORT`
(default `8080`).

[frappe-ui]: https://ui.frappe.io
