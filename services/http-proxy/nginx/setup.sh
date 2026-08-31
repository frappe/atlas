#!/usr/bin/env bash

set -euo pipefail

OPENRESTY_VERSION="1.29.2.5"
OPENRESTY_PKG_RELEASE="1"

CONF_DIR="/etc/nginx"
HTML_DIR="/usr/share/nginx/html"
LUA_DIR="/etc/nginx/lua"
RUN_DIR="/run/nginx"
LOG_DIR="/var/log/nginx"
STATE_DIR="/var/lib/nginx"
SBIN_PATH="/usr/local/openresty/nginx/sbin/nginx"
SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export DEBIAN_FRONTEND=noninteractive

# Install the exact OpenResty package required by the proxy.
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://openresty.org/package/pubkey.gpg \
	| gpg --batch --yes --dearmor -o /usr/share/keyrings/openresty.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/openresty.gpg] https://openresty.org/package/ubuntu $(lsb_release -sc) main" \
	> /etc/apt/sources.list.d/openresty.list
apt-get update
OPENRESTY_PKG_VERSION="${OPENRESTY_VERSION}-${OPENRESTY_PKG_RELEASE}~$(lsb_release -sc)1"
apt-mark unhold openresty 2>/dev/null || true
apt-get install -y --reinstall --no-install-recommends "openresty=${OPENRESTY_PKG_VERSION}"
apt-get install -y --no-install-recommends python3 python3-venv
apt-mark hold openresty

INSTALLED_VERSION="$("$SBIN_PATH" -v 2>&1 | sed 's#.*openresty/##')"
if [ "$INSTALLED_VERSION" != "$OPENRESTY_VERSION" ]; then
	echo "FATAL: pinned OpenResty ${OPENRESTY_VERSION} but installed ${INSTALLED_VERSION}" >&2
	exit 1
fi
"$SBIN_PATH" -V 2>&1 | grep -q -- "--with-stream_ssl_preread_module" \
	|| { echo "FATAL: binary lacks stream_ssl_preread - the SNI front door needs it" >&2; exit 1; }
echo "installed stock OpenResty ${OPENRESTY_VERSION} (${OPENRESTY_PKG_VERSION})"

# Install the proxy files and create the unprivileged worker account.
if ! id -u nginx >/dev/null 2>&1; then
	useradd --system --no-create-home --shell /usr/sbin/nologin nginx
fi

# Keep runtime files under the package paths expected by the systemd units.
install -d "$CONF_DIR" "$LUA_DIR" "$HTML_DIR"
install -m 0644 "$SERVICE_DIR/nginx/nginx.conf"  "$CONF_DIR/nginx.conf"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/pages.lua"    "$LUA_DIR/pages.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/router.lua"   "$LUA_DIR/router.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/plain_router.lua" "$LUA_DIR/plain_router.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/admin.lua"    "$LUA_DIR/admin.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/persist.lua"  "$LUA_DIR/persist.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/acme_router.lua"  "$LUA_DIR/acme_router.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/domains_http_persist.lua" "$LUA_DIR/domains_http_persist.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/stream/sni_bridge.lua"    "$LUA_DIR/sni_bridge.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/stream/sni_router.lua"      "$LUA_DIR/sni_router.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/stream/sni_passthrough.lua" "$LUA_DIR/sni_passthrough.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/stream/sni_persist.lua"     "$LUA_DIR/sni_persist.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/http/unconfigured.lua"    "$LUA_DIR/unconfigured.lua"
install -m 0644 "$SERVICE_DIR/nginx/lua/domain_lookup.lua"        "$LUA_DIR/domain_lookup.lua"
install -m 0644 "$SERVICE_DIR/nginx/pages/not_found.html" "$HTML_DIR/not_found.html"
install -m 0644 "$SERVICE_DIR/nginx/pages/domain_unconfigured.html" "$HTML_DIR/domain_unconfigured.html"

# Install the control daemon into its own isolated Python environment.
install -d /opt/atlas/proxy-control
python3 -m venv /opt/atlas/proxy-control
/opt/atlas/proxy-control/bin/pip install --no-cache-dir "$SERVICE_DIR/control"

# Create runtime state, bootstrap authentication, and certificate files.
install -d -m 0750 /etc/atlas
install -m 0640 /dev/null /etc/atlas/proxy-control.htpasswd
install -d -o root -g nginx -m 0770 "$RUN_DIR"
install -d -m 0755 "$LOG_DIR"
install -d -m 0750 "$STATE_DIR/certs"
install -d -o root -g nginx -m 0770 "$STATE_DIR"
install -d -o root -g nginx -m 0750 "$STATE_DIR/acme"
# The placeholder certificate makes first boot possible before cloud-init pushes
# the real region and wildcard certificate.
: > "$STATE_DIR/region"
install -d -m 0750 "$STATE_DIR/certs/_placeholder"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
	-keyout "$STATE_DIR/certs/_placeholder/privkey.pem" \
	-out "$STATE_DIR/certs/_placeholder/fullchain.pem" \
	-subj "/CN=This domain is not connected to a site yet/O=Frappe Cloud/OU=Connect it in your dashboard: frappe.dev\/domains"
chmod 0640 "$STATE_DIR/certs/_placeholder/privkey.pem"
ln -sfn _placeholder/fullchain.pem "$STATE_DIR/certs/fullchain.pem"
ln -sfn _placeholder/privkey.pem   "$STATE_DIR/certs/privkey.pem"

# Install and enable both services. They start on the next boot.
install -d /etc/systemd/system/openresty.service.d
install -m 0644 "$SERVICE_DIR/nginx/systemd/openresty.service.d/atlas.conf" \
	/etc/systemd/system/openresty.service.d/atlas.conf
install -m 0644 "$SERVICE_DIR/nginx/systemd/atlas-proxy-control.service" \
	/etc/systemd/system/atlas-proxy-control.service
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl enable openresty.service
	systemctl enable atlas-proxy-control.service
else
	install -d /etc/systemd/system/multi-user.target.wants
	ln -sf /usr/lib/systemd/system/openresty.service \
		/etc/systemd/system/multi-user.target.wants/openresty.service
	ln -sf /usr/lib/systemd/system/atlas-proxy-control.service \
		/etc/systemd/system/multi-user.target.wants/atlas-proxy-control.service
fi

"$SBIN_PATH" -t -c "$CONF_DIR/nginx.conf"

echo "proxy stack built: stock OpenResty ${OPENRESTY_VERSION} (nginx + lua + stream-lua + headers-more, no local compile)."
