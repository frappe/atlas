#!/usr/bin/env bash

set -eu

vector_version=0.59.0
vector_amd64_sha256=1dd9f52bda25d2364ab04d496a3b7edbc775b1a3cd02d9985eb81f3c174a245f
vector_arm64_sha256=5779afdc9abf08373c09b6656c933c8e1da34926c9b646cd80e76d96d7d64133

if [ "$(id -u)" -ne 0 ]; then
	echo "install-vector must run as root" >&2
	exit 1
fi

if [ "$(vector --version 2>/dev/null | cut -d' ' -f2)" != "$vector_version" ]; then
	echo "==> install Vector $vector_version"
	architecture=$(dpkg --print-architecture)
	case "$architecture" in
		amd64) expected_sha256=$vector_amd64_sha256 ;;
		arm64) expected_sha256=$vector_arm64_sha256 ;;
		*) echo "Vector has no package for $architecture" >&2; exit 1 ;;
	esac
	package=$(mktemp --suffix=.deb)
	trap 'rm -f "$package"' EXIT
	curl -fsSL -o "$package" "https://github.com/vectordotdev/vector/releases/download/v$vector_version/vector_$vector_version-1_$architecture.deb"
	echo "$expected_sha256  $package" | sha256sum -c --quiet
	apt-get install -y -qq "$package"
fi
usermod -aG systemd-journal vector
# The package unit stops restarting after 5 failures in 10 seconds.
install -d /etc/systemd/system/vector.service.d
printf '[Service]\nStartLimitInterval=0\nRestartSec=5\n' > /etc/systemd/system/vector.service.d/restart.conf
systemctl daemon-reload

if ! command -v smartctl >/dev/null; then
	echo "==> install smartmontools"
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends smartmontools
fi

# --syslog enables RAID monitoring without a mail address.
if command -v mdadm >/dev/null && grep -q '^md' /proc/mdstat; then
	install -d /etc/systemd/system/mdmonitor.service.d /etc/systemd/system/mdmonitor-oneshot.service.d
	printf '[Service]\nExecStart=\nExecStart=/usr/sbin/mdadm --monitor --scan --syslog\n' > /etc/systemd/system/mdmonitor.service.d/syslog.conf
	printf '[Service]\nExecStart=\nExecStart=/usr/sbin/mdadm --monitor --oneshot --scan --syslog\n' > /etc/systemd/system/mdmonitor-oneshot.service.d/syslog.conf
	systemctl daemon-reload
	systemctl reset-failed mdmonitor.service mdmonitor-oneshot.service
	systemctl restart mdmonitor.service
fi
echo "==> Vector, smartmontools and RAID logging installed"
