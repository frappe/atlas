#!/usr/bin/env bash

set -eu

# The Scaleway Ubuntu image replaces Ubuntu's cloud-init-base with its own
# cloud-init build, but leaves /etc/logrotate.d/cloud-init-base behind. Both
# files rotate the same logs, so logrotate fails every day.
if [ "$(dpkg-query -W -f='${Status}' cloud-init-base 2>/dev/null)" = "deinstall ok config-files" ]; then
	DEBIAN_FRONTEND=noninteractive dpkg --purge cloud-init-base
fi
