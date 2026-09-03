# Integration testing

Metal integration tests need a Linux host with root access, KVM, ZFS, iptables, `curl`, `jq`, and `sha256sum`.

## Prepare the host

```sh
sudo env METALD_BULK_DIR=/path/to/large/disk metal/scripts/dev.sh
sudo metal/dist/metald serve --config /tmp/metald/metald.toml
```

Run the script again when required. It performs these actions:

- Downloads Firecracker and Jailer.
- Downloads and imports a test image with its manifest.
- Creates the ZFS pool and parent datasets.
- Creates a Secure Shell key.
- Installs the systemd template unit.
- Enables host forwarding and NAT.
- Writes the Metal configuration and development token digest.

The default development token is `metal-development-token`. Set `METALD_AUTH_TOKEN` to use another value.

## Secure Shell test

`dev.sh` prepares the default test image. Run the test while `metald` is active:

```sh
sudo metal/test/integration/ssh-test.sh
```

Use the `METALD_IMAGE_URL`, digest, kernel, and architecture variables to test another image.

The script reserves a VM, waits for reconciliation, and connects to `172.16.0.2` in the VM namespace. It requests termination when the test ends.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `metald.base_dir` | `/var/lib/metal` | Host state directory. |
| `metald.listen` | `127.0.0.1:8080` | TCP address or `unix:/path`. |
| `metald.auth_token_hash` | none | Required lowercase SHA-256 token digest. |
| `firecracker.binary_path` | `/usr/bin/firecracker` | Firecracker binary. |
| `firecracker.sockets_dir` | `/run/metal` | Short VM socket links. |
| `jailer.binary_path` | `/usr/bin/jailer` | Jailer binary. |
| `zfs.pool` | `metal` | ZFS pool name. |

## Development environment

| Variable | Default | Meaning |
|---|---|---|
| `METALD_BULK_DIR` | `/tmp/metald` | Directory for the ZFS pool file. |
| `METALD_POOL_SIZE` | 8 GiB to 30 GiB | ZFS pool file size. |
| `METALD_FC_VERSION` | `v1.10.1` | Firecracker release. |
| `METALD_POOL` | `metal` | ZFS pool name. |
| `METALD_LISTEN` | `127.0.0.1:8080` | API address in the generated configuration. |
| `METALD_AUTH_TOKEN` | `metal-development-token` | API bearer token. |

## Manual access

```sh
sudo ip netns exec metal-<id> ssh -i /tmp/metald/keys/id_ed25519 root@172.16.0.2
```

Use `journalctl -fu metal-vm@<id>.service` to read the guest console.
