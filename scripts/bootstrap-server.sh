#!/bin/bash
# Turn a fresh Ubuntu 24.04 host into a Firecracker host.
# Idempotent. Re-run after editing this file to roll forward.
#
# Inputs (environment variables):
#   FIRECRACKER_VERSION  - e.g. v1.15.1
#   ARCHITECTURE         - e.g. x86_64 (must match `uname -m`)

set -euo pipefail

: "${FIRECRACKER_VERSION:?required}"
: "${ARCHITECTURE:?required}"

if [ "$(uname -m)" != "$ARCHITECTURE" ]; then
    echo "Architecture mismatch: host is $(uname -m), expected $ARCHITECTURE" >&2
    exit 1
fi

# 1. KVM must be present.
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
    echo "/dev/kvm not available. Server must support nested virtualization." >&2
    exit 1
fi

# 2. Install packages.
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y \
    ca-certificates \
    curl \
    e2fsprogs \
    iproute2 \
    jq \
    nftables \
    squashfs-tools

# 3. Install Firecracker binary.
INSTALLED_VERSION="$(/usr/local/bin/firecracker --version 2>/dev/null | head -n1 | awk '{print $2}' || true)"
WANTED_VERSION="${FIRECRACKER_VERSION#v}"
if [ "$INSTALLED_VERSION" != "$WANTED_VERSION" ]; then
    cd /tmp
    sudo rm -rf firecracker-install
    mkdir firecracker-install
    cd firecracker-install
    curl -fsSL \
        "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-${ARCHITECTURE}.tgz" \
        | tar -xz
    sudo install -m 0755 "release-${FIRECRACKER_VERSION}-${ARCHITECTURE}/firecracker-${FIRECRACKER_VERSION}-${ARCHITECTURE}" \
        /usr/local/bin/firecracker
    cd /tmp
    rm -rf firecracker-install
fi

# 4. IPv6 forwarding and neighbor proxy, plus IPv4 forwarding for NAT44 egress.
#    IPv6 is the guest's public address; IPv4 is egress-only via masquerade
#    (see step 5 and spec/06-networking.md).
sudo install -m 0644 /dev/stdin /etc/sysctl.d/60-atlas.conf <<'CONF'
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.forwarding = 1
net.ipv6.conf.all.proxy_ndp = 1
net.ipv4.ip_forward = 1
CONF
sudo sysctl --system >/dev/null

# 5. nftables scaffold. Two-shot: create-if-missing, then ensure chains exist.
#    One inet table holds both the v6 forward chain and the v4 egress NAT.
sudo nft list table inet atlas >/dev/null 2>&1 || sudo nft add table inet atlas
sudo nft list chain inet atlas forward >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas forward { type filter hook forward priority filter; policy accept; }"

# 5a. IPv4 egress: masquerade the per-VM private /30s (carved from
#     100.64.0.0/16) out the host's public uplink. One host-wide rule covers
#     every VM — the source range is fixed, so no per-VM NAT churn. The guest
#     is reachable from outside over IPv6 only; this gives it *outbound* v4.
uplink="$(ip -j route show default | jq -r '.[0].dev')"
sudo nft list chain inet atlas postrouting >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas postrouting { type nat hook postrouting priority srcnat; policy accept; }"
sudo nft list chain inet atlas postrouting | grep -q "ip saddr 100.64.0.0/16" || \
    sudo nft add rule inet atlas postrouting ip saddr 100.64.0.0/16 oifname "$uplink" masquerade

# 6. Directories.
sudo install -d -m 0700 /var/lib/atlas
sudo install -d -m 0700 /var/lib/atlas/images
sudo install -d -m 0700 /var/lib/atlas/virtual-machines
sudo install -d -m 0700 /var/lib/atlas/run
sudo install -d -m 0755 /var/lib/atlas/bin

# 7. Helper scripts and systemd unit are uploaded alongside this script by
#    the caller, into /var/lib/atlas/bin/ and /etc/systemd/system/. See
#    spec/03-bootstrapping.md for the exact list. scp preserves source perms,
#    so set the executable bit here to be safe — systemd invokes these
#    directly via ExecStartPost / ExecStopPost.
sudo chmod 0755 /var/lib/atlas/bin/*.sh
sudo systemctl daemon-reload

# 8. Record state for Atlas to pick up. Single JSON file is the canonical
#    source of truth; the trailing `cat` keeps the same bytes on stdout so
#    operators tailing the Task can still see the values.
sudo install -d -m 0755 /var/lib/atlas
sudo jq -nc \
    --arg firecracker_version "$(/usr/local/bin/firecracker --version | head -n1 | awk '{print $2}')" \
    --arg kernel_version "$(uname -r)" \
    --arg architecture "$(uname -m)" \
    '{firecracker_version: $firecracker_version,
      kernel_version: $kernel_version,
      architecture: $architecture}' \
    | sudo tee /var/lib/atlas/bootstrap.json >/dev/null

cat /var/lib/atlas/bootstrap.json
