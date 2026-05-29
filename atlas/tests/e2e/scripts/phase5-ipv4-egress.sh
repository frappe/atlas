#!/bin/bash
# Phase 5 e2e: SSH into a provisioned VM over its public IPv6 and prove the
# NAT44 IPv4 egress path (spec/06-networking.md "IPv4 egress (NAT44)") works:
#
#   - eth0 has the derived private v4 (100.64.x.x/30)
#   - a v4 default route exists (via the tap's host side)
#   - the guest can reach a v4-ONLY destination (curl to an IPv4 literal,
#     which forces the v4 path and needs no DNS) — proves masquerade end to end
#
# We hop in over IPv6 (the guest's only inbound path) and run the v4 checks
# from inside the guest.
#
# Inputs:
#   VIRTUAL_MACHINE_IPV6  - destination address for the SSH hop.
#   SSH_PRIVATE_KEY       - private half of the key Atlas injected.

set -euo pipefail
# Disable bash -x tracing: SSH_PRIVATE_KEY is in scope and any expansion would
# trace the key into stderr, which we capture into the Task row.
{ set +x; } 2>/dev/null

: "${VIRTUAL_MACHINE_IPV6:?}"
: "${SSH_PRIVATE_KEY:?}"

key_file="$(mktemp /tmp/atlas-ipv4-probe-XXXXXX.key)"
trap 'rm -f "$key_file"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" >"$key_file"
chmod 0600 "$key_file"

# Wait for sshd in the guest (first boot regenerates host keys).
deadline=$((SECONDS + 90))
while ! ssh \
        -i "$key_file" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes \
        -o ConnectTimeout=5 \
        "root@${VIRTUAL_MACHINE_IPV6}" true 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "guest ssh not ready after 90s at ${VIRTUAL_MACHINE_IPV6}" >&2
        exit 1
    fi
    sleep 3
done

guest() {
    ssh \
        -i "$key_file" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "root@${VIRTUAL_MACHINE_IPV6}" "$@"
}

# One round trip, one clean failure point.
guest bash -s <<'REMOTE'
set -euo pipefail

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# 1. eth0 has the derived private v4 from the 100.64.0.0/16 supernet.
ip -4 -o addr show dev eth0 scope global | grep -q '100\.64\.' \
    || fail "eth0 has no 100.64.x.x egress address: $(ip -4 -o addr show dev eth0)"

# 2. A v4 default route exists (via the tap's host side).
ip -4 route show default | grep -q 'default via 100\.64\.' \
    || fail "no IPv4 default route via 100.64.x.x: $(ip -4 route show default)"

# 3. Reach a v4-ONLY destination. 1.1.1.1 is an IPv4 literal, so this forces
#    the v4 egress path with no DNS involved — proving the host masquerade.
curl -4 --max-time 15 -sS -o /dev/null https://1.1.1.1/ \
    || fail "curl -4 to 1.1.1.1 failed (NAT44 egress not working)"

echo "OK ipv4-egress"
REMOTE
