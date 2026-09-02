#!/usr/bin/env bash
# Build a bootable HTTP proxy image in a local Firecracker VM. Requires root.
# Usage: sudo ./build-image.sh [-o output.ext4]
set -euo pipefail

# Pin the build inputs and VM resources.
ROOTFS_URL="https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260826/ubuntu-24.04-minimal-cloudimg-amd64.squashfs"
ROOTFS_SHA256="a6426197cffaa3e419b255f39ccbe03e9123feecbea74f9f58a0d59284bbde52"
FC_CI_VERSION=v1.15
FC_KERNEL_VERSION=6.1.155
FC_VERSION=v1.16.1

DISK_GIB=4
MEMORY_MIB=1024
VCPU_COUNT=2
TIMEOUT_SECONDS=1200

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUTPUT="$HERE/proxy.ext4"
WORKDIR="$HERE/.build"

while getopts "o:h" opt; do
	case "$opt" in
	o) OUTPUT=$OPTARG ;;
	h)
		echo "usage: $0 [-o output.ext4]"
		exit 0
		;;
	*) exit 1 ;;
	esac
done

[[ $EUID -eq 0 ]] || {
	echo "build-image.sh: must run as root" >&2
	exit 1
}

# Check the host tools before the build changes host state.
for command in chroot curl ip iptables mkfs.ext4 sha256sum ssh ssh-keygen tar timeout truncate unsquashfs; do
	command -v "$command" >/dev/null || {
		echo "build-image.sh: missing command: $command" >&2
		exit 1
	}
done

# Define the build helpers.
step() {
	echo "==> $*"
}

verify_sha256() {
	local file=$1 expected=$2 actual
	actual=$(sha256sum "$file" | awk '{print $1}')
	[[ $actual == "$expected" ]] && return

	rm -f "$file"
	echo "build-image.sh: checksum mismatch for $file" >&2
	exit 1
}

fetch() {
	local url=$1 destination=$2 checksum=$3
	if [[ -f $destination ]]; then
		verify_sha256 "$destination" "$checksum"
		return
	fi

	curl -fsSL -o "$destination.part" "$url"
	verify_sha256 "$destination.part" "$checksum"
	mv "$destination.part" "$destination"
}

wait_for_socket() {
	local elapsed=0
	until [[ -S $SOCK ]]; do
		kill -0 "$FC_PID" 2>/dev/null || {
			echo "build-image.sh: firecracker exited before its API was ready" >&2
			exit 1
		}
		sleep 1
		elapsed=$((elapsed + 1))
		((elapsed < 30)) || {
			echo "build-image.sh: timed out waiting for firecracker" >&2
			exit 1
		}
	done
}

ssh_guest() {
	ssh "${SSH_OPTIONS[@]}" "root@$GUEST_IP" "$@"
}

wait_for_ssh() {
	local elapsed=0
	until ssh_guest true 2>/dev/null; do
		kill -0 "$FC_PID" 2>/dev/null || {
			echo "build-image.sh: firecracker exited before SSH was ready" >&2
			exit 1
		}
		sleep 2
		elapsed=$((elapsed + 2))
		((elapsed < TIMEOUT_SECONDS)) || {
			echo "build-image.sh: timed out waiting for SSH" >&2
			exit 1
		}
	done
}

wait_for_exit() {
	local elapsed=0
	while kill -0 "$FC_PID" 2>/dev/null; do
		sleep 2
		elapsed=$((elapsed + 2))
		if ((elapsed >= TIMEOUT_SECONDS)); then
			echo "build-image.sh: timed out waiting for shutdown" >&2
			kill "$FC_PID" 2>/dev/null || true
		fi
	done
	wait "$FC_PID" 2>/dev/null || true
	FC_PID=""
}

# Track and remove the temporary VM and host network state.
FC_PID=""
UPLINK=""
IP_FORWARD_PREVIOUS=""
TAP_CREATED=0
NAT_RULE_ADDED=0
FORWARD_OUT_RULE_ADDED=0
FORWARD_IN_RULE_ADDED=0

cleanup() {
	[[ -n $FC_PID ]] && kill "$FC_PID" 2>/dev/null || true
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

trap cleanup EXIT
trap 'exit 130' INT TERM

# Define the build files and guest network.
ARCH=$(uname -m)
SQUASHFS="$WORKDIR/ubuntu-24.04-minimal.squashfs"
ROOTFS="$WORKDIR/rootfs"
KERNEL="$WORKDIR/vmlinux-$FC_KERNEL_VERSION"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/$FC_CI_VERSION/$ARCH/vmlinux-$FC_KERNEL_VERSION"
FIRECRACKER="$WORKDIR/firecracker"
SOCK="$WORKDIR/firecracker.sock"
BUILDING="$WORKDIR/proxy.ext4"
KEY="$WORKDIR/id_ed25519"

TAP=atlas-build
GUEST_IP=100.64.1.2
GATEWAY_IP=100.64.1.1
NETMASK=255.255.255.252

mkdir -p "$WORKDIR"

# Prepare the Ubuntu root filesystem for remote provisioning.
step "download Ubuntu 24.04 minimal"
fetch "$ROOTFS_URL" "$SQUASHFS" "$ROOTFS_SHA256"

step "extract rootfs"
rm -rf "$ROOTFS"
unsquashfs -f -d "$ROOTFS" "$SQUASHFS"

step "configure rootfs"
[[ -f $KEY ]] || ssh-keygen -q -t ed25519 -N "" -f "$KEY"
[[ -f $KEY.pub ]] || ssh-keygen -y -f "$KEY" >"$KEY.pub"
chroot "$ROOTFS" chpasswd <<<"root:atlas-dev"
chroot "$ROOTFS" ssh-keygen -A
install -d -m 0700 "$ROOTFS/root/.ssh"
install -m 0600 "$KEY.pub" "$ROOTFS/root/.ssh/authorized_keys"
install -d "$ROOTFS/etc/ssh/sshd_config.d" "$ROOTFS/etc/cloud/cloud.cfg.d" "$ROOTFS/etc/systemd/network"
install -d "$ROOTFS/etc/systemd/system/ssh.service.d"
printf 'PermitRootLogin yes\n' >"$ROOTFS/etc/ssh/sshd_config.d/atlas-build.conf"
touch "$ROOTFS/etc/cloud/cloud-init.disabled"
printf 'atlas-proxy\n' >"$ROOTFS/etc/hostname"
cat >"$ROOTFS/etc/hosts" <<-'EOF'
	127.0.0.1 localhost
	127.0.1.1 atlas-proxy

	::1 ip6-localhost ip6-loopback
	fe00::0 ip6-localnet
	ff00::0 ip6-mcastprefix
	ff02::1 ip6-allnodes
	ff02::2 ip6-allrouters
	ff02::3 ip6-allhosts
EOF
cat >"$ROOTFS/etc/cloud/cloud.cfg.d/99-atlas-default-user.cfg" <<'EOF'
preserve_hostname: true
system_info:
  default_user:
    name: frappe
    groups: [sudo]
    sudo: "ALL=(ALL:ALL) NOPASSWD: ALL"
    shell: /bin/bash
    lock_passwd: true
EOF
cat >"$ROOTFS/etc/systemd/system/atlas-ssh-host-keys.service" <<'EOF'
[Unit]
Description=Generate SSH host keys
Before=ssh.service

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
EOF
cat >"$ROOTFS/etc/systemd/system/ssh.service.d/atlas-host-keys.conf" <<'EOF'
[Unit]
Requires=atlas-ssh-host-keys.service
After=atlas-ssh-host-keys.service
EOF

cat >"$ROOTFS/etc/systemd/network/eth0.network" <<-'EOF'
	[Match]
	Name=eth0

	[Link]
	Unmanaged=yes
EOF
rm -f "$ROOTFS/etc/resolv.conf"
cat >"$ROOTFS/etc/resolv.conf" <<-'EOF'
	nameserver 8.8.8.8
	nameserver 1.1.1.1
	options single-request-reopen
EOF

for unit in snapd.seeded.service snapd.service snapd.socket apport.service lxd-installer.socket \
	systemd-networkd-wait-online.service pollinate.service; do
	ln -sf /dev/null "$ROOTFS/etc/systemd/system/$unit"
done

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

# Create the writable disk that becomes the image.
step "create proxy disk"
rm -f "$BUILDING"
truncate -s "${DISK_GIB}G" "$BUILDING"
mkfs.ext4 -q -d "$ROOTFS" -F "$BUILDING"

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
	echo "build-image.sh: no default network route" >&2
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

# Start and configure the build VM.
step "start build VM"
rm -f "$SOCK"
"$FIRECRACKER" --api-sock "$SOCK" >/dev/null 2>&1 &
FC_PID=$!
wait_for_socket

api() {
	curl -fsS --unix-socket "$SOCK" -X "$1" "http://localhost$2" -H 'content-type: application/json' -d "$3" >/dev/null
}

api PUT /machine-config "{\"vcpu_count\":$VCPU_COUNT,\"mem_size_mib\":$MEMORY_MIB,\"smt\":false}"
api PUT /boot-source "{\"kernel_image_path\":\"$KERNEL\",\"boot_args\":\"console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw ip=$GUEST_IP::$GATEWAY_IP:$NETMASK::eth0:off\"}"
api PUT /drives/rootfs "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$BUILDING\",\"is_root_device\":true,\"is_read_only\":false}"
api PUT /network-interfaces/eth0 "{\"iface_id\":\"eth0\",\"host_dev_name\":\"$TAP\"}"
api PUT /actions '{"action_type":"InstanceStart"}'

SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=2 -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$KEY")

# Copy the source and run the real setup script in the guest.
step "wait for SSH"
wait_for_ssh

step "copy proxy source"
tar -C "$HERE" --exclude=.build --exclude=.dev --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache '--exclude=*.ext4' -cf - . \
	| ssh_guest 'rm -rf /src/proxy && mkdir -p /src/proxy && tar -C /src/proxy -xf -'

step "install proxy"
timeout --foreground "$TIMEOUT_SECONDS" ssh -tt "${SSH_OPTIONS[@]}" "root@$GUEST_IP" 'chmod +x /src/proxy/nginx/setup.sh && /src/proxy/nginx/setup.sh'

# Remove build credentials and publish the completed image.
step "finalize image"
ssh_guest 'rm -f /etc/cloud/cloud-init.disabled /etc/ssh/sshd_config.d/atlas-build.conf /var/lib/dbus/machine-id; rm -rf /root/.ssh /src/proxy; rm -f /etc/ssh/ssh_host_*; id -u ubuntu >/dev/null 2>&1 && userdel --remove ubuntu || true; getent group ubuntu >/dev/null 2>&1 && groupdel ubuntu || true; : >/etc/machine-id; passwd -l root; apt-get clean; rm -rf /var/lib/apt/lists/*; sync; reboot' || true

wait_for_exit

mkdir -p "$(dirname "$OUTPUT")"
mv "$BUILDING" "$OUTPUT"
echo "built $OUTPUT"
