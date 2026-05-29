#!/bin/bash
# Symmetric teardown for vm-network-up.sh. Invoked by ExecStopPost on the
# systemd unit. Idempotent: missing rules and devices are not an error.

set -euo pipefail

virtual_machine_name="${1:?virtual machine name required}"
network_env="/var/lib/atlas/virtual-machines/${virtual_machine_name}/network.env"

# If the env file is gone (terminate-vm already ran) we still want to do our
# best to clean up. Try to source, but accept absence.
if [ -f "$network_env" ]; then
    . "$network_env"
fi

uplink="$(ip -j -6 route show default | jq -r '.[0].dev' 2>/dev/null || true)"

if [ -n "${VIRTUAL_MACHINE_IPV6:-}" ] && [ -n "$uplink" ]; then
    sudo ip -6 neigh del proxy "$VIRTUAL_MACHINE_IPV6" dev "$uplink" 2>/dev/null || true
fi

if [ -n "${TAP_DEVICE:-}" ]; then
    sudo ip -6 route del "${VIRTUAL_MACHINE_IPV6}/128" dev "$TAP_DEVICE" 2>/dev/null || true
    # Deleting the tap also drops its IPv4 /30 host address, so there is no
    # separate v4 addr teardown. The masquerade rule is host-wide (matches the
    # whole 100.64.0.0/16 source), so it is intentionally NOT removed per-VM —
    # it stays for the next VM, exactly like the v6 forward chain scaffold.
    sudo ip link del "$TAP_DEVICE" 2>/dev/null || true
fi

# Delete the two nft rules by handle. Look them up by VM IPv6.
if [ -n "${VIRTUAL_MACHINE_IPV6:-}" ]; then
    handles="$(sudo nft -a list chain inet atlas forward 2>/dev/null \
        | awk -v ip="$VIRTUAL_MACHINE_IPV6" '$0 ~ ip {print $NF}')"
    for handle in $handles; do
        sudo nft delete rule inet atlas forward handle "$handle" 2>/dev/null || true
    done
fi
