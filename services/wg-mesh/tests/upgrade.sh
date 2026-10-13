#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ] || [ "$(id -u)" -ne 0 ]; then
	echo "usage: sudo $0 OLD_ATLAS_WG_MESH NEW_ATLAS_WG_MESH" >&2
	exit 2
fi

# Run in a private mount namespace with its own BPF file system, so the test never touches the mesh of the
# machine that runs it.
if [ "${WG_MESH_UPGRADE_TEST:-}" != private ]; then
	exec unshare --mount --propagation private env WG_MESH_UPGRADE_TEST=private "$0" "$@"
fi
mount -t bpf bpf /sys/fs/bpf

old=$(realpath "$1")
new=$(realpath "$2")
namespace=wg-mesh-upgrade-$$
pins=/sys/fs/bpf/atlas-wg-mesh
work=$(mktemp -d)
trap 'ip netns del "$namespace" 2>/dev/null || true; rm -rf "$work"' EXIT

step() { echo "==> $*"; }

# nsenter enters only the network namespace, so every command sees the same BPF file system.
in_namespace() { nsenter --net="/run/netns/$namespace" "$@"; }

# map_directory prints the pin directory of the installed map layout.
map_directory() {
	local newest
	newest=$(find "$pins/maps" -mindepth 1 -maxdepth 1 -type d -name 'v*' 2>/dev/null | sort -V | tail -1)
	echo "${newest:-$pins}"
}

# map_counts prints the entry count of each pinned map. The rate limit maps change with traffic, so they are left out.
map_counts() {
	local directory map
	directory=$(map_directory)
	for map in "$directory"/*; do
		[ -f "$map" ] || continue
		case $(basename "$map") in discovery_limits | announcement_limits) continue ;; esac
		echo "$(basename "$map") $(bpftool -j map dump pinned "$map" | jq length)"
	done
}

step "create a host network in $namespace"
ip netns add "$namespace"
in_namespace ip link add uplink0 type veth peer name switch0
in_namespace ip link add wg0 type dummy
in_namespace ip link add vm0 type veth peer name guest0
in_namespace ip link add vm1 type veth peer name guest1
for device in uplink0 switch0 wg0 vm0 guest0 vm1 guest1; do
	in_namespace ip link set "$device" up
done
in_namespace ip addr add 192.0.2.10/24 dev uplink0
in_namespace ip -6 addr add fd00:192:0:2::10/64 dev uplink0 nodad
in_namespace ip route add default via 192.0.2.1 dev uplink0
in_namespace ip -6 addr add fdab:1::10/128 dev wg0
cat > "$work/peers.json" <<'EOF'
[{"mesh_address": "fdab:1::20", "public_address": "192.0.2.20", "private_network_mac_address": "02:00:00:00:00:20"}]
EOF

step "install the old build and add mesh state"
in_namespace "$old" configure --uplink uplink0 --wireguard wg0 --controller fdaa:1::ffff:ffff:ffff:ffff
in_namespace "$old" peers sync "$work/peers.json"
in_namespace "$old" vm sync --interface vm0 --address fdaa:1::100 --gateway \
	--prefix 2001:db8:100::/64 --route 2001:db8:200::/64=fdaa:1::100
in_namespace "$old" vm sync --interface vm1 --address fdaa:1::101 --prefix 2001:db8:101::/64
# A removed VM leaves its prefix in moved_prefixes for a while.
in_namespace "$old" vm remove --interface vm1 --address fdaa:1::101
in_namespace "$old" privileged-vm replace fdaa:1::100
# The datapath learns remote VMs from NDP. Add one as if an advertisement taught it: fdaa:1::200 on fdab:1::20.
bpftool map update pinned "$(map_directory)/remote_vms" \
	key hex fd aa 00 01 00 00 00 00 00 00 00 00 00 00 02 00 \
	value hex fd ab 00 01 00 00 00 00 00 00 00 00 00 00 00 20
map_counts > "$work/before"
cat "$work/before"

step "upgrade to the new build"
in_namespace "$new" configure --uplink uplink0 --wireguard wg0 --controller fdaa:1::ffff:ffff:ffff:ffff
map_counts > "$work/after"
if ! diff -u "$work/before" "$work/after"; then
	echo "error: the upgrade changed the map entry counts" >&2
	exit 1
fi

installed=$(in_namespace "$new" version | awk '/^Installed BPF:/ {print $3}')
embedded=$(in_namespace "$new" version | awk '/^Embedded BPF:/ {print $3}')
if [ "$installed" != "$embedded" ]; then
	echo "error: the host runs BPF $installed, not the new build $embedded" >&2
	exit 1
fi
if [ "$(map_directory)" != "$pins" ] && find "$pins" -maxdepth 1 -type f | grep -q .; then
	echo "error: the map pins of the old layout remain in $pins" >&2
	exit 1
fi

step "configure the new build again"
in_namespace "$new" configure --uplink uplink0 --wireguard wg0 --controller fdaa:1::ffff:ffff:ffff:ffff
map_counts > "$work/again"
if ! diff -u "$work/after" "$work/again"; then
	echo "error: a repeated configure changed the map entry counts" >&2
	exit 1
fi

step "the upgrade kept every map entry"
