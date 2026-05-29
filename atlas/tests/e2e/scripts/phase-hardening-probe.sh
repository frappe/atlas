#!/bin/bash
# Hardening e2e: read back the host-hardening state bootstrap-server.sh applies.
# Asserts both that the CIS controls are present AND that the three deliberate
# deviations hold (forwarding on, squashfs NOT blocklisted, root key-login kept).
# Fail-loud: any missing/wrong control exits non-zero and fails the Task.
#
# Each check prints the OBSERVED value before asserting, so a failure shows
# what the host actually had — not just which assertion tripped.
set -euo pipefail

fail() { echo "HARDENING FAIL: $1" >&2; exit 1; }
note() { echo "  [probe] $1"; }

# --- sshd: key-only root, password auth off (CIS 5.1; deviation: NOT `no`) ---
sshd_config="$(sudo sshd -T)"
permitroot="$(echo "$sshd_config" | grep -E '^permitrootlogin ' || echo '<none>')"
passauth="$(echo "$sshd_config" | grep -E '^passwordauthentication ' || echo '<none>')"
emptypass="$(echo "$sshd_config" | grep -E '^permitemptypasswords ' || echo '<none>')"
maxauth="$(echo "$sshd_config" | grep -E '^maxauthtries ' || echo '<none>')"
note "sshd: $permitroot | $passauth | $emptypass | $maxauth"
# `sshd -T` canonicalizes `prohibit-password` to its synonym `without-password`;
# accept either token (both mean key-only root, no password login).
echo "$sshd_config" | grep -qE "^permitrootlogin (prohibit-password|without-password)$" \
    || fail "PermitRootLogin is not prohibit-password/without-password (got: $permitroot)"
echo "$sshd_config" | grep -qx "passwordauthentication no" \
    || fail "PasswordAuthentication is not no (got: $passauth)"
echo "$sshd_config" | grep -qx "permitemptypasswords no" \
    || fail "PermitEmptyPasswords is not no (got: $emptypass)"
echo "$sshd_config" | grep -qx "maxauthtries 4" \
    || fail "MaxAuthTries is not 4 (got: $maxauth)"
echo "sshd OK (prohibit-password, no password auth, MaxAuthTries 4)"

# --- forwarding deviation: MUST stay on or every VM goes dark (CIS 3.3.1) ---
v6fwd="$(cat /proc/sys/net/ipv6/conf/all/forwarding)"
v4fwd="$(cat /proc/sys/net/ipv4/ip_forward)"
note "forwarding: ipv6.all=$v6fwd ipv4.ip_forward=$v4fwd (both must be 1)"
[ "$v6fwd" = "1" ] || fail "ipv6 forwarding is off ($v6fwd) — VM networking would be dead"
[ "$v4fwd" = "1" ] || fail "ipv4 forwarding is off ($v4fwd) — NAT44 egress would be dead"
echo "forwarding deviation OK (ipv6=$v6fwd, ipv4=$v4fwd)"

# --- a sample CIS 3.3 sysctl is actually applied ---
redir="$(cat /proc/sys/net/ipv4/conf/all/accept_redirects)"
synck="$(cat /proc/sys/net/ipv4/tcp_syncookies)"
note "cis sysctls: accept_redirects=$redir tcp_syncookies=$synck"
[ "$redir" = "0" ] || fail "net.ipv4.conf.all.accept_redirects is not 0 (got: $redir)"
[ "$synck" = "1" ] || fail "net.ipv4.tcp_syncookies is not 1 (got: $synck)"
echo "network sysctls OK (accept_redirects=0, tcp_syncookies=1)"

# --- module blocklist: an unused module is blocked; squashfs is NOT ---
# Capture into a var first: under `set -o pipefail`, modprobe -n's exit code
# would otherwise leak into the pipeline and produce a false result.
dccp_probe="$(sudo modprobe -n -v dccp 2>&1 || true)"
squashfs_probe="$(sudo modprobe -n -v squashfs 2>&1 || true)"
note "modprobe dccp -> $dccp_probe"
note "modprobe squashfs -> $squashfs_probe"
case "$dccp_probe" in *"/bin/false"*) ;; *) fail "dccp is not blocklisted (got: $dccp_probe)" ;; esac
# squashfs deviation: unsquashfs needs it, so it must remain loadable.
case "$squashfs_probe" in
    *"/bin/false"*) fail "squashfs is blocklisted — image sync would break" ;;
esac
echo "module blocklist OK (dccp blocked, squashfs kept)"

# --- unattended security updates enabled (CIS 1.2.2.1) ---
note "unattended-upgrades: config $([ -f /etc/apt/apt.conf.d/60-atlas-unattended.conf ] && echo present || echo MISSING), pkg $(dpkg -s unattended-upgrades >/dev/null 2>&1 && echo installed || echo MISSING)"
test -f /etc/apt/apt.conf.d/60-atlas-unattended.conf \
    || fail "unattended-upgrades config missing"
dpkg -s unattended-upgrades >/dev/null 2>&1 \
    || fail "unattended-upgrades package not installed"
echo "unattended-upgrades OK"

# --- KSM off (no cross-VM memory side channel) when KSM is present ---
if [ -r /sys/kernel/mm/ksm/run ]; then
    ksm="$(cat /sys/kernel/mm/ksm/run)"
    note "ksm: run=$ksm (must be 0)"
    [ "$ksm" = "0" ] || fail "KSM is running (got: $ksm)"
    echo "KSM OK (off)"
else
    note "ksm: /sys/kernel/mm/ksm/run absent"
    echo "KSM OK (not present)"
fi

echo "HARDENING PROBE OK"
