# WG Mesh operations

The `atlas-wg-mesh` command embeds the BPF object and manages one host.

It does not run a daemon.

Run all commands except `version` as root.

Pinned state is in `/sys/fs/bpf/atlas-wg-mesh`.

## Build

```sh
make build
make build VERSION=v1.2.3
make bpf
```

The built command does not need Clang or bpftool on the target host.

## Configure a host

```sh
sudo atlas-wg-mesh configure --uplink eno1.1680 --wireguard wg0 --controller fdaa:1::ffff:ffff:ffff:ffff
```

`--controller` sets the Atlas mesh address. Without it, the stored address stays.

The uplink needs IPv4, IPv6, and an Ethernet MAC address.

The WireGuard interface needs an address in `fdab::/16`.

Run the same command after you install a new binary.

The command loads the candidate programs before it replaces any hook.

If a hook replacement fails, the command restores the old programs.

The command rejects an incompatible map layout and keeps the active release.

## Synchronize peers

Use multicast Neighbor Discovery Protocol (NDP) on a shared host network:

```sh
sudo atlas-wg-mesh peers sync /var/lib/metal/wireguard-peers.json
```

Use IPv4-wrapped unicast NDP:

```sh
sudo atlas-wg-mesh peers sync /var/lib/metal/wireguard-peers.json --unicast
```

The command validates the complete file before it changes the peer map.

The command attaches or detaches the egress hook after it writes the peer map.

## Synchronize a virtual machine

Use one command for the complete virtual machine state:

```sh
sudo atlas-wg-mesh vm sync \
  --interface vh-100001 \
  --address fdaa:1:10:20::5 \
  --mtu 1380
```

Add gateway state when the virtual machine needs it:

```sh
sudo atlas-wg-mesh vm sync \
  --interface vh-100002 \
  --address fdaa:1:0:0::1 \
  --mtu 1380 \
  --gateway \
  --prefix 2001:db8:100::/64 \
  --route 2000::/3=fdaa:1:0:0::2
```

Each `--prefix` gives the virtual machine ownership of one public prefix.

Each `--route` has the form `DESTINATION=GATEWAY`.

The command replaces all prefixes and routes for that virtual machine.

Omit `--gateway`, `--prefix`, or `--route` to remove old state of that type.

List or remove local virtual machines:

```sh
sudo atlas-wg-mesh vm list
sudo atlas-wg-mesh vm list --json
sudo atlas-wg-mesh vm remove --interface vh-100001 --address fdaa:1:10:20::5
```

The remove command also removes the gateway state owned by the virtual machine.

## Synchronize privileged virtual machines

A privileged virtual machine is a listed tenant-0 virtual machine.

It can communicate with all tenants.

Replace the complete set:

```sh
sudo atlas-wg-mesh privileged-vm replace \
  fdaa:1:0:0::1 \
  fdaa:1:0:0::2
```

List or remove the complete set:

```sh
sudo atlas-wg-mesh privileged-vm list
sudo atlas-wg-mesh privileged-vm list --json
sudo atlas-wg-mesh privileged-vm clear
```

## Check a host

```sh
sudo atlas-wg-mesh status
sudo atlas-wg-mesh status --json
sudo atlas-wg-mesh inspect fdaa:1:10:20::5
sudo atlas-wg-mesh inspect fdaa:1:10:20::5 --json
atlas-wg-mesh version
```

`status` shows the interfaces, NDP mode, BPF hash, and map counts.

`inspect` shows local state, a learned remote host, or an unknown location.

An unknown location is normal before a local virtual machine contacts the address.

## Remove the mesh

```sh
sudo atlas-wg-mesh reset
sudo atlas-wg-mesh reset --force
```

`reset` refuses to run while local virtual machines remain.

`reset --force` also removes their hooks and proxy NDP entries.
