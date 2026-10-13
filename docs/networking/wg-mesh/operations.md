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

`metald` runs this command on each start. A [Metald Update](../../region/hosts-and-providers.md#roll-out-a-build) installs a new binary.

The command loads the candidate programs before it replaces any hook.

If a hook replacement fails, the command restores the old programs.

Each build has a map layout version, `map_layout_version` in `bpf/maps.h`. Version 0 pins its maps in `/sys/fs/bpf/atlas-wg-mesh/`. A later version pins them in `/sys/fs/bpf/atlas-wg-mesh/maps/v<version>/`. `atlas-wg-mesh version` shows the layout of a build.

When the host runs an older layout, the command moves the maps to the new layout before it replaces any hook:

1. An unchanged map gets a second pin. The old and new programs share it, so no entry is lost.
2. A changed map gets a new map, which the patches in `cli/patches` fill from the old map.
3. After the hook switch, the command copies the changed maps again. This adds the entries that the old programs learned during the switch.
4. The command removes the pins of the old layout.

If a step fails, the old programs keep the old maps. The command refuses a build with an older layout than the host runs, so a failed upgrade keeps the running release.

A build from before the layout versions recreates its maps empty when it replaces a newer layout. Local VMs come back from their interfaces, peers from the next `metald` sync, and remote VMs from NDP.

## Change a map

A map keeps its type, key, value, maximum entries, and flags for the life of its pin. To change one of them:

1. Change the map in `bpf/maps.h`, and increase `map_layout_version` by one.
2. Add `cli/patches/v<version>.go`, and list the patch at the end of `Patches`. The patch names each changed map and converts one entry. A nil key drops the entry.

```go
// v1.go: remote_vms stores the time of the last advertisement after the host address.
var addAdvertisementTime = Patch{
	Maps: []string{"remote_vms"},
	Convert: func(_ string, key, value []byte) ([]byte, []byte, error) {
		return key, append(value, make([]byte, 8)...), nil
	},
}
```

3. Never change a released patch. A host that skips versions runs every patch in order.

`upgrade-metald.sh` installs the new `atlas-wg-mesh` before it restarts `metald`. A VM command that the old `metald` runs in that moment fails, because the new layout has no pins yet. The `metald` reconciler applies the VM network again within 5 seconds.

If a migration stops before it commits, the next `configure` builds the new layout again from the old maps.

CI fails a pull request that changes a map without a patch that lists it. A new map or a removed map needs no patch. CI also installs the build of the base branch on a test host, adds state, upgrades it to the pull request build, and checks that every map keeps its entries. Run the same check on a machine with `bpftool` and `jq`:

```sh
sudo tests/upgrade.sh <base-branch-atlas-wg-mesh> <new-atlas-wg-mesh>
```

The check runs in its own network namespace and BPF file system, so it does not change the mesh of the machine.

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
