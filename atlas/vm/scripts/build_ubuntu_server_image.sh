#!/usr/bin/env bash
# Build an Ubuntu server cloud image for Metal.
set -euo pipefail

output=""
kernel_output=""
platform=""
version=""
minimal=false

step() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		--platform) platform=$2; shift 2 ;;
		--version) version=$2; shift 2 ;;
		--minimal) minimal=true; shift ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[[ -n $output && -n $kernel_output && -n $platform && -n $version ]] || { echo "--output, --kernel-output, --platform, and --version are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run this builder with root permissions" >&2; exit 1; }

case "$platform" in
	amd64) ;;
	*) echo "unsupported platform: $platform" >&2; exit 2 ;;
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
		kernel_url="$release_url/unpacked/ubuntu-22.04-server-cloudimg-amd64-vmlinuz-generic"
		kernel_sha256="a62ee965bcca969a9f0249e4e3394d02ece84e55e5a4bd7d10ba74fda24b114c"
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

rootfs_path="$work_path/rootfs.squashfs"
rootfs_directory="$work_path/rootfs"
fetch "$rootfs_url" "$rootfs_sha256" "$rootfs_path"

vmlinuz_path="$work_path/vmlinuz"
fetch "$kernel_url" "$kernel_sha256" "$vmlinuz_path"
step "extract uncompressed vmlinux"
extract_vmlinux "$vmlinuz_path" "$kernel_path.part" || {
	echo "could not extract an ELF vmlinux from the Ubuntu kernel" >&2
	exit 1
}
mv "$kernel_path.part" "$kernel_path"
step "extracted $(basename "$kernel_path")"

step "extract root file system"
unsquashfs -q -d "$rootfs_directory" "$rootfs_path"

# Firecracker MMDS V2 does not accept the AWS IMDSv2 token headers that cloud-init sends.
cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-datasource.cfg" <<'EOF'
datasource_list: [ None ]
EOF

# Disable cloud-init networking. systemd-networkd owns the static guest network.
# Each VM can use this fixed address because each VM has a separate network namespace.
# The link route provides access to MMDS at 169.254.169.254.
cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-network.cfg" <<'EOF'
network: {config: disabled}
EOF

install -d -m 0755 "$rootfs_directory/etc/systemd/network"
cat > "$rootfs_directory/etc/systemd/network/10-atlas.network" <<'EOF'
[Match]
Name=eth0

[Network]
Address=172.16.0.2/24
Gateway=172.16.0.1
DNS=1.1.1.1

[Route]
Destination=169.254.169.254/32
Scope=link
EOF

install -d -m 0755 "$rootfs_directory/usr/local/lib/atlas"
cat > "$rootfs_directory/usr/local/lib/atlas/authorized-keys-command" <<'EOF'
#!/usr/bin/python3
from urllib.request import Request, urlopen

metadata_url = "http://169.254.169.254/latest"


def fetch(path, token, method="GET", headers=None):
	request_headers = {}
	if token:
		request_headers["X-metadata-token"] = token
	if headers:
		request_headers.update(headers)
	request = Request(f"{metadata_url}/{path}", method=method, headers=request_headers)
	return urlopen(request, timeout=3).read().decode().strip()


try:
	token = fetch("api/token", "", method="PUT", headers={"X-metadata-token-ttl-seconds": "21600"})
	for entry in fetch("meta-data/public-keys/", token).splitlines():
		index = entry.partition("=")[0]
		if index:
			print(fetch(f"meta-data/public-keys/{index}/openssh-key", token))
except Exception:
	pass
EOF
chmod 0755 "$rootfs_directory/usr/local/lib/atlas/authorized-keys-command"

install -d -m 0755 "$rootfs_directory/etc/ssh/sshd_config.d"
cat > "$rootfs_directory/etc/ssh/sshd_config.d/90-atlas-mmds.conf" <<'EOF'
AuthorizedKeysCommand /usr/local/lib/atlas/authorized-keys-command
AuthorizedKeysCommandUser nobody
EOF

step "create ext4 image"
truncate -s 4G "$image_path.part"
mkfs.ext4 -q -F -d "$rootfs_directory" "$image_path.part"
mv "$image_path.part" "$image_path"

echo "Built $image_path"
echo "Built $kernel_path"
