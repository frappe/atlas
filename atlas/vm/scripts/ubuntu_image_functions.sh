# Shared artifact and guest setup functions for the Ubuntu image recipes.

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

	# zstd rejects trailing bzImage data after writing the kernel; validate the ELF header.
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

# DefaultDependencies=no lets metadata run before cloud-init completes. Ordering
# this unit before cloud-init creates a cycle that prevents SSH host key generation.
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

package_rootfs() {
	step "create ext4 image"
	truncate -s 4G "$image_path.part"

	# Remount read-only at the first file system error, so a damaged disk stops taking writes.
	mkfs.ext4 -q -F -e remount-ro -d "$rootfs_directory" "$image_path.part"
	mv "$image_path.part" "$image_path"

	step "compress ext4 image"
	zstd -q -T0 -3 -f -o "$image_path.zst" "$image_path"

	echo "Built $image_path and $image_path.zst"
	echo "Built $kernel_path"
}
