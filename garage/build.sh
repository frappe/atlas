#!/usr/bin/env bash
set -euo pipefail

GARAGE_VERSION="${GARAGE_VERSION:-v2.3.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="$(uname -m)"

case "$ARCH" in
  x86_64|amd64) GO_ARCH=amd64; RELEASE_ARCH=x86_64 ;;
  aarch64|arm64) GO_ARCH=arm64; RELEASE_ARCH=aarch64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl nginx

rm -rf /etc/nginx/sites-enabled/default

curl -fsSL \
  "https://garagehq.deuxfleurs.fr/_releases/${GARAGE_VERSION}/${RELEASE_ARCH}-unknown-linux-musl/garage" \
  -o /usr/local/bin/garage
chmod 0755 /usr/local/bin/garage

install -m 0644 "$ROOT/guest/garage.service" /etc/systemd/system/garage.service
test -s /etc/systemd/system/garage.service
systemctl daemon-reload
/usr/local/bin/garage --version
sync
# sleep 5 so the sync works properly
sleep 5
