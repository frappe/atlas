#!/bin/bash
# Warm-restore e2e: flip the captured host signature of a warm snapshot
# (MODE=tamper) or flip it back (MODE=restore). Tampering makes the next warm
# clone's staged signature mismatch the live host, which MUST send it down the
# cold-boot fallback — the only way to exercise that path deterministically
# without waiting for DigitalOcean to live-migrate the droplet.
#
# Inputs:
#   MEMORY_DIRECTORY - the warm snapshot's durable /var/lib/atlas/snapshots/<id>
#   MODE             - tamper | restore

set -euo pipefail

: "${MEMORY_DIRECTORY:?}"
: "${MODE:?}"

signature="$MEMORY_DIRECTORY/host-signature.json"
sudo test -f "$signature"

case "$MODE" in
tamper)
	sudo sed -i 's/"kernel": "/"kernel": "tampered-/' "$signature"
	;;
restore)
	sudo sed -i 's/"kernel": "tampered-/"kernel": "/' "$signature"
	;;
*)
	echo "unknown MODE: $MODE" >&2
	exit 1
	;;
esac

sudo cat "$signature"
