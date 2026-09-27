#!/usr/bin/env bash
# Install the datum write token bundle for this host and its VMs.

set -eu

: "${DATUM_TOKEN_BUNDLE:?DATUM_TOKEN_BUNDLE is required}"

bundle_path=/var/lib/metal/datum-tokens.json

if [ "$(id -u)" -ne 0 ]; then
	echo "install-datum-tokens must run as root" >&2
	exit 1
fi

install -d -m 0755 "$(dirname "$bundle_path")"

staged_bundle=$(mktemp "$bundle_path.staged.XXXXXX")
trap 'rm -f "$staged_bundle"' EXIT

printf '%s' "$DATUM_TOKEN_BUNDLE" > "$staged_bundle"
chmod 0600 "$staged_bundle"
mv -f "$staged_bundle" "$bundle_path"

# metald reads this file fresh on every export pass, so no restart or signal follows.
