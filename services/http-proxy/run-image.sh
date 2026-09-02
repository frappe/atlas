#!/usr/bin/env bash
# Boot a proxy image in a local Firecracker VM. Requires root.
# Usage: sudo ./run-image.sh [-i image.ext4] [-j jwks-url] [-a jwks-audience] [-p password]
set -euo pipefail

FC_CI_VERSION=v1.15
FC_KERNEL_VERSION=6.1.155
FC_VERSION=v1.16.1

MEMORY_MIB=1024
VCPU_COUNT=2

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IMAGE="$HERE/proxy.ext4"
WORKDIR="$HERE/.dev"
JWKS_URL=""
JWKS_AUDIENCE=""
PASSWORD=""

while getopts "i:j:a:p:h" opt; do
	case "$opt" in
	i) IMAGE=$OPTARG ;;
	j) JWKS_URL=$OPTARG ;;
	a) JWKS_AUDIENCE=$OPTARG ;;
	p) PASSWORD=$OPTARG ;;
	h)
		echo "usage: $0 [-i image.ext4] [-j jwks-url] [-a jwks-audience] [-p password]"
		exit 0
		;;
	*) exit 1 ;;
	esac
done

[[ $EUID -eq 0 ]] || {
	echo "run-image.sh: must run as root" >&2
	exit 1
}
[[ -f $IMAGE ]] || {
	echo "run-image.sh: image not found: $IMAGE" >&2
	exit 1
}
for command in chroot curl ip iptables mount python3 ssh ssh-keygen tar; do
	command -v "$command" >/dev/null || {
		echo "run-image.sh: missing command: $command" >&2
		exit 1
	}
done

if [[ -z $PASSWORD ]]; then
	read -r -s -p "Proxy password: " PASSWORD
	echo
fi
[[ -n $PASSWORD ]] || {
	echo "run-image.sh: proxy password is required" >&2
	exit 1
}

step() {
	echo "==> $*"
}

wait_for_socket() {
	local elapsed=0
	until [[ -S $SOCK ]]; do
		kill -0 "$FC_PID" 2>/dev/null || {
			echo "run-image.sh: firecracker exited before its API was ready" >&2
			exit 1
		}
		sleep 1
		elapsed=$((elapsed + 1))
		((elapsed < 30)) || {
			echo "run-image.sh: timed out waiting for firecracker" >&2
			exit 1
		}
	done
}

wait_for_proxy() {
	local elapsed=0
	until curl --silent --noproxy '*' --insecure --connect-timeout 2 --max-time 3 \
		--resolve "probe.invalid:443:$GUEST_IP" https://probe.invalid/ -o /dev/null; do
		kill -0 "$FC_PID" 2>/dev/null || {
			echo "run-image.sh: firecracker exited before the proxy was ready" >&2
			exit 1
		}
		sleep 1
		elapsed=$((elapsed + 1))
		((elapsed < 120)) || {
			echo "run-image.sh: timed out waiting for the proxy" >&2
			exit 1
		}
	done
}

FC_PID=""
UPLINK=""
IP_FORWARD_PREVIOUS=""
MOUNTED=0
TAP_CREATED=0
NAT_RULE_ADDED=0
FORWARD_OUT_RULE_ADDED=0
FORWARD_IN_RULE_ADDED=0

cleanup() {
	[[ -n $FC_PID ]] && kill "$FC_PID" 2>/dev/null || true
	if ((MOUNTED)); then
		umount "$MOUNT" 2>/dev/null || true
	fi
	if ((NAT_RULE_ADDED)); then
		iptables -t nat -D POSTROUTING -s "$GUEST_IP/32" -o "$UPLINK" -j MASQUERADE 2>/dev/null || true
	fi
	if ((FORWARD_OUT_RULE_ADDED)); then
		iptables -D FORWARD -i "$TAP" -o "$UPLINK" -j ACCEPT 2>/dev/null || true
	fi
	if ((FORWARD_IN_RULE_ADDED)); then
		iptables -D FORWARD -i "$UPLINK" -o "$TAP" -j ACCEPT 2>/dev/null || true
	fi
	if [[ -n $IP_FORWARD_PREVIOUS ]]; then
		sysctl -q -w "net.ipv4.ip_forward=$IP_FORWARD_PREVIOUS" 2>/dev/null || true
	fi
	((TAP_CREATED)) && ip link del "$TAP" 2>/dev/null || true
	rm -f "$SOCK"
}

# Define the build files and guest network.
ARCH=$(uname -m)
FIRECRACKER="$WORKDIR/firecracker"
KERNEL="$WORKDIR/vmlinux-$FC_KERNEL_VERSION"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/$FC_CI_VERSION/$ARCH/vmlinux-$FC_KERNEL_VERSION"
SOCK="$WORKDIR/firecracker.sock"
ROOTFS_COPY="$WORKDIR/rootfs.ext4"
MOUNT="$WORKDIR/mount"
KEY="$WORKDIR/id_ed25519"

TAP=atlas-dev
GUEST_IP=100.64.0.2
GATEWAY_IP=100.64.0.1
NETMASK=255.255.255.252

mkdir -p "$WORKDIR"

trap cleanup EXIT
trap 'exit 130' INT TERM

# Download the Firecracker runtime and kernel.
step "download Firecracker"
if [[ ! -x $FIRECRACKER ]]; then
	curl -fsSL -o "$WORKDIR/firecracker.tgz" "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VERSION/firecracker-$FC_VERSION-$ARCH.tgz"
	tar -xzf "$WORKDIR/firecracker.tgz" -C "$WORKDIR"
	cp "$WORKDIR/release-$FC_VERSION-$ARCH/firecracker-$FC_VERSION-$ARCH" "$FIRECRACKER"
	chmod +x "$FIRECRACKER"
fi

step "download Firecracker kernel"
[[ -f $KERNEL ]] || curl -fsSL -o "$KERNEL" "$KERNEL_URL"

# Copy the image and add temporary control and SSH credentials.
step "prepare image"
cp "$IMAGE" "$ROOTFS_COPY"
mkdir -p "$MOUNT"
mount -o loop "$ROOTFS_COPY" "$MOUNT"
MOUNTED=1
[[ -f $KEY ]] || ssh-keygen -q -t ed25519 -N "" -f "$KEY"
[[ -f $KEY.pub ]] || ssh-keygen -y -f "$KEY" >"$KEY.pub"
# The script runs as root. Give the key to the caller, so the printed SSH
# command works without sudo.
if [[ -n ${SUDO_UID:-} ]]; then
	chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$WORKDIR" "$KEY" "$KEY.pub"
fi
install -d -m 0700 "$MOUNT/home/frappe/.ssh"
install -m 0600 "$KEY.pub" "$MOUNT/home/frappe/.ssh/authorized_keys"
chroot "$MOUNT" chown -R frappe:frappe /home/frappe/.ssh
PASSWORD_HASH=$(chroot "$MOUNT" /opt/atlas/proxy-control/bin/python3 -c \
	"import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())" "$PASSWORD")
install -m 0640 /dev/stdin "$MOUNT/etc/atlas/proxy-control.htpasswd" <<<"atlas:$PASSWORD_HASH"
umount "$MOUNT"
MOUNTED=0

# Route guest traffic through the host uplink.
step "configure network"
ip tuntap add "$TAP" mode tap
TAP_CREATED=1
ip addr add "$GATEWAY_IP/30" dev "$TAP"
ip link set "$TAP" up
IP_FORWARD_PREVIOUS=$(sysctl -n net.ipv4.ip_forward)
[[ $IP_FORWARD_PREVIOUS == 1 ]] || sysctl -q -w net.ipv4.ip_forward=1
UPLINK=$(ip route show default | awk '{print $5; exit}')
[[ -n $UPLINK ]] || {
	echo "run-image.sh: no default network route" >&2
	exit 1
}
iptables -t nat -C POSTROUTING -s "$GUEST_IP/32" -o "$UPLINK" -j MASQUERADE 2>/dev/null || {
	iptables -t nat -A POSTROUTING -s "$GUEST_IP/32" -o "$UPLINK" -j MASQUERADE
	NAT_RULE_ADDED=1
}
iptables -C FORWARD -i "$TAP" -o "$UPLINK" -j ACCEPT 2>/dev/null || {
	iptables -I FORWARD -i "$TAP" -o "$UPLINK" -j ACCEPT
	FORWARD_OUT_RULE_ADDED=1
}
iptables -C FORWARD -i "$UPLINK" -o "$TAP" -j ACCEPT 2>/dev/null || {
	iptables -I FORWARD -i "$UPLINK" -o "$TAP" -j ACCEPT
	FORWARD_IN_RULE_ADDED=1
}

# Start the VM and seed its control configuration.
step "start VM"
rm -f "$SOCK"
"$FIRECRACKER" --api-sock "$SOCK" &
FC_PID=$!
wait_for_socket

api() {
	curl -fsS --unix-socket "$SOCK" -X "$1" "http://localhost$2" -H 'content-type: application/json' -d "$3" >/dev/null
}

BOOT_ARGS="console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw ip=$GUEST_IP::$GATEWAY_IP:$NETMASK::eth0:off"
api PUT /machine-config "{\"vcpu_count\":$VCPU_COUNT,\"mem_size_mib\":$MEMORY_MIB,\"smt\":false}"
api PUT /boot-source "{\"kernel_image_path\":\"$KERNEL\",\"boot_args\":\"$BOOT_ARGS\"}"
api PUT /drives/rootfs "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$ROOTFS_COPY\",\"is_root_device\":true,\"is_read_only\":false}"
api PUT /network-interfaces/eth0 "{\"iface_id\":\"eth0\",\"host_dev_name\":\"$TAP\"}"

USER_DATA="{}"
[[ -n $JWKS_URL ]] && USER_DATA=$(echo "$USER_DATA" | python3 -c "import json,sys; data=json.load(sys.stdin); data['proxy_jwks_url']=sys.argv[1]; print(json.dumps(data))" "$JWKS_URL")
[[ -n $JWKS_AUDIENCE ]] && USER_DATA=$(echo "$USER_DATA" | python3 -c "import json,sys; data=json.load(sys.stdin); data['proxy_jwks_audience_id']=sys.argv[1]; print(json.dumps(data))" "$JWKS_AUDIENCE")
api PUT /mmds/config '{"version":"V2","network_interfaces":["eth0"]}'
api PUT /mmds "{\"latest\":{\"meta-data\":{\"user-data\":$USER_DATA}}}"
api PUT /actions '{"action_type":"InstanceStart"}'

step "wait for proxy"
wait_for_proxy

echo "booted. control daemon: http://$GUEST_IP:9000, proxy: http://$GUEST_IP:80 https://$GUEST_IP:443"
echo "SSH: ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $KEY frappe@$GUEST_IP"
echo "Ctrl-C stops the VM and cleans up."
wait "$FC_PID"
