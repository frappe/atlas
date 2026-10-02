#!/usr/bin/env bash
# Install the Atlas WireGuard gateway on Ubuntu 24.04. You can run this script again.

set -euo pipefail

: "${REGION_ID:?REGION_ID is required}"
: "${GATEWAY_ID:?GATEWAY_ID is required}"
: "${GATEWAY_MESH:?GATEWAY_MESH is required}"
: "${LISTEN_PORT:?LISTEN_PORT is required}"

source_dir=$(cd "$(dirname "$0")" && pwd)
state_dir=/opt/atlas/wg-gateway
python_version=3.14
deadsnakes_key=F23C5A6CF475977595C89F51BA6932366A755776

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
apt-get install -y -qq wireguard-tools clang libbpf-dev linux-libc-dev iproute2 python3 ca-certificates curl gnupg lsb-release


step "load kernel modules"
# Atlas boots the kernel from outside the root file system, so the image can miss these modules.
apt-get install -y -qq "linux-modules-$(uname -r)" "linux-modules-extra-$(uname -r)"
modprobe wireguard
modprobe sch_ingress
modprobe cls_bpf
printf '%s\n' wireguard sch_ingress cls_bpf > /etc/modules-load.d/atlas-wg-gateway.conf


step "enable IPv6 forwarding"
cat > /etc/sysctl.d/90-atlas-wg-gateway.conf <<'SYSCTL'
net.ipv6.conf.all.forwarding = 1
SYSCTL
sysctl -q -p /etc/sysctl.d/90-atlas-wg-gateway.conf


step "create the WireGuard interface"
install -d -m 0750 "$state_dir"

# A customer tunnel packet fits eth0: its MTU minus the outer IPv4 (20), UDP (8), and WireGuard (32) headers.
wireguard_mtu=$(( $(cat /sys/class/net/eth0/mtu) - 60 ))
ip link add wg0 type wireguard 2>/dev/null || true
ip link set wg0 mtu "$wireguard_mtu"
ip link set wg0 up
read -r gateway_address gateway_prefix mesh0 mesh1 mesh2 mesh3 < <(python3 - "$REGION_ID" "$GATEWAY_ID" "$GATEWAY_MESH" <<'PYTHON'
import ipaddress
import sys

region, gateway = map(int, sys.argv[1:3])
if not 0 <= region <= 0xffff or not 1 <= gateway <= 0xffff:
	sys.exit("REGION_ID or GATEWAY_ID is outside its IPv6 address field")
base = (0xfdac << 112) | (region << 96) | (gateway << 80)
mesh = ipaddress.IPv6Address(sys.argv[3]).packed
print(ipaddress.IPv6Address(base | 1), ipaddress.IPv6Network((base, 48)),
	*(int.from_bytes(mesh[index:index + 4], "big") for index in range(0, 16, 4)))
PYTHON
)
ip -6 addr replace "$gateway_address/128" dev wg0
ip -6 route replace "$gateway_prefix" dev wg0
printf 'WG_GATEWAY_ADDRESS=%s/128\nWG_GATEWAY_PREFIX=%s\nWG_GATEWAY_MTU=%s\n' \
	"$gateway_address" "$gateway_prefix" "$wireguard_mtu" > "$state_dir/network.env"


step "compile the tenant filter"
clang -O2 -g -Wall -Werror -DREGION_ID="$REGION_ID" -DGATEWAY_ID="$GATEWAY_ID" \
	-DMESH_WORD0="$mesh0" -DMESH_WORD1="$mesh1" -DMESH_WORD2="$mesh2" -DMESH_WORD3="$mesh3" \
	-I"/usr/include/$(uname -m)-linux-gnu" -target bpf \
	-c "$source_dir/bpf/gateway.c" -o "$state_dir/gateway.bpf.o"


step "start the gateway"
install -m 0644 "$source_dir/systemd/atlas-wg-gateway.service" /etc/systemd/system/atlas-wg-gateway.service

systemctl daemon-reload
systemctl enable atlas-wg-gateway.service
systemctl restart atlas-wg-gateway.service


step "install the gateway API"
curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${deadsnakes_key}" \
	| gpg --batch --yes --dearmor -o /usr/share/keyrings/deadsnakes.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/deadsnakes.gpg] https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu $(lsb_release -sc) main" \
	> /etc/apt/sources.list.d/deadsnakes.list
apt-get update -qq
apt-get install -y -qq "python${python_version}" "python${python_version}-venv"
"python${python_version}" -m venv "$state_dir/venv"
"$state_dir/venv/bin/pip" install --no-cache-dir --quiet "$source_dir/control-cluster" "$source_dir/daemon"
"$state_dir/venv/bin/python" -m compileall -q "$state_dir/venv"/lib/python3*/site-packages/{atlas_control,gatewayd}
install -m 0644 "$source_dir/systemd/atlas-wg-gateway-api.service" /etc/systemd/system/atlas-wg-gateway-api.service
systemctl daemon-reload
# Atlas writes /etc/atlas/wireguard-gateway.toml next and starts the API.
systemctl enable atlas-wg-gateway-api.service


step "check the gateway"
ip link show wg0 >/dev/null
tc filter show dev wg0 ingress | grep -q gateway.bpf.o
tc filter show dev eth0 ingress | grep -q gateway.bpf.o

echo "the WireGuard gateway listens on port $LISTEN_PORT and routes $gateway_prefix"
