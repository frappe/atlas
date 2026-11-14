#!/usr/bin/env bash

set -eu

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
echo "==> smartmontools and RAID logging installed"
