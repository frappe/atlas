#!/bin/bash
# Warm-restore e2e: read identity + boot facts from a guest over its public
# IPv6 and print them as FACT_* lines for the controller to parse. The facts
# are what the fan-out contract is judged on: two warm clones must DIFFER in
# hostname/machine-id/host key (the freshen regenerated identity) while
# SHARING one boot_id (both resumed the same frozen instant — a cold boot
# would mint a fresh boot_id), and /etc/atlas-vm-uuid must equal each clone's
# own uuid (the freshen completed for that VM).
#
# Inputs:
#   VIRTUAL_MACHINE_IPV6  - destination address for the SSH probe.
#   SSH_PRIVATE_KEY       - private half of a key in the guest's authorized_keys.
#   WAIT_SECONDS          - optional ssh-up deadline (default 180).

set -euo pipefail
# Disable bash -x tracing: SSH_PRIVATE_KEY is in scope and any expansion would
# trace the key into stderr, which the Task row captures (same guard as
# phase5-guest-identity.sh).
{ set +x; } 2>/dev/null

: "${VIRTUAL_MACHINE_IPV6:?}"
: "${SSH_PRIVATE_KEY:?}"

key_file="$(mktemp /tmp/atlas-warm-facts-XXXXXX.key)"
trap 'rm -f "$key_file"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" >"$key_file"
chmod 0600 "$key_file"

guest() {
	ssh \
		-i "$key_file" \
		-o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		-o ConnectTimeout=5 \
		-o BatchMode=yes \
		"root@${VIRTUAL_MACHINE_IPV6}" "$@"
}

# A warm clone is reachable only after its in-guest freshen brought the
# clone's addresses up; a cold-fallback clone after a full boot. Poll.
deadline=$((SECONDS + ${WAIT_SECONDS:-180}))
until guest true 2>/dev/null; do
	if ((SECONDS >= deadline)); then
		echo "guest ${VIRTUAL_MACHINE_IPV6} not reachable over SSH" >&2
		exit 1
	fi
	sleep 2
done

echo "FACT_HOSTNAME=$(guest hostname)"
echo "FACT_MACHINE_ID=$(guest cat /etc/machine-id)"
echo "FACT_BOOT_ID=$(guest cat /proc/sys/kernel/random/boot_id)"
echo "FACT_HOST_KEY=$(guest cat /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')"
echo "FACT_ATLAS_VM_UUID=$(guest cat /etc/atlas-vm-uuid 2>/dev/null || echo missing)"
