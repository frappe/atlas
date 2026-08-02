#!/bin/bash
# Sleepy-VM e2e (stimulus, not an assertion): ICMP the target /128 from an
# off-host vantage. The caller asserts afterwards that the VM is STILL sleeping.
#
# The wake rule matches `tcp flags syn`, which implies TCP, so ICMP falls through
# the forward chain's policy accept, is forwarded out atlas-park0, and is
# discarded — no wake. That is the "TCP only" contract (spec/32), and it is worth
# probing because the failure mode is silent and expensive: any stray ping (a
# monitoring sweep, a neighbour discovery) would otherwise resurrect every
# sleeping VM on the host and quietly undo the whole feature.
#
# Runs on a DIFFERENT host than the one holding the VM: a packet from the VM's
# own host would be locally delivered and never traverse `inet atlas forward`.
#
# Always exits 0 — the absence of a wake is what the caller checks.
#
# Inputs:
#   TARGET_IPV6 - the sleeping VM's /128.

set -euo pipefail

: "${TARGET_IPV6:?}"

ping -6 -c 3 -W 2 "${TARGET_IPV6}" >/dev/null 2>&1 || true
echo "OK pinged ${TARGET_IPV6} (expecting NO wake)"
