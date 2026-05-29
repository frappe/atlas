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
#    A freshly-booted cloud image still has cloud-init / unattended-upgrades
#    running its own apt for the first minutes, holding the apt locks. apt's
#    `DPkg::Lock::Timeout` does NOT cover the `apt-get update` *lists* lock
#    (/var/lib/apt/lists/lock) on this apt version, so update failed fast with
#    "Could not get lock" and left fresh droplets Broken. Wait for cloud-init
#    to finish and the locks to clear before touching apt at all.
export DEBIAN_FRONTEND=noninteractive

# cloud-init owns the first-boot apt run; block until it's done (best-effort —
# `status --wait` returns promptly if cloud-init isn't present or already done).
sudo cloud-init status --wait >/dev/null 2>&1 || true

# Belt-and-suspenders: poll the apt/dpkg locks in case unattended-upgrades or a
# late apt timer still holds them after cloud-init reports done. Cap the wait so
# a genuinely stuck lock still surfaces as a bootstrap failure rather than hang.
wait_for_apt_locks() {
    local deadline=$(( SECONDS + 300 ))
    while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "apt/dpkg lock still held after 300s; aborting bootstrap" >&2
            return 1
        fi
        echo "waiting for apt/dpkg lock to be released..." >&2
        sleep 5
    done
}
wait_for_apt_locks

sudo apt-get -o DPkg::Lock::Timeout=300 update
sudo apt-get -o DPkg::Lock::Timeout=300 install -y \
    ca-certificates \
    curl \
    e2fsprogs \
    iproute2 \
    jq \
    nftables \
    squashfs-tools

# 3. Install Firecracker + jailer binaries. Both ship in the same release
#    tarball; production runs every VM under the jailer (de-privileged, chrooted,
#    cgroup-isolated), so we install both. Gate on EITHER binary being absent or
#    at the wrong version, so a host bootstrapped before the jailer existed picks
#    it up on re-run.
INSTALLED_FIRECRACKER="$(/usr/local/bin/firecracker --version 2>/dev/null | head -n1 | awk '{print $2}' || true)"
INSTALLED_JAILER="$(/usr/local/bin/jailer --version 2>/dev/null | head -n1 | awk '{print $2}' || true)"
WANTED_VERSION="${FIRECRACKER_VERSION#v}"
if [ "$INSTALLED_FIRECRACKER" != "$WANTED_VERSION" ] || [ "$INSTALLED_JAILER" != "$WANTED_VERSION" ]; then
    cd /tmp
    sudo rm -rf firecracker-install
    mkdir firecracker-install
    cd firecracker-install
    curl -fsSL \
        "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-${ARCHITECTURE}.tgz" \
        | tar -xz
    sudo install -m 0755 "release-${FIRECRACKER_VERSION}-${ARCHITECTURE}/firecracker-${FIRECRACKER_VERSION}-${ARCHITECTURE}" \
        /usr/local/bin/firecracker
    sudo install -m 0755 "release-${FIRECRACKER_VERSION}-${ARCHITECTURE}/jailer-${FIRECRACKER_VERSION}-${ARCHITECTURE}" \
        /usr/local/bin/jailer
    cd /tmp
    rm -rf firecracker-install
fi

# 4. Kernel/network sysctls: VM-networking essentials + CIS 3.3 hardening.
#    The forwarding + proxy_ndp lines are LOAD-BEARING for the routed-tap VM
#    networking model (each VM is a per-VM tap, no bridge; the host routes
#    eth0<->tap, which IS forwarding). IPv6 is the guest's public address;
#    IPv4 forwarding backs the NAT44 egress masquerade (step 9a). CIS 3.3.1
#    says disable forwarding — we DO NOT, by design (both families), see
#    spec/03-bootstrapping.md "Host hardening". Blast radius is contained at
#    the `inet atlas` nft chains, not here. The remaining lines are CIS 3.3
#    controls that a routing host still wants.
sudo install -m 0644 /dev/stdin /etc/sysctl.d/60-atlas.conf <<'CONF'
# --- VM networking (required; deliberate CIS 3.3.1 deviation, v4 + v6) ---
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.forwarding = 1
net.ipv6.conf.all.proxy_ndp = 1
net.ipv4.ip_forward = 1

# --- CIS 3.3 network hardening (compatible with a routing host) ---
# 3.3.5/3.3.6 a hostile guest must not inject routes via ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
# 3.3.2 we are not a redirect-sending router
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
# 3.3.8 source-routed packets are a spoofing vector
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0
# 3.3.9 log spoofed/source-routed/redirect martians
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1
# 3.3.3 ignore bogus ICMP error responses
net.ipv4.icmp_ignore_bogus_error_responses = 1
# 3.3.4 ignore broadcast ICMP (smurf)
net.ipv4.icmp_echo_ignore_broadcasts = 1
# 3.3.10 SYN cookies blunt SYN floods
net.ipv4.tcp_syncookies = 1
# 3.3.11 guests use static addressing, not SLAAC; host must not accept RAs
net.ipv6.conf.all.accept_ra = 0
net.ipv6.conf.default.accept_ra = 0
# NOTE: rp_filter (CIS 3.3.7) is intentionally omitted — strict reverse-path
# filtering can drop the asymmetric traffic of the routed-tap topology. It is
# gated on a live-bench check before being added (see llm/state/plan.md).
CONF
sudo sysctl --system >/dev/null

# 5. sshd hardening (CIS 5.1). A drop-in so we never edit the stock config and
#    survive package upgrades. Atlas connects key-only as root, so these only
#    tighten what is already true. PermitRootLogin stays `prohibit-password`
#    (NOT `no`) — there is no unprivileged user yet and Atlas SSHes as root;
#    `no` would lock Atlas out of every server (deliberate CIS 5.1.20
#    deviation, see spec/03-bootstrapping.md "Host hardening").
sudo install -m 0644 /dev/stdin /etc/ssh/sshd_config.d/60-atlas.conf <<'CONF'
# 5.1.20 key-only root (NOT `no`: Atlas operates as root, no unpriv user yet)
PermitRootLogin prohibit-password
# turn "we happen to use keys" into "the server refuses anything else"
PasswordAuthentication no
KbdInteractiveAuthentication no
# 5.1.19 no empty-password accounts may log in
PermitEmptyPasswords no
# 5.1.16 cap auth attempts per connection
MaxAuthTries 4
# 5.1.13 drop slow/abandoned pre-auth connections
LoginGraceTime 60
# 5.1.7 reap dead sessions. Probes are answered by the client's ssh transport
# at the protocol layer (independent of task output), so long silent tasks
# (e.g. apt-get, ~1800s) stay connected; 300x3=900s only reaps a dead client.
ClientAliveInterval 300
ClientAliveCountMax 3
# 5.1.6/5.1.15/5.1.12 modern algorithms only (sets a default OpenSSH client
# negotiates — the e2e SSH connecting at all is the regression guard).
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com,umac-128-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org,diffie-hellman-group16-sha512,diffie-hellman-group18-sha512
CONF
# Validate BEFORE reload so a bad drop-in can never brick SSH (fail loud).
sudo sshd -t
sudo systemctl reload ssh

# 6. Kernel-module blocklist (CIS 1.1.1 filesystems + 3.2 network protocols).
#    `install <m> /bin/false` defeats modprobe; `blacklist` covers autoload.
#    squashfs is DELIBERATELY ABSENT — unsquashfs unpacks the rootfs image, so
#    blocklisting it would break image sync (deliberate CIS 1.1.1.7 deviation).
#    We never blocklist load-bearing modules: tun/tap (VM taps), kvm/kvm_intel/
#    kvm_amd (Firecracker), vhost/vhost_net (virtio), nf_tables/nft_* (firewall).
sudo install -m 0644 /dev/stdin /etc/modprobe.d/60-atlas-blocklist.conf <<'CONF'
# 1.1.1.x unused filesystem modules
install cramfs /bin/false
install freevxfs /bin/false
install hfs /bin/false
install hfsplus /bin/false
install jffs2 /bin/false
install udf /bin/false
install usb-storage /bin/false
blacklist cramfs
blacklist freevxfs
blacklist hfs
blacklist hfsplus
blacklist jffs2
blacklist udf
blacklist usb-storage
# 3.2.x unused network-protocol modules (remote attack surface)
install dccp /bin/false
install tipc /bin/false
install rds /bin/false
install sctp /bin/false
blacklist dccp
blacklist tipc
blacklist rds
blacklist sctp
CONF

# 7. Automatic security updates (CIS 1.2.2.1). Security pocket ONLY — we do not
#    want a feature kernel rolling under a running Firecracker host. No auto
#    reboot: a security kernel needs a reboot to take effect, but an unattended
#    reboot would kill every running VM; the operator reboots on a window.
sudo apt-get -o DPkg::Lock::Timeout=300 install -y unattended-upgrades
sudo install -m 0644 /dev/stdin /etc/apt/apt.conf.d/60-atlas-unattended.conf <<'CONF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

# 8. Firecracker host controls (prod-host-setup.md): no cross-VM memory side
#    channel, no guest RAM on disk. Both idempotent; guarded for absence.
#    KSM (page dedup across VMs) is a side channel — turn it off if present.
if [ -w /sys/kernel/mm/ksm/run ]; then
    echo 0 | sudo tee /sys/kernel/mm/ksm/run >/dev/null
fi
# Swap lets guest memory hit disk (data remanence). DO droplets are typically
# swapless; swapoff -a is idempotent and a no-op when there is no swap.
sudo swapoff -a

# 9. nftables scaffold. Two-shot: create-if-missing, then ensure chains exist.
#    One inet table holds both the v6 forward chain and the v4 egress NAT.
sudo nft list table inet atlas >/dev/null 2>&1 || sudo nft add table inet atlas
sudo nft list chain inet atlas forward >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas forward { type filter hook forward priority filter; policy accept; }"

# 9a. IPv4 egress: masquerade the per-VM private /30s (carved from
#     100.64.0.0/16) out the host's public uplink. One host-wide rule covers
#     every VM — the source range is fixed, so no per-VM NAT churn. The guest
#     is reachable from outside over IPv6 only; this gives it *outbound* v4.
uplink="$(ip -j route show default | jq -r '.[0].dev')"
sudo nft list chain inet atlas postrouting >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas postrouting { type nat hook postrouting priority srcnat; policy accept; }"
sudo nft list chain inet atlas postrouting | grep -q "ip saddr 100.64.0.0/16" || \
    sudo nft add rule inet atlas postrouting ip saddr 100.64.0.0/16 oifname "$uplink" masquerade

# 10. Directories.
sudo install -d -m 0700 /var/lib/atlas
sudo install -d -m 0700 /var/lib/atlas/images
sudo install -d -m 0700 /var/lib/atlas/virtual-machines
sudo install -d -m 0700 /var/lib/atlas/run
sudo install -d -m 0755 /var/lib/atlas/bin

# 11. Helper scripts and systemd unit are uploaded alongside this script by
#     the caller, into /var/lib/atlas/bin/ and /etc/systemd/system/. See
#     spec/03-bootstrapping.md for the exact list. scp preserves source perms,
#     so set the executable bit here to be safe — systemd invokes these
#     directly via ExecStartPost / ExecStopPost.
sudo chmod 0755 /var/lib/atlas/bin/*.sh
sudo systemctl daemon-reload

# 12. Record state for Atlas to pick up. Single JSON file is the canonical
#     source of truth; the trailing `cat` keeps the same bytes on stdout so
#     operators tailing the Task can still see the values.
sudo install -d -m 0755 /var/lib/atlas
sudo jq -nc \
    --arg firecracker_version "$(/usr/local/bin/firecracker --version | head -n1 | awk '{print $2}')" \
    --arg jailer_version "$(/usr/local/bin/jailer --version | head -n1 | awk '{print $2}')" \
    --arg kernel_version "$(uname -r)" \
    --arg architecture "$(uname -m)" \
    '{firecracker_version: $firecracker_version,
      jailer_version: $jailer_version,
      kernel_version: $kernel_version,
      architecture: $architecture}' \
    | sudo tee /var/lib/atlas/bootstrap.json >/dev/null

cat /var/lib/atlas/bootstrap.json
