# Integration testing — boot a real VM and SSH in

Requires a real host: root, KVM, `curl`, `jq`, `zfsutils` (zfs/zpool),
`iptables`. Cannot run in CI. Everything lives under `$METALD_WORKDIR` (default
`/tmp/metald`); bulk storage (zfs pool image + rootfs) goes under
`$METALD_BULK_DIR` (a big disk).

## One-line bootstrap + serve

```sh
sudo env METALD_BULK_DIR=/path/to/big/disk go run ./cmd/metald up
```

`metald up` is idempotent and:
- downloads firecracker + jailer and the firecracker CI kernel + **partitionless
  Ubuntu 22.04 rootfs** (boots directly, `root=/dev/vda`),
- bakes a **keyless MMDS shim** into the rootfs — a boot-time script that pulls
  the SSH key from `169.254.169.254` and installs it for `root`,
- makes a ZFS pool + `base/ubuntu` zvol (snapshot `@ready`),
- generates an ssh key at `$METALD_WORKDIR/keys/id_ed25519`,
- sets up forwarding + NAT,
- serves the API on `$METALD_LISTEN` (default `127.0.0.1:8080`).

## SSH test (another terminal, while `up` runs)

```sh
sudo test/integration/ssh-test.sh
```

Creates a VM via the HTTP API with your key in `ssh_keys`, then SSHes in as
`root`. The key flows entirely through the API:
`POST /vms {ssh_keys} → MMDS → guest shim → /root/.ssh/authorized_keys`.

## Config

metald resolves its config in three layers, lowest to highest: built-in
defaults, an optional `config.toml`, then the environment. A set `METALD_*` env
var wins over the file. Pass the file with `metald serve --config path`, or put a
`config.toml` in the working dir. See `cmd/metald/config.example.toml`.

These keys map to the daemon config file and env:

| Env | `config.toml` key | Default | Meaning |
|---|---|---|---|
| `METALD_LISTEN` | `listen` | `127.0.0.1:8080` | API address; `host:port` or `unix:/path` |
| `METALD_CHROOT_BASE` | `firecracker.chroot_base` | `/srv/jailer` | jailer chroot base dir |
| `METALD_VAR_DIR` | `firecracker.var_dir` | `/var/lib/metal/vms` | per-VM state dir |
| `METALD_JAILER` | `firecracker.jailer_bin` | `/usr/bin/jailer` | jailer binary |
| `METALD_FIRECRACKER` | `firecracker.firecracker_bin` | `/usr/bin/firecracker` | firecracker binary |
| `METALD_POOL` | `storage.pool` | `metal` | ZFS pool name |
| `METALD_KERNEL_DIR` | `storage.kernel_dir` | `/var/lib/metal/kernels` | guest kernel dir |

These env vars drive the `up` bootstrap script only (not the config file):

| Env | Default | Meaning |
|---|---|---|
| `METALD_WORKDIR` | `/tmp/metald` | POSIX runtime (chroot, kernels, keys) |
| `METALD_BULK_DIR` | = workdir | zfs pool image + rootfs image (point at a big disk) |
| `METALD_POOL_SIZE` | auto (8–30 G) | zfs pool image size |

## Reaching a VM by hand

Every guest shares `172.16.0.2` inside its own netns:

```sh
ip netns exec metal-<id> ssh -i $METALD_WORKDIR/keys/id_ed25519 root@172.16.0.2
```

## Debugging

- Guest serial console → the unit's journal:
  `sudo journalctl -fu metal-vm@<id>.service`
- metald logs the exact kernel cmdline for each create.
- If SSH never comes up but the guest booted: the MMDS shim couldn't fetch the
  key — check the console for `metal-sshkey`, confirm the rootfs has `curl`/`wget`,
  and that `169.254.169.254` is reachable from the guest.
