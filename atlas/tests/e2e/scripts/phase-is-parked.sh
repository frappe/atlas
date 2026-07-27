#!/bin/bash
# Sleepy-VM e2e: assert a Sleeping VM is PARKED — i.e. the host still has the
# minimum reachability needed to trap an inbound TCP SYN, with no running guest
# (spec/32-sleepy-vms.md).
#
# phase7-is-sleeping.sh already covers the "VM is off" half (unit inactive,
# SLEEPING marker present). This covers the half that makes wake-on-TCP possible
# at all, and which is otherwise invisible until a connection silently fails:
#
#   1. the named counter `wake_<uuid-no-dashes>` exists in table inet atlas —
#      it is the flat surface atlas-wake-trap polls;
#   2. a rule in the `forward` chain references it, matching a bare SYN and
#      DROPping (not rejecting, so the client retransmits into the woken guest);
#   3. the VM's /128 routes out the shared atlas-park0 dummy, which is what makes
#      an inbound packet FORWARDED (so it reaches the rule) instead of being
#      input-delivered and consumed by the host;
#   4. atlas-wake-trap.service is actually running — the trap is inert without it.
#
# Inputs:
#   VIRTUAL_MACHINE_NAME  - the VM UUID.
#   VIRTUAL_MACHINE_IPV6  - its public /128.

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?}"
: "${VIRTUAL_MACHINE_IPV6:?}"

counter="wake_${VIRTUAL_MACHINE_NAME//-/}"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# The park happens in sleep-vm.py right after the unit stops, so give it a moment
# rather than racing the tail of the sleep Task.
for _ in $(seq 1 30); do
    if sudo nft list counter inet atlas "${counter}" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

sudo nft list counter inet atlas "${counter}" >/dev/null 2>&1 \
    || fail "named counter ${counter} missing — nothing for atlas-wake-trap to poll"

chain="$(sudo nft list chain inet atlas forward)"
printf '%s\n' "${chain}" | grep -q "${counter}" \
    || fail "no forward rule references ${counter} — an inbound SYN would not be counted"

rule="$(printf '%s\n' "${chain}" | grep "${counter}")"
printf '%s\n' "${rule}" | grep -q 'tcp flags syn' \
    || fail "wake rule is not SYN-only (ICMP/UDP would wake the VM): ${rule}"
printf '%s\n' "${rule}" | grep -q 'drop' \
    || fail "wake rule does not drop — the client would not retransmit: ${rule}"

route="$(ip -6 route show "${VIRTUAL_MACHINE_IPV6}/128" || true)"
printf '%s\n' "${route}" | grep -q 'atlas-park0' \
    || fail "/128 does not route out atlas-park0 (got '${route}') — inbound packets would not be forwarded"

systemctl is-active --quiet atlas-wake-trap.service \
    || fail "atlas-wake-trap.service is not active — the trap would never fire"

echo "OK parked ${VIRTUAL_MACHINE_NAME}"
