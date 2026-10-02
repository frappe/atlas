#!/usr/bin/env bash
# Install the Atlas IPv6 router on Ubuntu 24.04. You can run this script again.

set -euo pipefail

: "${REGION_ID:?REGION_ID is required}"
: "${PUBLIC_IPV6_PREFIX:?PUBLIC_IPV6_PREFIX is required}"

source_dir=$(cd "$(dirname "$0")" && pwd)
state_dir=/opt/atlas/ipv6-router
modules=(sch_ingress cls_bpf nf_tables)

if [ "$(id -u)" -ne 0 ]; then
	echo "setup.sh must run as root" >&2
	exit 1
fi

step() { echo "==> $*"; }


step "install packages"
export DEBIAN_FRONTEND=noninteractive
# needrestart would restart ssh and systemd-networkd during the install.
export NEEDRESTART_SUSPEND=1
apt-get update -qq
# Atlas boots the kernel from outside the root file system, so the image has no modules for it.
apt-get install -y -qq clang llvm libbpf-dev linux-libc-dev iproute2 nftables python3 "linux-modules-$(uname -r)"


step "load kernel modules"
modprobe -a "${modules[@]}"
printf '%s\n' "${modules[@]}" > /etc/modules-load.d/atlas-ipv6-router.conf


step "write the configuration"
install -d -m 0755 "$state_dir"

# The public host part needs 48 bits, so the prefix can be at most /80.
read -r mesh_region public_prefix < <(python3 - "$REGION_ID" "$PUBLIC_IPV6_PREFIX" "$state_dir/config.h" <<'PYTHON'
import ipaddress
import sys

try:
	region_id = int(sys.argv[1])
except ValueError:
	sys.exit(f"REGION_ID {sys.argv[1]} is not an integer")
try:
	network = ipaddress.IPv6Network(sys.argv[2], strict=True)
except ValueError:
	sys.exit(f"PUBLIC_IPV6_PREFIX {sys.argv[2]} is not a canonical IPv6 prefix")
if not 0 <= region_id <= 0xFFFF:
	sys.exit(f"REGION_ID {region_id} is not a 16-bit number")
if not network.subnet_of(ipaddress.IPv6Network("2000::/3")):
	sys.exit(f"PUBLIC_IPV6_PREFIX {network} is outside 2000::/3")
if network.prefixlen > 80:
	sys.exit(f"PUBLIC_IPV6_PREFIX {network} is longer than /80")

words = {
	"PUBLIC_HIGH": int(network.network_address) >> 64,
	"PUBLIC_LOW": int(network.network_address) & (2**64 - 1),
	"PUBLIC_HIGH_MASK": int(network.netmask) >> 64,
	"PUBLIC_LOW_MASK": int(network.netmask) & (2**64 - 1),
}
with open(sys.argv[3], "w") as config:
	config.write(f"#define REGION_ID {region_id}ULL\n")
	for name, value in words.items():
		config.write(f"#define {name} {value:#x}ULL\n")

print(f"fdaa:{region_id:x}::/32", network)
PYTHON
)
sed -e "s|@MESH_REGION@|$mesh_region|" -e "s|@PUBLIC_PREFIX@|$public_prefix|" \
	"$source_dir/nftables/router.nft" > "$state_dir/router.nft"


step "compile the eBPF program"
clang -O2 -g -Wall -Werror -I"$state_dir" -I"/usr/include/$(uname -m)-linux-gnu" -target bpf \
	-c "$source_dir/bpf/router.c" -o "$state_dir/router.bpf.o"


step "enable IPv6 forwarding"
cat > /etc/sysctl.d/90-atlas-ipv6-router.conf <<'SYSCTL'
net.ipv6.conf.all.forwarding = 1
SYSCTL
sysctl -q -p /etc/sysctl.d/90-atlas-ipv6-router.conf


step "start the router"
install -m 0644 "$source_dir/systemd/atlas-ipv6-router.service" /etc/systemd/system/atlas-ipv6-router.service
systemctl daemon-reload
systemctl enable atlas-ipv6-router.service
systemctl restart atlas-ipv6-router.service


step "check the router"
tc filter show dev eth0 ingress | grep -q 'router.bpf.o' || { echo "the eBPF program is not attached to eth0" >&2; exit 1; }
nft list table ip6 atlas_ipv6_router >/dev/null

echo "the IPv6 router translates $public_prefix for region $REGION_ID"
