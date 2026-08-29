#!/usr/bin/env bash
# Host-wide networking for metal VMs: forwarding + NAT of the VM transit subnets
# out the uplink. Run as root, once. Per-VM netns/veth wiring is done by metald.
set -euo pipefail

UPLINK=${UPLINK:?set UPLINK to the internet-facing interface, e.g. eth0}

sysctl -q -w net.ipv4.ip_forward=1

# idempotent: add the MASQUERADE rule only if it isn't already present
if ! iptables -t nat -C POSTROUTING -s 10.0.0.0/8 -o "$UPLINK" -j MASQUERADE 2>/dev/null; then
	iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o "$UPLINK" -j MASQUERADE
fi

echo "forwarding + NAT ready (transit 10.0.0.0/8 -> $UPLINK)"
