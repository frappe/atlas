#!/usr/bin/env bash

set -eu

: "${DATUM_URL:?DATUM_URL is required}"
: "${DATUM_TOKEN:?DATUM_TOKEN is required}"

config_file=/var/lib/metal/metald.toml

if [ "$(id -u)" -ne 0 ]; then
	echo "configure-metald-telemetry must run as root" >&2
	exit 1
fi

# install-metald.sh edits the same file under the same lock.
if [ ! -f "$config_file" ]; then
	echo "error: $config_file is missing; install metald first" >&2
	exit 1
fi
exec 9>"$config_file.lock"
flock 9
if ! grep -q '^\[telemetry\]' "$config_file"; then
	printf '\n[telemetry]\n' >> "$config_file"
fi
sed -i -e '/^datum_url[[:space:]]*=/d' -e '/^datum_token[[:space:]]*=/d' "$config_file"
sed -i "/^\[telemetry\]/a datum_url = \"${DATUM_URL%/}\"\ndatum_token = \"$DATUM_TOKEN\"" "$config_file"
flock -u 9

# metald reads the file again every 24 hours and when its token expires, so it needs no restart.
echo "==> metald sends telemetry to ${DATUM_URL%/}"
