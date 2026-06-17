# Golden bench image

The bake recipe for the **bench-preinstalled image** self-serve sites land on. A
freshly-provisioned VM from this image already has bench-cli, its uv venv, the
Frappe clone + **ERPNext (version-16)**, MariaDB + Redis on a **mandatory ZFS
pool**, nginx + supervisor configured and enabled, **a fully-created Frappe +
ERPNext site baked under the fixed name `site.local`**, and the whole production
stack **running and serving** — so a snapshot-booted clone comes up answering on
`:80` (v4 **and** v6) with no deploy step. `deploy-site.py`
([`../spec/14-self-serve.md`](../spec/14-self-serve.md)) then does only the
per-VM work: **rename** the baked `site.local` dir to the FQDN + `bench setup
nginx` to regenerate the vhost (`server_name <fqdn>` + reload) — no admin reset,
no restart (cold clones also `setup production` first), never paying the
multi-minute `bench new-site` + `install-app erpnext` per signup. The production
gunicorn is multitenant (no `--site`), so it resolves the renamed `<fqdn>` from the
`Host` header per request with no restart; the FQDN is now the on-disk name, the
proxy `Host`, and the `Site` key (Contract A), one string never transformed.
The spec slice is [`../spec/08-images.md`](../spec/08-images.md) (§ golden bench
image); the self-serve flow it feeds is
[`../spec/14-self-serve.md`](../spec/14-self-serve.md).

**ZFS is now mandatory.** The current bench-cli's `bench init` sets up a ZFS pool
unconditionally on Linux (the root-disk-zfs merge). The build VM is a single-disk
droplet, so the pool is a preallocated **file vdev** on the root filesystem
(`[volume] backing = "image"`); at init the `benches` dataset mounts at
`/root/bench-cli/benches` and the `mariadb` dataset at `/var/lib/mysql`. Because
both the bench code and the MariaDB data live on ZFS, `build.sh` orders MariaDB +
the bench supervisord `After=` the ZFS mount at boot, and runs the bench-owned
supervisord as a systemd unit (`atlas-bench.service`) so a cold boot of the
snapshot brings the stack up unattended.

**The golden image is a VM snapshot**, not a from-URL `Virtual Machine Image`.
It is built *inside* a plain Ubuntu VM (this directory's `build.sh`, run over
SSH) and the built VM is snapshotted — that snapshot is the reusable image, the
same build-in-guest + snapshot pattern the proxy uses (`proxy/build.sh` →
`Virtual Machine.snapshot`). There is no chroot bake at sync time: apt's
MariaDB/Redis postinst run normally in a real booted guest, not in a rootfs the
host never boots.

## Layout

```
bench.toml      committed bench config — pins Frappe (version-16), the
                localhost-only MariaDB root password (see its header), the
                mandatory ZFS [volume] (file-backed pool), the supervisor +
                nginx [production] config, and nginx :80 serving (http_port = 80)
build.sh        install bench-cli + `bench init` (with the ZFS pool) INSIDE the
                guest, install ERPNext, bake a `site.local` site (past the
                setup-wizard gate), `setup production`, add IPv6 listeners + mark
                the vhost `default_server` (serves any Host), wire the boot
                ordering + the supervisord systemd unit, and leave the stack
                RUNNING + serving on v4 + v6
warm.sh         arm the build VM for a WARM snapshot capture (freshen unit +
                pre-warmed production stack) — run after build.sh, before freeze
deploy-site.py  per-site deploy, run IN A CLONE over guest-SSH by
                atlas.atlas.deploy_site: RENAME the baked `site.local` dir to the
                per-VM FQDN + `bench setup nginx` (regenerate the vhost as
                `server_name <fqdn>` + v6 listener) + reload — no admin reset, no
                restart; a cold clone also runs `setup production` first
README.md       this file
```

## Serving model (how a clone answers the proxy)

The golden image carries a baked `site.local` and boots with the production stack
already running and serving it (the `atlas-bench.service` systemd unit brings the
bench-owned supervisord up after the ZFS mount + MariaDB). The production gunicorn
is **multitenant** — `frappe.app:application` runs with no fixed `--site`, so it
resolves the site from the request `Host` header **per request** (`get_site_name`),
with nothing cached at boot. The bake also marks the vhost `default_server` so a
pre-rename probe (the warm resume, before the deploy runs) answers any `Host` off
the baked `site.local`. When a `Site` is created the controller clones the snapshot
and runs `deploy-site.py` in the clone
([`../spec/14-self-serve.md`](../spec/14-self-serve.md)) to do the one per-VM thing
the image can't bake — give the site its FQDN identity on disk:

1. **Rename** `sites/site.local` → `sites/<fqdn>` (Contract A — the on-disk name
   now equals the proxy `Host` and the `Site` key). Atomic, sub-millisecond. The
   multitenant gunicorn then resolves `<fqdn>` from the `Host` per request with NO
   restart.
2. **`bench setup nginx`** (NOT `setup production`) — regenerate the vhost: it
   scans `sites/`, finds the renamed dir, emits `server_name <fqdn>` + a
   `root .../sites/<fqdn>/public` files block, then reloads nginx. Pure config-gen,
   no Frappe boot, no process restart. We add the IPv6 listener bench-cli omits and
   reload once more.
3. **Cold clone only: `setup production`** first — a freshly image-provisioned VM
   whose bench was never brought up needs the production stack started before the
   rename. A **warm clone** is already serving, so it does only steps 1–2.

There is **no `set-admin-password`** — the owner is handed the shared baked
throwaway and rotates it after first login (the per-VM reset cost a ~28s
CPU-throttled `bench frappe` boot that dominated the deploy). The slow `bench
new-site` + `install-app erpnext` are paid once at bake time, not per signup.

The edge proxy (spec/12) routes `Host: acme.blr1.frappe.dev` → `[<vm-v6>]:80`,
where this nginx answers via the renamed `server_name <fqdn>` vhost. **TLS
terminates at the edge proxy, not here** — there is no in-guest certbot. The `Site`
flips to Running only on an observed HTTP 200 from that `:80` (Contract B;
`atlas.atlas.deploy_site.wait_for_http`).

## How it's built

1. Provision a plain `ubuntu-24.04` VM (any server in the region).
2. `atlas.atlas.bench_image.build_bench(<vm>)` uploads this tree and runs
   `build.sh` over guest-SSH (mirrors `atlas.atlas.proxy.build_proxy`).
3. Stop the VM and `Virtual Machine.snapshot(...)` it.
4. Register the snapshot as the golden image (clone source for new site VMs).

See [`../atlas/tests/e2e/use_cases/bench_image.py`](../atlas/tests/e2e/use_cases/bench_image.py)
for the operator action that drives all four steps end to end.
