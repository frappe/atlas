# Golden bench image

The bake recipe for the **bench-preinstalled image** self-serve sites land on.
build.sh is the PROVEN recipe ([`../llm/references/bench-setup.md`](../llm/references/bench-setup.md))
and **nothing more**: it creates the `frappe` user, installs the ZFS kernel
module, runs bench-cli's `install.sh`, drops the committed `bench.toml`, and runs
`bench init` + `bench start`. Because `bench.toml` sets `process_manager =
"systemd"`, **bench-cli itself stands up and manages the whole stack** — the
dedicated `mariadb@atlas` instance, Redis, nginx (with v6 listeners), the ZFS
pool, and the bench processes — as lingering `systemctl --user` units that
survive reboot. So there is no hand-rolled supervisord unit, ZFS boot drop-in, or
nginx surgery here; that is all bench-cli's job now.

A freshly-provisioned VM from this image already has bench-cli, its uv venv, the
Frappe clone (+ **ERPNext (version-16)** in site mode), MariaDB + Redis, nginx +
the production stack **running and serving** — so a snapshot-booted clone comes up
answering on `:80` (v4 **and** v6) with no deploy step.

**Two modes** (build.sh's first arg → two golden snapshots):

- **`site`** (default) — bakes a fully-created Frappe + ERPNext site under the
  fixed name `site.local`. `deploy-site.py` renames `sites/site.local` → the FQDN
  + `bench setup nginx`, so the **domain maps to the site URL** (Contract A: the
  on-disk name, the proxy `Host`, and the `Site` key are one string). The
  production gunicorn is multitenant (no `--site`), resolving `<fqdn>` from the
  `Host` per request with no restart.
- **`admin`** — bakes only the bench + the admin app (no site). `deploy-site.py`
  sets `[admin].domain = <fqdn>` + `bench setup nginx`, so the **domain maps to
  the admin URL** (nginx routes the FQDN to the socket-activated admin gunicorn).

The spec slice is [`../spec/08-images.md`](../spec/08-images.md) (§ golden bench
image); the self-serve flow it feeds is
[`../spec/14-self-serve.md`](../spec/14-self-serve.md).

**ZFS.** `bench init` creates the pool + `benches`/`mariadb` datasets from the
committed `bench.toml` (`[volume]`, a preallocated **file vdev** since the build
VM is single-disk) and mounts them — the bench code and MariaDB data live on ZFS.
At the pinned bench-cli, the mere presence of a `[volume]` table enables ZFS
(`_parse_volume` defaults `enabled` to true). The Firecracker `vmlinux` ships no
ZFS module, so the **one** ZFS thing build.sh does itself is DKMS-build `zfs.ko`
against the running kernel (`zfs-dkms` + `linux-headers-$(uname -r)` + `modprobe
zfs`); the built `.ko` travels in the snapshot. (Cold-boot ZFS
auto-import/mount-ordering is not wired here — to be verified on a host.)

**The golden image is a VM snapshot**, not a from-URL `Virtual Machine Image`.
It is built *inside* a plain Ubuntu VM (this directory's `build.sh`, run over
SSH) and the built VM is snapshotted — that snapshot is the reusable image, the
same build-in-guest + snapshot pattern the proxy uses (`proxy/build.sh` →
`Virtual Machine.snapshot`). There is no chroot bake at sync time: apt's
MariaDB/Redis postinst run normally in a real booted guest, not in a rootfs the
host never boots.

## Layout

```
bench.toml      committed bench config — pins Frappe (version-16), the dedicated
                mariadb@atlas instance, the systemd [production] process manager,
                nginx :80 serving (http_port = 80), the admin app, and `[volume]`
                (ZFS on a file vdev, benches + mariadb datasets)
build.sh        the PROVEN recipe, nothing more: fix setuid bits; install the ZFS
                kernel module (DKMS); create the frappe user (+ NOPASSWD sudo);
                bench-cli install.sh (pinned ref); `bench new` + drop bench.toml;
                `bench init` + `bench start` (run AS frappe). Site mode also bakes
                a `site.local` ERPNext site. Takes `[site|admin]` as the first arg
warm.sh         arm the build VM for a WARM snapshot capture — install the freshen
                unit, pre-warm the (already-running) stack with localhost HTTP, and
                sync. Run after build.sh, before freeze. The only per-clone deploy
                work on a warm resume is `mv` + `bench setup nginx`
deploy-site.py  per-VM deploy, run IN A CLONE over guest-SSH by
                atlas.atlas.deploy_site (AS frappe): site mode RENAMEs
                `sites/site.local` → the FQDN; admin mode sets `[admin].domain`;
                both then `bench setup nginx` + reload — no admin reset, no restart
README.md       this file
```

## Serving model (how a clone answers the proxy)

The golden boots with the production stack already running and serving — bench-cli's
lingering `systemctl --user` units (enabled by `bench start` under
`process_manager = "systemd"`, with `loginctl enable-linger frappe`) bring the
bench, the dedicated `mariadb@atlas`, Redis, and nginx up at boot. The production
gunicorn is **multitenant** — `frappe.app:application` runs with no fixed `--site`,
so it resolves the site from the request `Host` header **per request**
(`get_site_name`), with nothing cached at boot. When a `Site` is created the
controller clones the snapshot and runs `deploy-site.py` in the clone
([`../spec/14-self-serve.md`](../spec/14-self-serve.md)) to do the one per-VM thing
the image can't bake — give the FQDN its identity on disk:

**Site mode:**

1. **Rename** `sites/site.local` → `sites/<fqdn>` (Contract A — the on-disk name
   now equals the proxy `Host` and the `Site` key). Atomic, sub-millisecond. The
   multitenant gunicorn then resolves `<fqdn>` from the `Host` per request with NO
   restart.
2. **`bench setup nginx`** (NOT `setup production`) — regenerate the vhost: it
   scans `sites/`, finds the renamed dir, emits `server_name <fqdn>` (on `listen
   80;` + `listen [::]:80;`, both emitted by bench-cli) + a
   `root .../sites/<fqdn>/public` files block, then reloads nginx. Pure config-gen,
   no Frappe boot, no process restart.

**Admin mode:** set `[admin].domain = <fqdn>` in bench.toml + `bench setup nginx`,
which emits the `_admin.conf` vhost (`server_name <fqdn>` → the socket-activated
admin gunicorn). No site rename.

A **cold clone** (snapshot-booted) idempotently re-asserts `bench start` first; a
**warm clone** (resumed from a memory snapshot) is already serving and skips it.

There is **no `set-admin-password`** — the owner is handed the shared baked
throwaway and rotates it after first login (the per-VM reset cost a ~28s
CPU-throttled `bench frappe` boot that dominated the deploy). The slow `bench
new-site` + `install-app erpnext` are paid once at bake time, not per signup.

The edge proxy (spec/12) routes `Host: acme.blr1.frappe.dev` → `[<vm-v6>]:80`,
where this nginx answers via the `server_name <fqdn>` vhost. **TLS terminates at
the edge proxy, not here** — there is no in-guest certbot. The `Site` flips to
Running only on an observed HTTP 200 from that `:80` (Contract B;
`atlas.atlas.deploy_site.wait_for_http`).

## How it's built

1. Provision a plain `ubuntu-24.04` VM (any server in the region).
2. `atlas.atlas.bench_image.build_bench(<vm>)` uploads this tree and runs
   `build.sh` over guest-SSH (mirrors `atlas.atlas.proxy.build_proxy`).
3. Stop the VM and `Virtual Machine.snapshot(...)` it.
4. Register the snapshot as the golden image (clone source for new site VMs).

See [`../atlas/tests/e2e/use_cases/bench_image.py`](../atlas/tests/e2e/use_cases/bench_image.py)
for the operator action that drives all four steps end to end.
