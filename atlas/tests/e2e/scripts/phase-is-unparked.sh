#!/bin/bash
# Sleepy-VM e2e: assert a woken VM is UNPARKED — every artefact phase-is-parked
# asserted is gone (spec/32-sleepy-vms.md).
#
# vm-network-up.py calls unpark() as its FIRST step, before rebuilding the real
# netns, so the client's retransmitted SYN reaches the resumed guest rather than
# hitting the trap again. If the rule or the off-link route survived the wake,
# the VM would be up but its traffic would still be dropped or blackholed out the
# dummy — which looks exactly like "the VM is broken" and is why this is asserted
# rather than assumed.
#
# A leftover counter also matters on its own: atlas-wake-trap skips a non-zero
# counter whose marker is gone, so the VM would not be re-woken, but the stale
# object would accumulate on the host for every sleep/wake cycle.
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

# The unpark is the first thing the starting unit does; allow for the Task
# returning slightly ahead of ExecStartPre completing.
for _ in $(seq 1 30); do
    if ! sudo nft list counter inet atlas "${counter}" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

! sudo nft list counter inet atlas "${counter}" >/dev/null 2>&1 \
    || fail "named counter ${counter} survived the wake"

! sudo nft list chain inet atlas forward | grep -q "${counter}" \
    || fail "forward chain still has a wake rule for ${counter} — live traffic would be dropped"

route="$(ip -6 route show "${VIRTUAL_MACHINE_IPV6}/128" || true)"
! printf '%s\n' "${route}" | grep -q 'atlas-park0' \
    || fail "/128 still routes out atlas-park0 (got '${route}') — the guest is unreachable"

echo "OK unparked ${VIRTUAL_MACHINE_NAME}"
