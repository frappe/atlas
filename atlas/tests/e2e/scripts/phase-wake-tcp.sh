#!/bin/bash
# Sleepy-VM e2e: open a TCP connection to a SLEEPING VM's /128 from an off-host
# vantage and assert it eventually answers — the wake-on-first-inbound-TCP path
# end to end (spec/32), and the only part of the feature a tenant ever sees.
#
# What is being proved: the first SYN is DROPped by the park rule (so this first
# connect attempt fails by design), the named counter goes non-zero,
# atlas-wake-trap notices within ~1s and starts the unit, vm-network-up unparks
# and vm-restore resumes the guest from its memory snapshot, and the client's
# retransmit — or one of our retries — lands on the live guest.
#
# Runs on a DIFFERENT host than the one holding the VM. A packet originating on
# the VM's own host would be input-delivered locally and never traverse
# `inet atlas forward`, so the trap would never fire and this would prove nothing.
#
# Inputs:
#   TARGET_IPV6    - the sleeping VM's /128.
#   TARGET_PORT    - port to dial (22; sshd is up in the guest once resumed).
#   TIMEOUT_SECONDS- how long to keep retrying before failing.

set -euo pipefail

: "${TARGET_IPV6:?}"
TARGET_PORT="${TARGET_PORT:-22}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-150}"

deadline=$((SECONDS + TIMEOUT_SECONDS))
attempt=0

while [ "$SECONDS" -lt "$deadline" ]; do
    attempt=$((attempt + 1))
    # -w bounds each attempt so a DROPped SYN cannot hang until the kernel gives
    # up; -z is connect-only (no payload), which is exactly one SYN handshake.
    if timeout 10 nc -6 -z -w 5 "${TARGET_IPV6}" "${TARGET_PORT}" 2>/dev/null; then
        echo "OK woke ${TARGET_IPV6}:${TARGET_PORT} after ${attempt} attempt(s), ${SECONDS}s"
        exit 0
    fi
    sleep 3
done

echo "FAIL: ${TARGET_IPV6}:${TARGET_PORT} never answered in ${TIMEOUT_SECONDS}s (${attempt} attempts) — the SYN trap did not wake the VM" >&2
exit 1
