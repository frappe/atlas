#!/usr/bin/env bash
set -euo pipefail

: "${SERVER_HOSTNAME:?SERVER_HOSTNAME is required}"

echo "==> set hostname to $SERVER_HOSTNAME"
hostnamectl set-hostname "$SERVER_HOSTNAME"

if [ -d /etc/cloud/cloud.cfg.d ]; then
	printf 'preserve_hostname: true\n' > /etc/cloud/cloud.cfg.d/99-atlas-hostname.cfg
fi
