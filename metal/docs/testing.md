# Integration testing — boot a real VM and SSH in

Requires a real host: root, KVM, `curl`, `jq`, `lvm2`, `iptables`. Cannot run in
CI. Everything lives under `$METALD_WORKDIR` (default `/tmp/metald`); bulk
storage (loop pool + rootfs) goes under `$METALD_BULK_DIR` (a big disk).

## One-line bootstrap + serve

```sh
sudo env METALD_BULK_DIR=/path/to/big/disk go run ./cmd/metald up
```

`metald up` is idempotent and:
- downloads firecracker + jailer and the firecracker CI kernel + **partitionless
  Ubuntu 22.04 rootfs** (boots directly, `root=/dev/vda`),
- bakes a **keyless MMDS shim** into the rootfs — a boot-time script that pulls
  the SSH key from `169.254.169.254` and installs it for `root`,
- makes a loop device + thin LVM pool + `base-ubuntu`,
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

| Env | Default | Meaning |
|---|---|---|
| `METALD_LISTEN` | `127.0.0.1:8080` | API address; `host:port` or `unix:/path` |
| `METALD_WORKDIR` | `/tmp/metald` | POSIX runtime (chroot, kernels, keys) |
| `METALD_BULK_DIR` | = workdir | loop pool + rootfs image (point at a big disk) |
| `METALD_LOOP_SIZE` | auto (8–30 G) | thin-pool backing size |
| `METALD_VG` | `metalvg` | LVM volume group |

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
