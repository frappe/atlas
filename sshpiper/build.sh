#!/usr/bin/env bash
set -euo pipefail

SSHPIPER_VERSION="${SSHPIPER_VERSION:-v1.5.4}"
GO_VERSION="${GO_VERSION:-1.26.4}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="$(uname -m)"

case "$ARCH" in
  x86_64|amd64) GO_ARCH=amd64; RELEASE_ARCH=x86_64 ;;
  aarch64|arm64) GO_ARCH=arm64; RELEASE_ARCH=aarch64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl openssh-server tar

curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" -o /tmp/go.tgz
rm -rf /usr/local/go
tar -C /usr/local -xzf /tmp/go.tgz
PATH="/usr/local/go/bin:$PATH" go -C "$ROOT" build -trimpath -o /usr/local/bin/sshpiper-atlas .

curl -fsSL \
  "https://github.com/tg123/sshpiper/releases/download/${SSHPIPER_VERSION}/sshpiperd_with_plugins_linux_${RELEASE_ARCH}.tar.gz" \
  -o /tmp/sshpiper.tgz
mkdir -p /tmp/sshpiper-release
tar -C /tmp/sshpiper-release -xzf /tmp/sshpiper.tgz
install -m 0755 /tmp/sshpiper-release/sshpiperd /usr/local/bin/sshpiperd

install -d -m 0700 /etc/atlas
install -m 0644 "$ROOT/guest/sshpiper.service" /etc/systemd/system/sshpiper.service
install -d -m 0755 /etc/ssh/sshd_config.d
install -m 0644 "$ROOT/guest/60-atlas-sshpiper.conf" /etc/ssh/sshd_config.d/60-atlas-sshpiper.conf
test -s /etc/systemd/system/sshpiper.service
test -s /etc/ssh/sshd_config.d/60-atlas-sshpiper.conf
test "$(sshd -T | awk '$1 == "port" { print $2; exit }')" = "222"
sshd -t
systemctl daemon-reload
systemctl disable --now ssh.socket ssh.service && systemctl enable --now ssh.service
/usr/local/bin/sshpiperd --version
/usr/local/bin/sshpiper-atlas --help >/dev/null
rm -rf /tmp/go.tgz /tmp/sshpiper.tgz /tmp/sshpiper-release /usr/local/go
sync
# sleep 5 so the sync works properly
sleep 5
