# Integration testing: boot a virtual machine and use Secure Shell

Requires a real host with root access, Kernel-based Virtual Machine, `curl`,
`jq`, `zfsutils` (zfs and zpool), and `iptables`. It cannot run in continuous
integration. Everything lives under `/tmp/metald`; bulk storage (ZFS pool image
and root file system) goes under `$METALD_BULK_DIR` (a large disk).

## Bootstrap, then serve

```sh
sudo env METALD_BULK_DIR=/path/to/big/disk scripts/dev.sh
sudo go run ./cmd/metald serve --config /tmp/metald/metald.toml
```

`scripts/dev.sh` is safe to run again. It:

- downloads Firecracker, Jailer, and the continuous integration kernel, plus a **partitionless
  Ubuntu 22.04 rootfs** (boots directly, `root=/dev/vda`),
- uses cloud-init to read Secure Shell keys from Firecracker MMDSv2,
- makes a ZFS pool and `images/ubuntu` virtual block device with an `@ready` snapshot,
- generates a Secure Shell key at `/tmp/metald/keys/id_ed25519`,
- sets up forwarding and network address translation,
- writes `/tmp/metald/metald.toml`, which you pass to `metald serve --config`.

## Secure Shell test (another terminal, while `serve` runs)

```sh
sudo test/integration/ssh-test.sh
```

Creates a virtual machine through the HTTP API with your key in `ssh_keys`, then
uses Secure Shell to log in as `root`. The key flows through the application
programming interface, MMDSv2, cloud-init, and `/root/.ssh/authorized_keys`.

## Config

metald uses built-in defaults and the optional `/var/lib/metal/metald.toml` file.
The environment does not change the daemon config. Pass another file with
`metald serve --config path`. See `cmd/metald/config.example.toml`.

Each section is named for the thing it configures.

| Configuration key | Default | Meaning |
|---|---|---|
| `metald.base_dir` | `/var/lib/metal` | holds the `machines` and `images` dirs |
| `metald.listen` | `127.0.0.1:8080` | API address; `host:port` or `unix:/path` |
| `metald.auth_token_hash` | required | lowercase SHA-256 digest of the bearer token |
| `firecracker.binary_path` | `/usr/bin/firecracker` | Firecracker binary |
| `firecracker.sockets_dir` | `/run/metal` | short symlinks to each VM API socket |
| `jailer.binary_path` | `/usr/bin/jailer` | jailer binary |
| `zfs.pool` | `metal` | ZFS pool name |

The machines and images directories are a convention below
`base_dir`, not separate keys, so one value moves all of them. The development scripts must write `metald.auth_token_hash` and API requests must send the matching bearer token.

metald does not bootstrap a host. `scripts/dev.sh` owns the development layout.
It puts everything, including the config file, under `/tmp/metald`.

These env vars drive `scripts/dev.sh` only:

| Env | Default | Meaning |
|---|---|---|
| `METALD_BULK_DIR` | `/tmp/metald` | ZFS pool image and root file system image |
| `METALD_POOL_SIZE` | auto (8–30 G) | ZFS pool image size |
| `METALD_FC_VERSION` | `v1.10.1` | Firecracker release to download |
| `METALD_POOL` | `metal` | ZFS pool name |
| `METALD_LISTEN` | `127.0.0.1:8080` | API address written into the configuration |

## Reaching a virtual machine by hand

Every guest shares `172.16.0.2` inside its own network namespace:

```sh
ip netns exec metal-<id> ssh -i /tmp/metald/keys/id_ed25519 root@172.16.0.2
```

## Debugging

- Guest serial console: the unit's journal:
  `sudo journalctl -fu metal-vm@<id>.service`
- metald logs the exact kernel cmdline for each create.
- If Secure Shell does not start but the guest booted, the metadata service helper
  could not fetch the key. Check the console for `metal-sshkey`, confirm that the
  root file system has `curl` and `wget`, and check that `169.254.169.254` is
  reachable from the guest.
