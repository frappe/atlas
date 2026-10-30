#!/usr/bin/env bash
# Build the Ubuntu rescue image for Metal: the server image steps, plus the repair tools and menu.
set -euo pipefail

output=""
kernel_output=""
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_directory/ubuntu_image_functions.sh"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[[ -n $output && -n $kernel_output ]] || { echo "--output and --kernel-output are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run this builder with root permissions" >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo "rescue builds require an amd64 build host" >&2; exit 2; }
command -v chroot >/dev/null || { echo "missing command: chroot" >&2; exit 1; }

series=noble
series_url="https://cloud-images.ubuntu.com/releases/$series"
rootfs_name="ubuntu-24.04-server-cloudimg-amd64"
kernel_package=linux-image-generic-hwe-24.04

install_repair_packages() {
	# Package scripts must not start daemons in the build host's namespaces.
	cat > "$rootfs_directory/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
	chmod 0755 "$rootfs_directory/usr/sbin/policy-rc.d"
	rm -f "$rootfs_directory/etc/resolv.conf"
	cp /etc/resolv.conf "$rootfs_directory/etc/resolv.conf"

	chroot "$rootfs_directory" env TMPDIR=/tmp apt-get update
	chroot "$rootfs_directory" env TMPDIR=/tmp DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		python3 whiptail rsync ncdu dnsutils tcpdump e2fsprogs xfsprogs btrfs-progs util-linux parted lvm2 mdadm \
		cryptsetup-bin curl ca-certificates openssh-server vim-tiny
	chroot "$rootfs_directory" env TMPDIR=/tmp apt-get clean

	rm "$rootfs_directory/usr/sbin/policy-rc.d" "$rootfs_directory/etc/resolv.conf"
	ln -s ../run/systemd/resolve/stub-resolv.conf "$rootfs_directory/etc/resolv.conf"
}

# The menu opens at login. The message of the day shows only the two lines from rescue-motd.
install_repair_menu() {
	install -D -m 0755 "$script_directory/guest/atlas-rescue" "$rootfs_directory/usr/local/sbin/atlas-rescue"
	install -D -m 0755 "$script_directory/guest/atlas-rescue-menu" "$rootfs_directory/usr/local/sbin/atlas-rescue-menu"
	install -D -m 0755 "$script_directory/guest/rescue-menu-screen" "$rootfs_directory/usr/local/lib/atlas/rescue-menu-screen"
	install -D -m 0755 "$script_directory/guest/rescue.py" "$rootfs_directory/usr/local/lib/atlas/rescue.py"
	cat > "$rootfs_directory/etc/profile.d/atlas-rescue.sh" <<'PROFILE'
if [ -t 0 ] && [ -t 1 ] && [ -z "${ATLAS_RESCUE_MENU_SHOWN:-}" ]; then
  export ATLAS_RESCUE_MENU_SHOWN=1
  /usr/local/sbin/atlas-rescue-menu
fi
PROFILE
	install -D -m 0644 "$script_directory/guest/rescue-motd" "$rootfs_directory/etc/motd"
	find "$rootfs_directory/etc/update-motd.d" -mindepth 1 -delete
}

# The serial console has a temporary root password, because the VM's own login may be what is broken.
# Its client is the Atlas web terminal, so it does not need the vt220 default of a serial getty.
install_rescue_console() {
	install -D -m 0755 "$script_directory/guest/rescue-console-password" "$rootfs_directory/usr/local/lib/atlas/rescue-console-password"
	cat > "$rootfs_directory/etc/systemd/system/atlas-rescue-console.service" <<'SERVICE'
[Unit]
Description=Prepare rescue console credentials
After=cloud-config.service
Wants=cloud-config.service
Before=serial-getty@ttyS0.service
[Service]
Type=oneshot
ExecStart=/usr/local/lib/atlas/rescue-console-password
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
SERVICE
	mkdir -p "$rootfs_directory/etc/systemd/system/serial-getty@ttyS0.service.d"
	cat > "$rootfs_directory/etc/systemd/system/serial-getty@ttyS0.service.d/atlas-rescue.conf" <<'SERVICE'
[Unit]
Requires=atlas-rescue-console.service
After=atlas-rescue-console.service
[Service]
Environment=TERM=xterm-256color
SERVICE
	ln -sf /etc/systemd/system/atlas-rescue-console.service "$rootfs_directory/etc/systemd/system/multi-user.target.wants/atlas-rescue-console.service"
}

# SSH accepts only the VM's keys.
install_rescue_ssh() {
	cat > "$rootfs_directory/etc/ssh/sshd_config.d/00-atlas-rescue.conf" <<'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSH
}

# Cloud-init must not mount or resize the original VM disk.
install_rescue_cloud_init() {
	cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-rescue.cfg" <<'EOF'
mounts: []
growpart:
  mode: "off"
resize_rootfs: false
ssh:
  emit_keys_to_console: false
no_ssh_fingerprints: true
EOF
}

# Snap seeding can block cloud-final without Internet access.
# These services are not needed here, and a namespaced one can keep the VM disk mounted.
mask_background_services() {
	local unit
	for unit in snapd.service snapd.socket snapd.seeded.service snapd.autoimport.service fwupd.service \
		fwupd-refresh.timer apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service motd-news.timer; do
		ln -sf /dev/null "$rootfs_directory/etc/systemd/system/$unit"
	done
}

# Package installation can create host identities. Each guest must generate its own.
reset_host_identities() {
	rm -f "$rootfs_directory"/etc/ssh/ssh_host_* "$rootfs_directory/var/lib/dbus/machine-id"
	: > "$rootfs_directory/etc/machine-id"
	ln -s /etc/machine-id "$rootfs_directory/var/lib/dbus/machine-id"
}

prepare_build
build_rootfs

install_repair_packages
install_repair_menu
install_rescue_console
install_rescue_ssh
install_rescue_cloud_init
mask_background_services
reset_host_identities

package_rootfs
