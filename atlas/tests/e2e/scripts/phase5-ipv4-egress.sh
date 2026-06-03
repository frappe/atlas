#!/bin/bash
# Phase 5 e2e: SSH into a provisioned VM over its public IPv6 and prove the
# guest reaches the internet over BOTH families (spec/06-networking.md):
#
#   IPv4 egress (NAT44):
#   - eth0 has the derived private v4 (100.64.x.x/30)
#   - a v4 default route exists (via the tap's host side)
#   - the guest can reach a v4-ONLY destination (curl to an IPv4 literal,
#     which forces the v4 path and needs no DNS) — proves masquerade end to end
#
#   IPv6 egress (routed, public per-VM /128):
#   - the guest can reach a v6-ONLY destination (curl to an IPv6 literal,
#     which forces the v6 path and needs no DNS) — proves the routed-tap
#     egress and host forwarding end to end
#
#   DNS (system resolver):
#   - the guest resolves a hostname through getaddrinfo() — the path apt uses.
#     The two egress checks above use literals on purpose, so they pass even
#     when /etc/resolv.conf is hijacked by systemd-resolved (the 127.0.0.53
#     stub with zero upstreams). This check is the one that fails in that case:
#     `getent hosts` exercises the real resolver, not `dig @<literal>`.
#
# We hop in over IPv6 (the guest's only inbound path) and run all checks
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

# 4. Reach a v6-ONLY destination. 2606:4700:4700::1111 is an IPv6 literal
#    (the v6 analog of 1.1.1.1), forcing the v6 egress path with no DNS —
#    proving the routed-tap path and host IPv6 forwarding end to end.
curl -6 --max-time 15 -sS -o /dev/null 'https://[2606:4700:4700::1111]/' \
    || fail "curl -6 to 2606:4700:4700::1111 failed (IPv6 egress not working)"

# 5. Resolve a hostname through the SYSTEM resolver (getaddrinfo), the path apt
#    uses. Guards the systemd-resolved hijack: if /etc/resolv.conf is the
#    127.0.0.53 stub with no upstreams, the literal-based checks above still
#    pass but this fails — exactly the "ping works, apt update doesn't" report.
getent hosts deb.debian.org >/dev/null \
    || fail "getent hosts deb.debian.org failed (DNS broken — check /etc/resolv.conf is the Cloudflare v6 line, not the 127.0.0.53 resolved stub)"

echo "OK ipv4-egress"
echo "OK ipv6-egress"
echo "OK dns-resolution"
REMOTE
