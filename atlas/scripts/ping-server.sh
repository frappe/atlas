#!/usr/bin/env bash

set -euo pipefail

echo "Server is up and running!"
uptime
echo "Current user: $(whoami)"
echo "OS Information:"
cat /etc/os-release
