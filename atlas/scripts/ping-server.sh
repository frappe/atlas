#!/usr/bin/env bash

set -euo pipefail

echo "Server is up and running!"
uptime
echo "Current user: $(whoami)"
echo "OS Information:"
cat /etc/os-release
echo "Listening sockets:"
ss -ltnu
echo "Host firewall: $(systemctl is-active atlas-host-firewall.service || true)"
nft list table inet atlas_host 2>/dev/null || echo "table inet atlas_host is absent"
