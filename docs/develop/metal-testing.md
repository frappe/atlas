# Integration testing

::: warning Host test setup is a work in progress
These setup steps are not fully verified. Use a disposable host and check failures with the team.
:::

Use [Metal development](metal.md) for normal package checks. Use [Metal operations](../operate/metal.md) for fault recovery.

Metal integration tests need a Linux host with root access, KVM, ZFS, iptables, `curl`, `jq`, and `sha256sum`.

## Prepare the host

```sh
sudo env METALD_BULK_DIR=/path/to/large/disk metal/scripts/dev.sh
sudo metal/dist/metald serve --config /tmp/metald/metald.toml
```

Run the script again when required. It performs these actions:

- Downloads Firecracker and Jailer.
- Builds the Atlas guest image with `build_ubuntu_server_image.sh` and imports it with its manifest. The image bakes the sshd `AuthorizedKeysCommand`, the cloud-init datasource, the network, and the metadata service, so a VM reads its per-VM SSH key from MMDS. Set `METALD_IMAGE_VERSION` to pick 22.04, 24.04 or 26.04.
- Creates the ZFS pool and parent datasets.
- Creates a Secure Shell key.
- Installs the systemd template unit.
- Enables host forwarding and NAT.
- Writes the Metal configuration and the development certificates.

The script creates a development authority in `$METALD_WORKDIR/tls` and issues a node certificate and an Atlas client certificate.
Call the API with `--cacert tls/ca.crt --cert tls/atlas.crt --key tls/atlas.key`.

## Network activity tests

These tests need root, `ip`, and Linux 6.6 or newer. Run them when no other test uses the same namespaces:

```sh
sudo -E go test -tags integration -v ./internal/network/traffic/
```

The tests attach the eBPF program to `tap0` in a temporary namespace. They verify host-to-guest IP traffic and operation when no process reads `tap0`.

## Firewall test

This test needs root, network namespaces, `iptables`, and `ip6tables`. It verifies IPv4 and IPv6 rule application and drift repair.

```sh
sudo -E go test -tags integration \
  -run TestEnsureFirewallReplacesDrift ./internal/network/
```

## Boot and idle shutdown

A full boot, Secure Shell, and idle shutdown check needs a complete environment. Use the manual steps in [Manual access](#manual-access) until the end to end environment exists.

An idle VM stops and keeps no Firecracker process. The next IPv4 or IPv6 packet starts it again. [Sleepy VMs](../compute/sleepy-vms.md) explains the full save and wake path.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `metald.base_dir` | `/var/lib/metal` | Host state directory. |
| `metald.listen` | `127.0.0.1:8080` | TCP address or `unix:/path`. |
| `firecracker.binary_path` | `/usr/bin/firecracker` | Firecracker binary. |
| `firecracker.sockets_dir` | `/run/metal` | Short VM socket links. |
| `jailer.binary_path` | `/usr/bin/jailer` | Jailer binary. |
| `zfs.pool` | `metal` | ZFS pool name. |
| `wg_mesh.enabled` | `true` | Enables WG Mesh on the host. |
| `wg_mesh.uplink` | none | Private network interface that carries Atlas NDP. Required. |
| `traffic_monitor.enabled` | `true` | Enables VM packet monitoring and idle shutdown. |

The idle timeout is per VM. A create request and a compute request carry `sleep_after_idle_seconds`.

## Development environment

| Variable | Default | Meaning |
|---|---|---|
| `METALD_BULK_DIR` | `/tmp/metald` | Directory for the ZFS pool file. |
| `METALD_WORKDIR` | `/tmp/metald` | Directory for runtime files, images, keys, and configuration. |
| `METALD_POOL_SIZE` | 8 GiB to 30 GiB | ZFS pool file size. |
| `METALD_FC_VERSION` | `v1.16.1` | Firecracker release. |
| `METALD_POOL` | `metal` | ZFS pool name. |
| `METALD_LISTEN` | `127.0.0.1:8080` | API address in the generated configuration. |
| `METALD_ATLAS_COMMON_NAME` | `atlas.metal.test` | Common name of the development Atlas client certificate. |
| `METALD_SLEEP_AFTER_IDLE_SECONDS` | `1` | The per-VM timeout that the idle shutdown test requests. |
| `METALD_IMAGE_VERSION` | `22.04` | Ubuntu version the guest image builder uses. |

## Manual access

```sh
sudo ip netns exec metal-<id> \
  ssh -i /tmp/metald/keys/id_ed25519 root@172.16.0.2
```

Read the guest console with `GET /v1/vms/{id}/console`. Use `journalctl -fu metal-vm@<id>.service` for jailer and Firecracker errors.
