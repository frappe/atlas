#!/usr/bin/env bash
# Build an Ubuntu server cloud image for Metal.
set -euo pipefail

output=""
kernel_output=""
architecture=""
version=""
minimal=false
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

step() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		--architecture) architecture=$2; shift 2 ;;
		--version) version=$2; shift 2 ;;
		--minimal) minimal=true; shift ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[[ -n $output && -n $kernel_output && -n $architecture && -n $version ]] || { echo "--output, --kernel-output, --architecture, and --version are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run this builder with root permissions" >&2; exit 1; }

case "$architecture" in
	amd64) ;;
	*) echo "unsupported architecture: $architecture" >&2; exit 2 ;;
esac

# Boot the newest kernel that Ubuntu supports for the release: the HWE kernel when the release has one.
case "$version" in
	22.04) series=jammy; kernel_package=linux-image-generic-hwe-22.04 ;;
	24.04) series=noble; kernel_package=linux-image-generic-hwe-24.04 ;;
	26.04) series=resolute; kernel_package=linux-image-generic ;;
	*) echo "unsupported version: $version" >&2; exit 2 ;;
esac

archive_url="https://archive.ubuntu.com/ubuntu"
series_url="https://cloud-images.ubuntu.com/releases/$series"
rootfs_name="ubuntu-$version-server-cloudimg-amd64"
if $minimal; then
	[[ $version == 24.04 ]] || { echo "minimal images are available only for Ubuntu 24.04" >&2; exit 2; }
	series_url="https://cloud-images.ubuntu.com/minimal/releases/$series"
	rootfs_name="ubuntu-$version-minimal-cloudimg-amd64"
fi

for command in curl sha256sum unsquashfs mkfs.ext4 truncate zstd zcat ar depmod; do
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
	[[ -n $checksum ]] || { echo "no checksum for $(basename "$path")" >&2; return 1; }
	step "download $url"
	curl -fL --progress-bar --output "$path" "$url"
	echo "$checksum  $path" | sha256sum --check --status
	step "verified $(basename "$path")"
}

# Use the dated folder of the latest release, so a release published during the build cannot mix files.
fetch_rootfs() {
	local serial release_url checksum
	serial=$(curl -fsSL "$series_url/release/unpacked/build-info.txt" | sed -n 's/^serial=//p')
	release_url="$series_url/release-$serial"
	checksum=$(curl -fsSL "$release_url/SHA256SUMS" | awk -v file="*$rootfs_name.squashfs" '$2 == file { print $1 }')
	fetch "$release_url/$rootfs_name.squashfs" "$checksum" "$1"
}

package_field() {
	awk -v name="$1" -v field="$2:" '
		$1 == "Package:" { found = ($2 == name) }
		found && $1 == field { print $2 }' "$packages_path"
}

fetch_package() {
	local filename checksum
	filename=$(package_field "$1" Filename)
	checksum=$(package_field "$1" SHA256)
	fetch "$archive_url/$filename" "$checksum" "$2"
}

# Kernel image packages compress their data with zstd and modules packages do not. zstd -f passes plain data through.
extract_package() {
	local member
	member=$(ar t "$1" | grep '^data\.tar')
	mkdir -p "$2"
	ar p "$1" "$member" | zstd -dcf | tar -x --keep-directory-symlink -C "$2"
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

# DefaultDependencies=no lets the mesh address skip sysinit.target, which cloud-init holds for seconds.
# Never order this unit before cloud-init.service: systemd breaks the cycle by dropping cloud-init, so no SSH host keys.
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

rootfs_path="$work_path/rootfs.squashfs"
rootfs_directory="$work_path/rootfs"
fetch_rootfs "$rootfs_path"
step "extract root file system"
unsquashfs -q -d "$rootfs_directory" "$rootfs_path"

packages_path="$work_path/Packages"
curl -fsSL "$archive_url/dists/$series-updates/main/binary-amd64/Packages.gz" | zcat > "$packages_path"
# The kernel metapackage depends first on the versioned image, such as linux-image-7.0.0-38-generic.
kernel_version=$(package_field "$kernel_package" Depends | sed -E 's/^linux-image-//; s/,$//')

fetch_package "linux-image-$kernel_version" "$work_path/kernel.deb"
extract_package "$work_path/kernel.deb" "$work_path/kernel"
step "extract uncompressed vmlinux"
extract_vmlinux "$work_path/kernel/boot/vmlinuz-$kernel_version" "$kernel_path.part" || {
	echo "could not extract an ELF vmlinux from the Ubuntu kernel" >&2
	exit 1
}
mv "$kernel_path.part" "$kernel_path"
step "extracted $(basename "$kernel_path")"

# Docker and BPF need modules that match the kernel, whatever the root file system ships.
fetch_package "linux-modules-$kernel_version" "$work_path/modules.deb"
extract_package "$work_path/modules.deb" "$rootfs_directory"
depmod -b "$rootfs_directory" "$kernel_version"

install_cloud_init_datasource
install_guest_network
install_metadata_service
install_serial_console
install_ssh_metadata

step "create ext4 image"
truncate -s 4G "$image_path.part"

# Remount read-only at the first file system error, so a damaged disk stops taking writes.
mkfs.ext4 -q -F -e remount-ro -d "$rootfs_directory" "$image_path.part"
mv "$image_path.part" "$image_path"

step "compress ext4 image"
zstd -q -T0 -3 -f -o "$image_path.zst" "$image_path"

echo "Built $image_path and $image_path.zst"
echo "Built $kernel_path"
