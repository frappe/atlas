#!/usr/bin/env bash
# Build an Ubuntu server cloud image for Metal.
set -euo pipefail

output=""
kernel_output=""
architecture=""
version=""
minimal=false
rescue=false
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

step() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		--architecture) architecture=$2; shift 2 ;;
		--version) version=$2; shift 2 ;;
		--minimal) minimal=true; shift ;;
		--rescue) rescue=true; shift ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[[ -n $output && -n $kernel_output && -n $architecture && -n $version ]] || { echo "--output, --kernel-output, --architecture, and --version are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run this builder with root permissions" >&2; exit 1; }

if $rescue && [[ $version != 24.04 ]]; then
	echo "rescue images require Ubuntu 24.04 to match the supplied kernel modules" >&2
	exit 2
fi

case "$architecture" in
	amd64) ;;
	*) echo "unsupported architecture: $architecture" >&2; exit 2 ;;
esac

# Keep each release URL with its checksums.
case "$version" in
	22.04)
		if $minimal; then
			echo "minimal images are available only for Ubuntu 24.04" >&2
			exit 2
		fi
		release_url="https://cloud-images.ubuntu.com/releases/jammy/release-20260826"
		rootfs_url="$release_url/ubuntu-22.04-server-cloudimg-amd64.squashfs"
		rootfs_sha256="a4f612de4736d534a5617531eb7c1771b0b14878549c9d32191fd50e3077eb4f"
		kernel_url="https://cloud-images.ubuntu.com/releases/noble/release-20260518/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic"
		kernel_sha256="3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6"
		;;
	24.04)
		if $minimal; then
			release_url="https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260521"
			rootfs_url="$release_url/ubuntu-24.04-minimal-cloudimg-amd64.squashfs"
			rootfs_sha256="a288f0bd499e1a747f86fda8ec9822dd99a4e3c0721d89ffd9dd57608ff21072"
			kernel_url="$release_url/unpacked/ubuntu-24.04-minimal-cloudimg-amd64-vmlinuz-generic"
		else
			release_url="https://cloud-images.ubuntu.com/releases/noble/release-20260518"
			rootfs_url="$release_url/ubuntu-24.04-server-cloudimg-amd64.squashfs"
			rootfs_sha256="bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74"
			kernel_url="$release_url/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic"
		fi
		kernel_sha256="3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6"
		;;
	*) echo "unsupported version: $version" >&2; exit 2 ;;
esac

for command in curl sha256sum unsquashfs mkfs.ext4 truncate zstd; do
	command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done

image_path=$(realpath -m "$output")
kernel_path=$(realpath -m "$kernel_output")
work_path=$(mktemp -d)

cleanup() {
	rm -rf "$work_path"
}
trap cleanup EXIT

mkdir -p "$(dirname "$image_path")"
mkdir -p "$(dirname "$kernel_path")"

fetch() {
	local url=$1
	local checksum=$2
	local path=$3
	step "download $url"
	curl -fL --progress-bar --output "$path" "$url"
	echo "$checksum  $path" | sha256sum --check --status
	step "verified $(basename "$path")"
}


extract_vmlinux() {
	local image=$1 output=$2
	local zstd_magic offset
	zstd_magic=$(printf '\050\265\057\375')
	offset=$(grep -aboF -- "$zstd_magic" "$image" | head -n1 | cut -d: -f1)
	if [ -z "$offset" ]; then
		echo "no zstd stream in $image; the kernel compressor may have changed" >&2
		return 1
	fi

	# zstd writes the full kernel before it rejects the trailing bzImage data. Validate the ELF output below.
	tail -c "+$((offset + 1))" "$image" | zstd -cdq > "$output" 2>/dev/null || true
	if [ "$(head -c 4 "$output" | od -An -tx1 | tr -d ' \n')" != "7f454c46" ]; then
		echo "decompressed kernel is not an ELF vmlinux" >&2
		return 1
	fi
}

install_cloud_init_datasource() {
	install -d -m 0755 "$rootfs_directory/etc/cloud/cloud.cfg.d"
	cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-datasource.cfg" <<'EOF'
datasource_list: [ Atlas ]
EOF

	install -m 0644 "$script_directory/guest/DataSourceAtlas.py" \
		"$rootfs_directory/usr/lib/python3/dist-packages/cloudinit/sources/DataSourceAtlas.py"
	python3 -m py_compile "$rootfs_directory/usr/lib/python3/dist-packages/cloudinit/sources/DataSourceAtlas.py"
}

install_guest_network() {
	install -d -m 0755 "$rootfs_directory/etc/cloud/cloud.cfg.d"
	cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-network.cfg" <<'EOF'
network: {config: disabled}
EOF

	install -d -m 0755 "$rootfs_directory/etc/systemd/network"
	# Keep this equal to meshMTU in metal/internal/network/mesh.go.
	cat > "$rootfs_directory/etc/systemd/network/10-atlas.network" <<'EOF'
[Match]
Name=eth0

[Link]
MTUBytes=1380

[Network]
Address=172.16.0.2/24
Gateway=172.16.0.1
DNS=1.1.1.1
DNS=1.0.0.1
DNS=2606:4700:4700::1111
DNS=2606:4700:4700::1001

[Route]
Destination=169.254.169.254/32
Scope=link

# The VM namespace routes decide which IPv6 destinations leave the host.
[Route]
Destination=::/0
Gateway=fe80::1
EOF
}

# Apply the per-VM MMDS values that a shared image cannot hold. Do not order the
# unit before cloud-init.service: that service uses DefaultDependencies=no and
# runs before sysinit.target, so the order makes a cycle. systemd breaks the
# cycle by dropping cloud-init.service, which then never generates SSH host keys.
# The unit also uses DefaultDependencies=no, so the mesh address does not wait
# for sysinit.target, which cloud-init holds for several seconds after boot.
install_metadata_service() {
	install -d -m 0755 "$rootfs_directory/etc/cloud/cloud.cfg.d"
	cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-hostname.cfg" <<'EOF'
preserve_hostname: true
EOF

	install -D -m 0755 "$script_directory/guest/apply-metadata" \
		"$rootfs_directory/usr/local/lib/atlas/apply-metadata"
	cat > "$rootfs_directory/etc/systemd/system/atlas-metadata.service" <<'EOF'
[Unit]
Description=Apply the per-VM Atlas metadata
DefaultDependencies=no
After=local-fs.target systemd-networkd.service
Wants=systemd-networkd.service
Conflicts=shutdown.target
Before=shutdown.target

[Service]
Type=exec
ExecStart=/usr/local/lib/atlas/apply-metadata
Restart=always
RestartSec=1s

[Install]
WantedBy=multi-user.target
EOF

	install -d -m 0755 "$rootfs_directory/etc/systemd/system/multi-user.target.wants"
	ln -sf /etc/systemd/system/atlas-metadata.service \
		"$rootfs_directory/etc/systemd/system/multi-user.target.wants/atlas-metadata.service"
}

install_serial_console() {
	install -d -m 0755 "$rootfs_directory/etc/systemd/system/getty.target.wants"
	ln -sf /lib/systemd/system/serial-getty@.service \
		"$rootfs_directory/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service"
}

install_ssh_metadata() {
	install -D -m 0755 "$script_directory/guest/authorized-keys-command" \
		"$rootfs_directory/usr/local/lib/atlas/authorized-keys-command"
	install -d -m 0755 "$rootfs_directory/etc/ssh/sshd_config.d"
	cat > "$rootfs_directory/etc/ssh/sshd_config.d/90-atlas-mmds.conf" <<'EOF'
AuthorizedKeysCommand /usr/local/lib/atlas/authorized-keys-command
AuthorizedKeysCommandUser nobody
EOF
}

install_rescue_tools() {
	install -D -m 0755 "$script_directory/guest/rescue-reboot" \
		"$rootfs_directory/usr/lib/systemd/system-shutdown/atlas-rescue"
	install -D -m 0644 "$script_directory/guest/rescue-motd" "$rootfs_directory/etc/motd"
	# This image has a single root filesystem. Leave the attached original disk alone.
	cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-rescue.cfg" <<'EOF'
mounts: []
growpart:
  mode: "off"
resize_rootfs: false
EOF
	# Package scripts must not start daemons in the build host's namespaces.
	cat > "$rootfs_directory/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
	chmod 0755 "$rootfs_directory/usr/sbin/policy-rc.d"
	rm -f "$rootfs_directory/etc/resolv.conf"
	cp /etc/resolv.conf "$rootfs_directory/etc/resolv.conf"
	chroot "$rootfs_directory" apt-get update
	chroot "$rootfs_directory" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		python3 e2fsprogs xfsprogs btrfs-progs util-linux parted lvm2 mdadm \
		cryptsetup-bin curl ca-certificates openssh-server vim-tiny linux-image-virtual
	chroot "$rootfs_directory" apt-get clean
	# Snap seeding can wait for Internet access and hold cloud-final indefinitely.
	# The repair tools are installed by apt, so rescue does not need Snap services.
	for unit in snapd.service snapd.socket snapd.seeded.service snapd.autoimport.service; do
		ln -sf /dev/null "$rootfs_directory/etc/systemd/system/$unit"
	done
	# Use the kernel installed with the modules, rather than a separately downloaded version.
	local rescue_kernel
	rescue_kernel=$(find "$rootfs_directory/boot" -maxdepth 1 -name 'vmlinuz-*' | sort -V | tail -n1)
	[[ -n $rescue_kernel ]] || { echo "rescue kernel was not installed" >&2; return 1; }
	extract_vmlinux "$rescue_kernel" "$kernel_path.part"
	mv "$kernel_path.part" "$kernel_path"
	# Package installation can create host identities. Each guest must generate its own.
	rm -f "$rootfs_directory"/etc/ssh/ssh_host_* "$rootfs_directory/var/lib/dbus/machine-id"
	: > "$rootfs_directory/etc/machine-id"
	ln -s /etc/machine-id "$rootfs_directory/var/lib/dbus/machine-id"
	rm "$rootfs_directory/usr/sbin/policy-rc.d" "$rootfs_directory/etc/resolv.conf"
	ln -s ../run/systemd/resolve/stub-resolv.conf "$rootfs_directory/etc/resolv.conf"
	# Ubuntu ships the vsock transport as a module. Load it before shutdown begins.
	install -d -m 0755 "$rootfs_directory/etc/modules-load.d"
	printf 'vmw_vsock_virtio_transport\n' > "$rootfs_directory/etc/modules-load.d/atlas-rescue.conf"
}

rootfs_path="$work_path/rootfs.squashfs"
rootfs_directory="$work_path/rootfs"
fetch "$rootfs_url" "$rootfs_sha256" "$rootfs_path"

if ! $rescue; then
	vmlinuz_path="$work_path/vmlinuz"
	fetch "$kernel_url" "$kernel_sha256" "$vmlinuz_path"
	step "extract uncompressed vmlinux"
	extract_vmlinux "$vmlinuz_path" "$kernel_path.part" || {
		echo "could not extract an ELF vmlinux from the Ubuntu kernel" >&2
		exit 1
	}
	mv "$kernel_path.part" "$kernel_path"
	step "extracted $(basename "$kernel_path")"
fi

step "extract root file system"
unsquashfs -q -d "$rootfs_directory" "$rootfs_path"

install_cloud_init_datasource
install_guest_network
install_metadata_service
install_serial_console
install_ssh_metadata
if $rescue; then
	install_rescue_tools
fi

step "create ext4 image"
truncate -s 4G "$image_path.part"

# Remount read-only at the first file system error, so a damaged disk stops taking writes.
mkfs.ext4 -q -F -e remount-ro -d "$rootfs_directory" "$image_path.part"
mv "$image_path.part" "$image_path"

step "compress ext4 image"
zstd -q -T0 -3 -f -o "$image_path.zst" "$image_path"

echo "Built $image_path and $image_path.zst"
echo "Built $kernel_path"
