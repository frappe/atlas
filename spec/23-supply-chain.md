# Supply chain — the external artefacts Atlas pulls

This is the inventory of every binary, OS image, source tarball, and package
Atlas downloads from outside the repo, where each is pinned, and whether the
download is checksum-verified. It is the detailed companion to operating
principle 5 ("Few dependencies") in [README.md](./README.md) — the principle
states the *shape* of the dependency set; this doc is the *manifest*.

The rule: **every external artefact has a pinned version that lives in code**,
never a `latest`/floating reference. A bump is a deliberate edit to the pin
listed here, not a silent roll-forward at install time. When you add or bump a
pin, update the matching row.

There are four independent chains, by the host they land on:

1. **The Firecracker host** (every server) — Firecracker/jailer binaries, the
   Atlas venv interpreter (`uv` + CPython), apt packages, and the guest kernel +
   rootfs images.
2. **The proxy guest** (proxy VMs only) — the nginx + Lua module stack, built
   from source inside the guest.
3. **The controller** (the Atlas Frappe host) — the TLS issuance toolchain.
4. **Build-time only** — nothing ships to a host; listed for completeness.

---

## 1. Firecracker host

Installed by [`scripts/bootstrap-server.py`](../scripts/bootstrap-server.py) and
[`scripts/sync-image.py`](../scripts/sync-image.py). See
[03-bootstrapping.md](./03-bootstrapping.md) and [08-images.md](./08-images.md).

| Artefact | Version | Source | Pinned in | Checksum? |
| --- | --- | --- | --- | --- |
| `firecracker` + `jailer` binaries (one tarball) | `v1.16.0` | `github.com/firecracker-microvm/firecracker/releases` | `atlas/atlas/doctype/server/server.py` (the bootstrap call); default in `atlas/atlas/scripts_catalog.py` | **No** — trusts the GitHub release URL |
| `uv` (creates the Atlas venv) | `0.9.30` | `astral.sh/uv/<version>/install.sh` | `UV_VERSION` in `scripts/install.sh` | No — version is in the URL |
| CPython (the Atlas venv interpreter) | `3.14.3` | fetched by `uv` (python-build-standalone) | `PY_VERSION` in `scripts/install.sh` | `uv` verifies its own download |
| Guest kernel (`vmlinux`, from a packed `vmlinuz`) | Ubuntu Noble build | `cloud-images.ubuntu.com` | the `Virtual Machine Image` row's `kernel_url` / `kernel_sha256` (e2e default in `atlas/tests/e2e/_config.py`) | **Yes** — `kernel_sha256` of the packed artefact |
| Guest rootfs (`.squashfs` → ext4) | Ubuntu Noble build | `cloud-images.ubuntu.com` | the `Virtual Machine Image` row's `rootfs_url` / `rootfs_sha256` | **Yes** — `rootfs_sha256` of the source squashfs |
| apt packages | distro versions (Ubuntu 24.04 repos) | Ubuntu archive | `PACKAGES` in `scripts/bootstrap-server.py` | apt signature chain |

The apt set is: `ca-certificates`, `curl`, `e2fsprogs`, `iproute2`, `jq`,
`lvm2`, `nftables`, `squashfs-tools`, `thin-provisioning-tools`,
`wireguard-tools` (plus `unattended-upgrades`). These are stock-archive
packages, not version-pinned — we take what 24.04 ships and let
`unattended-upgrades` roll the security pocket.

**Why the Firecracker binary isn't checksummed.** `sync-image.py` SHA256-pins
the kernel and rootfs because they are mutable upstream artefacts re-cut per
Ubuntu point release; the Firecracker tarball is an immutable, tagged GitHub
release. We currently trust the tagged URL. Adding a pinned SHA256 (published in
the release notes) would close the gap — tracked as a hardening follow-up.

### Bumping Firecracker

The version string is hardcoded (not a single shared constant), so a bump edits
**all** of these together:

- `atlas/atlas/scripts_catalog.py` — the Run Task dialog default.
- `atlas/atlas/doctype/server/server.py` — the version the `Bootstrap` button
  passes.
- `atlas/atlas/providers/fake_tasks.py` — the fake provider's reported version
  (3 spots: `firecracker_version`, `jailer_version`, the warm `host_signature`).
- `spec/03-bootstrapping.md` — the documented pin.
- the e2e use-cases that pass `FIRECRACKER_VERSION` (`run_task.py`,
  `desk_buttons.py`, `fake_provider_desk.py`).

Then re-run `Bootstrap` on every server (idempotent; the install is gated on
both `firecracker` **and** `jailer` being at the wanted version, so a re-run
rolls forward).

**Warm snapshots are version-tied.** `hostinfo.host_signature()` folds the
Firecracker version into the snapshot-restore compatibility check
([05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md)), so a
bump **invalidates every golden warm snapshot baked under the old version** —
re-bake them. (`firecracker_version` and `jailer_version` are always equal —
one tarball — but both are recorded on the `Server` row.)

---

## 2. Proxy guest (nginx + Lua stack)

Built from source **inside the proxy guest** by
[`proxy/build.sh`](../proxy/build.sh), driven by `atlas.atlas.proxy.build_proxy`.
The nginx **base binary** comes from the signed `nginx.org` apt repo; the
**dynamic modules** (which ship in no apt repo) are compiled against matching
source. See [12-proxy.md](./12-proxy.md) and [17-tcp-proxy.md](./17-tcp-proxy.md).

| Artefact | Version | Source | Checksum? |
| --- | --- | --- | --- |
| nginx (base binary + OpenSSL) | `1.30.3` (`-1~<codename>`) | `nginx.org` apt repo (signed) | apt signature chain |
| nginx source (for `make modules`) | `1.30.3` | `nginx.org/download` | **No** |
| OpenResty `luajit2` | `v2.1-20250529` | `github.com/openresty/luajit2` | **No** |
| `ngx_devel_kit` (NDK) | `0.3.4` | `github.com/vision5/ngx_devel_kit` | **No** |
| `lua-nginx-module` | `0.10.29` | `github.com/openresty/lua-nginx-module` | **No** |
| `stream-lua-nginx-module` (L4) | `v0.0.17` | `github.com/openresty/stream-lua-nginx-module` | **No** |
| `lua-resty-core` | `0.1.32` | `github.com/openresty/lua-resty-core` | **No** |
| `lua-resty-lrucache` | `0.15` | `github.com/openresty/lua-resty-lrucache` | **No** |
| `lua-cjson` | `2.1.0.14` | `github.com/openresty/lua-cjson` | **No** |
| `headers-more-nginx-module` | `0.39` | `github.com/openresty/headers-more-nginx-module` | **No** |

All version pins are constants at the top of `proxy/build.sh`. The build VM also
`apt install`s a C toolchain (`build-essential`, `libpcre2-dev`, `zlib1g-dev`,
`libssl-dev`, …) for the module compile — stock-archive, not pinned.

**These four move as a set.** nginx `1.30.3` + `lua-nginx-module 0.10.29` +
`stream-lua v0.0.17` + `lua-resty-core 0.1.32` are mutually version-locked:
`lua-resty-core`'s `base.lua` asserts an **exact** subsystem version at nginx
startup and refuses to start on a mismatch (proven by the compose gate — see
[17-tcp-proxy.md](./17-tcp-proxy.md) § Release-gate risk). Bumping any one is a
coordinated stack update rolled as a new proxy snapshot.

`build.sh`'s `fetch()` helper does **not** verify checksums — it `curl`s the
tagged GitHub tarball and trusts it. Same hardening gap as the Firecracker
binary, contained to the proxy guest.

---

## 3. Controller (TLS issuance)

The TLS layer ([13-tls.md](./13-tls.md)) runs certificate issuance **on the
Atlas controller**, not over SSH. These are host dependencies of the controller,
installed out of band (not by a Task):

| Artefact | Source | Notes |
| --- | --- | --- |
| `certbot` | OS package / pip | ACME client; DNS-01 |
| `certbot-dns-route53` | pip | the Route 53 DNS-01 plugin |
| `openssl` | OS package | reads cert dates |
| `boto3` | pip | Route 53 client; imported lazily so it never loads at module import |

Issuance fails its preflight with a clear message if these are absent
([README.md](./README.md) "First run", `tls_issuance` e2e). They are **not**
pinned by Atlas — they live in the controller's own environment.

---

## 4. Build-time only (nothing ships to a host)

Listed for completeness; these touch no production host:

- **Rust toolchain** (`1.95.0`, per the Firecracker reference's
  `rust-toolchain.toml`) — only relevant if we ever *build* Firecracker from
  source. We don't; we install the prebuilt release binary, so the MSRV does not
  constrain us.
- **Ubuntu cloud image base** — the host OS itself (Ubuntu 24.04) is provisioned
  by the cloud vendor (DigitalOcean / Scaleway), outside Atlas's download path.

---

## Hardening backlog

The two unverified download paths — the Firecracker release tarball (§1) and the
proxy module tarballs (§2) — are the open supply-chain items. Both pin an
immutable tagged release but trust the URL rather than a SHA256. Closing them
means pinning the published digests next to the version constants and verifying
on download, the same discipline `sync-image.py` already applies to the guest
kernel and rootfs.
