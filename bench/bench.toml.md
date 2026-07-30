# bench.toml — companion notes

Explains every key in the sibling [`bench.toml`](./bench.toml): what it does,
how our pipeline treats it, and the open problems (drift vs. current pilot). The
**problems are NOT fixed here** — they're catalogued for a follow-up session.

Schema source of truth: the pilot config parser, package `pilot`,
`config/*.py`. `_from_dict`/`_parse_*` = which keys are read + their defaults;
`validate()` = the constraints; the TOML writer = what pilot emits when it
scaffolds a bench. A fuller cross-reference lives in
[`../llm/references/bench-toml-keys.md`](../llm/references/bench-toml-keys.md).

**Two pins read this one file**, and their schemas have diverged
(`atlas/atlas/image_recipes.py`): the **site** line bakes off `frappe/pilot`
`v0.0.9-pre-alpha`, the **admin** line is still on the older fork pin. Keys below
are marked when the two disagree; the notes referring to `_parse_volume`,
`mariadb@atlas` and the ZFS pool describe the **fork** only — v0.0.9 has no volume
schema at all and reads no `[mariadb]` table.

## How the file is used

`bench.toml` is the **committed golden config**. bench-cli/pilot's own generated
toml is thrown away and this one is dropped in its place, so a baked image pins
our Frappe branch + production shape, not pilot's moving template.

The pipeline rewrites specific lines at three stages:

| Stage | Who | What it rewrites |
|-------|-----|------------------|
| **Templatize** (pre-upload) | `atlas/atlas/image_builder.py::_render_bench_toml` | `[bench].python` ← `recipe.python_version`; the **frappe** `[[apps]].branch` ← `recipe.frappe_branch` (section-aware; fails loud if a target line is missing). Proxy recipe pins nothing → uploaded verbatim. |
| **Set while baking** (build VM) | `bench/build.sh` | `[admin].password` placeholder → `openssl rand -hex 32` (idempotent, never printed). Then `bench init` reads the whole file. |
| **Set while refreshing** (per clone, admin mode) | `bench/deploy-site.py::_set_admin_domain` | `[admin].domain` → the clone's FQDN. Then the admin session verb (fork pin only — v0.0.9 ships none) makes pilot auto-write `[admin].jwt_secret`. |

Read-only consumer: `atlas/atlas/bench_image.py` awk-reads `[admin].port` from the
guest toml (section-aware) and probes `port+1` in the sanity gate.

---

## Key usage

### `[bench]`
- `name = "atlas"` — bench name; drives the systemd unit names (and, on the fork pin only, the `mariadb@atlas` instance + the default ZFS dataset leaf).
- `python = "3.14"` — **templatized** per recipe. Required (no default).
- `http_port = 8000`, `socketio_port = 9000` — pilot defaults. `socketio_port` lives here, **not** under `[redis]`.
- `socketio_backend = "node"` — default; we run the Node socket.io backend.

### `[[apps]]`
- The `frappe` app; `branch = "version-16"` is **templatized** per recipe. ERPNext is *not* declared here — build.sh clones it via `get-app --branch` + the `ERPNEXT_BRANCH` env.

### `[mariadb]` — **fork pin only**
- **⚠️ v0.0.9 does not read this table at all.** Its parser takes MariaDB from the host-level common config (`_from_dict` sets `mariadb=common.mariadb`), so every key here is an unknown field it preserves but ignores. On a site golden MariaDB is one rootless, **user-owned `pilot-mariadb.service`** shared by the host's benches, datadir + socket under `~/pilot/databases/mariadb/` (`bench/deploy-site.py::DB_SOCKET` probes that socket), bench-user unix_socket auth, no ZFS.
- On the **fork** pin: `instance = "atlas"` + `socket_path` + `data_dir = /var/lib/mysql-atlas` provision a per-bench `mariadb@atlas` (own datadir/socket/port, enabled-at-boot). Empty `instance` = shared system mariadb.
- `root_password = "mariadb-root"` — fork pin only, same reason. Where read it is fixed & SHARED across every VM from the image. Safe: each VM is single-tenant, MariaDB is reachable only on 127.0.0.1 / a local socket. Kept in step with the e2e drop-site prompts (`self_serve_site.py`, `bench_self_routing.py`).

### `[redis]`
- `cache_port = 13000`, `queue_port = 11000` — off 6379 so nothing collides with a stray system Redis. Must be distinct (validated).

### `[[workers]]`
- One group `default,short,long` × 1. Matches pilot's default group. Under `systemd`, pilot runs a single `worker-pool` for the comma-joined queues.

### `[production]`
- `process_manager = "systemd"` — load-bearing: makes bring-up a set of lingering `systemctl --user` units, which is why a snapshot clone "boots serving". `enabled` is derived from this (no explicit key needed).
- `use_companion_manager = false` — default.
- `nginx = true` — **⚠️ dead key, see Problems.**

### `[nginx]`
- Pins the proven serving shape: `http_port = 80`, `https_port = 443`, `config_dir = /etc/nginx/conf.d`, `worker_processes = "auto"`, `client_max_body_size = "50m"`. All equal current pilot defaults.

### `[gunicorn]`
- `workers = 1`, `threads = 4`, `worker_class = "sync"`, `timeout = 120`, `malloc_arena_max = 2`, `max_requests = 0`, `max_requests_jitter = 0`. Tuned for the single-tenant micro-VM. **⚠️ several now differ from pilot defaults — see Problems.**

### `[letsencrypt]`
- `email = ""` **on purpose** — TLS terminates at the edge proxy, so certbot is never attempted. A non-empty email would trigger LE validation.
- `webroot_path` — default.

### `[admin]` — the pilot management console
- `port = 8002` — internal gunicorn runs on `port+1` (read by the sanity gate).
- `enabled = true`.
- `password = "admin-password"` — placeholder, **replaced at bake** with a random secret (never surfaced). Present only because pilot won't start the admin app with none; it is *not* the tenant handoff.
- `domain = "admin.localhost"` — placeholder, **rewritten to the FQDN per clone** in admin mode. Now **required in production** and hostname-validated (the placeholder passes).
- `jwt_secret` — absent; **auto-written by pilot** on the first admin session mint. Signs the one-click `?sid=` login URL (the tenant handoff where a session verb exists — v0.0.9 dropped it, so a v0.0.9 console falls back to `password`; see `bench/deploy-site.py::_mint_admin_login_url`).

### `[volume]` — ZFS, single dataset — **fork pin only**
- **⚠️ v0.0.9 has no volume schema.** There is no `volume` section in its config parser, so all three tables are inert on a site golden: bench code and the DB datadir sit on the plain rootfs and ZFS survives only as the `zfsutils-linux` package `build.sh` installs. They stay in the file because the fork-pinned admin line still reads them; drop all three once that pin goes.
- On the **fork**: present `[volume]` without `enabled` ⇒ ZFS **on**. `pool = "bench-pool"`, `backing = "image"` (preallocated file vdev — the build VM is a single disk).
- `[volume.image].size = "15G"` — PINNED to the build VM disk budget (pilot would auto-size from host free disk; we do not adopt that). `backing="image"` requires this.
- `[volume.image].path` — absolute path to the vdev file.
- `[volume.dataset]` — a **single** dataset holds bench files + the MariaDB datadir (via bind mounts). `reservation = "1G"`, `quota = "9G"`, pinned small for the 15G vdev. `reservation ≤ quota` is validated.

---

## Problems / drift to resolve (next session)

Nothing here breaks the current bake — the file parses and validates against
both pins (v0.0.9 ignores what it does not declare rather than rejecting it).
These are drift and hygiene items:

1. **`[production].nginx = true` is a dead key.** The current pilot parser
   (`_parse_production`) no longer reads `nginx`. It's silently ignored, so our
   comment claiming it drives the nginx bring-up is now misleading. Production +
   nginx are driven by `process_manager`/`enabled` + `bench setup production`.
   → **Drop the line** and fix the comment.

2. **`[gunicorn]` overrides drifted from pilot defaults.** Pilot now defaults
   `workers=2, threads=8, worker_class="gthread", max_requests=2000,
   max_requests_jitter=500` (heap-release + threaded serving). Ours pin
   `1 / 4 / sync / 0 / 0`. This may still be the right call for a 1-worker
   micro-VM, but it's now a conscious divergence, not "= default."
   → **Decide:** keep the lean single-tenant tuning, or adopt the new
   `gthread` + request-recycling defaults.

3. **`[admin].port = 8002` drifted from the new default `7000`.** Harmless (we
   pin our own), but the internal-port math (`port+1 = 8003`) and the sanity
   gate depend on it. → **Confirm** we want 8002 vs. following pilot to 7000.

4. **`bench.db_type` is unset.** New key; pilot defaults `"mariadb"` (what we
   want) and its writer always emits it. → Optionally **set `db_type =
   "mariadb"`** explicitly for self-documentation.

5. **New tables we don't declare:** `[postgres]`, `[monitor]`, `[admin].tls`.
   All default safely (`db_type=mariadb` ⇒ postgres unused; monitor defaults
   fine; `tls=false` = our edge-terminated model). → **No action needed**;
   noted so a future reader knows the omission is intentional.

6. **History note — earlier false alarm.** A prior analysis (from a stray
   *compiled* `bench_cli/` dir, now deleted) claimed pilot had split
   `[volume.dataset]` into `[volume.benches]` + `[volume.mariadb]`. The real
   pilot uses the **single `[volume.dataset]`** we already have. **No volume
   migration is needed** — do not act on that earlier claim.
