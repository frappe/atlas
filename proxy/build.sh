#!/usr/bin/env bash
# Build the Atlas reverse proxy stack — run INSIDE a freshly-provisioned Ubuntu
# guest (proxy-design.md §3.1). Compiles vanilla nginx + OpenResty luajit2 +
# lua-nginx-module from pinned sources, installs the committed conf/lua/html and
# the guest unit at the stock Ubuntu `nginx`-package paths (/usr/sbin/nginx,
# /etc/nginx, /var/log/nginx, …), and enables nginx.service. The built VM is then
# snapshotted by Atlas — that snapshot is the reusable "proxy image".
#
# This is the AUTHORITATIVE build. The docker-compose test harness (proxy/test)
# runs this same script so the tested stack and the shipped stack are identical.
#
# Idempotent (spec taste #14: retry = re-run). Re-running rebuilds from the
# pinned sources and reinstalls; already-present source tarballs are reused.
#
# Run as root. Reads the committed tree from the directory this script lives in.

set -euo pipefail

# --- Pinned versions (proxy-design.md §3.1; verified released + mutually
# compatible against nginx 1.30.x). Bumping any of these is a deliberate stack
# update rolled as a new proxy snapshot. ---
NGINX_VERSION="1.30.2"
LUAJIT2_REF="v2.1-20250529"          # OpenResty's fork (NOT upstream LuaJIT)
LUA_NGINX_MODULE_VERSION="0.10.29"
NDK_VERSION="0.3.4"                   # ngx_devel_kit — MUST precede lua module
LUA_RESTY_CORE_VERSION="0.1.32"      # mandatory — nginx won't start without it
                                     # (0.1.33 was never cut as a stable tag —
                                     # only RCs exist; 0.1.32 is the last stable)
LUA_RESTY_LRUCACHE_VERSION="0.15"    # dependency of lua-resty-core
LUA_CJSON_VERSION="2.1.0.14"         # cjson C module — NOT bundled with vanilla
                                     # nginx (it ships in the OpenResty distro we
                                     # deliberately don't use); persist/admin need it
HEADERS_MORE_VERSION="0.39"          # more_set_headers

# --- Paths mirror the stock Ubuntu/Debian `nginx` package EXACTLY (verified
# against nginx-common 1.24.0-2ubuntu7.12: `nginx -V` + the deb file manifest), so
# an engineer debugging the guest finds everything where `apt install nginx` would
# put it AND `nginx -V` shows the same configure paths: binary /usr/sbin/nginx,
# --prefix /usr/share/nginx, config /etc/nginx, logs /var/log/nginx, pid
# /run/nginx.pid, temp/state dirs under /var/lib/nginx. The only app-specific
# additions live under clearly-nginx-named dirs (the Lua modules in /etc/nginx/lua,
# the admin socket in /run/nginx, the live map + region + certs in /var/lib/nginx)
# — no /opt, no bespoke prefix. ---
PREFIX="/usr/share/nginx"            # matches the deb's --prefix (html/ lives here)
SBIN_PATH="/usr/sbin/nginx"
CONF_DIR="/etc/nginx"
HTML_DIR="/usr/share/nginx/html"
LUA_DIR="/etc/nginx/lua"
RUN_DIR="/run/nginx"                  # admin socket dir (pid is /run/nginx.pid)
LOG_DIR="/var/log/nginx"
STATE_DIR="/var/lib/nginx"           # deb temp dirs (body/…) + our map.json/region/certs/acme
BUILD_DIR="/usr/local/src/nginx-build"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- 1. Build toolchain. nginx needs PCRE2, zlib, OpenSSL headers; luajit2
# needs a C toolchain. curl to fetch the pinned tarballs. ---
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
	build-essential ca-certificates curl \
	libpcre2-dev zlib1g-dev libssl-dev

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# fetch <url> <output> — download once, reuse on re-run.
fetch() {
	local url="$1" out="$2"
	if [ -f "$out" ]; then
		echo "  reuse $out"
		return
	fi
	echo "  fetch $url"
	curl -fsSL --output "$out.part" "$url"
	mv "$out.part" "$out"
}

# --- 2. OpenResty luajit2. The Lua module REQUIRES this fork, not upstream
# LuaJIT. Install to /usr/local; nginx links against it via rpath. ---
fetch "https://github.com/openresty/luajit2/archive/refs/tags/${LUAJIT2_REF}.tar.gz" "luajit2.tar.gz"
rm -rf "luajit2-src"
mkdir luajit2-src
tar -xzf luajit2.tar.gz -C luajit2-src --strip-components=1
make -C luajit2-src -j"$(nproc)"
make -C luajit2-src install
ldconfig

# --- 3. nginx source + the modules (NDK before lua-nginx-module). ---
fetch "https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz" "nginx.tar.gz"
fetch "https://github.com/vision5/ngx_devel_kit/archive/refs/tags/v${NDK_VERSION}.tar.gz" "ndk.tar.gz"
fetch "https://github.com/openresty/lua-nginx-module/archive/refs/tags/v${LUA_NGINX_MODULE_VERSION}.tar.gz" "lua-nginx-module.tar.gz"
fetch "https://github.com/openresty/headers-more-nginx-module/archive/refs/tags/v${HEADERS_MORE_VERSION}.tar.gz" "headers-more.tar.gz"

for pair in "nginx.tar.gz:nginx" "ndk.tar.gz:ndk" \
	"lua-nginx-module.tar.gz:lua-nginx-module" "headers-more.tar.gz:headers-more"; do
	tarball="${pair%%:*}"
	dir="${pair##*:}"
	rm -rf "$dir"
	mkdir "$dir"
	tar -xzf "$tarball" -C "$dir" --strip-components=1
done

# --- 4. Configure + build nginx. Order matters: NDK before lua-nginx-module.
# The rpath is load-bearing — without it nginx can't find libluajit-5.1.so. ---
cd "$BUILD_DIR/nginx"
# The path flags (prefix/conf/pid/lock/log + the four *-temp-path dirs) are copied
# verbatim from the Ubuntu nginx deb's `nginx -V`, so our `nginx -V` shows the same
# layout an apt-installed nginx would. (We add the Lua modules + omit the deb's
# dynamic/mail/stream modules we don't use; those don't move any path.) ONE
# deliberate deviation: the deb compiles --error-log-path=stderr and only sends
# errors to /var/log/nginx/error.log via its nginx.conf `error_log` line; we make
# that the compile default too, so a startup error BEFORE the config is parsed
# still lands in the file an engineer greps (our nginx.conf sets the same path).
LUAJIT_LIB=/usr/local/lib LUAJIT_INC=/usr/local/include/luajit-2.1 \
./configure \
	--prefix="$PREFIX" \
	--sbin-path="$SBIN_PATH" \
	--conf-path="$CONF_DIR/nginx.conf" \
	--pid-path=/run/nginx.pid \
	--lock-path=/var/lock/nginx.lock \
	--error-log-path="$LOG_DIR/error.log" \
	--http-log-path="$LOG_DIR/access.log" \
	--http-client-body-temp-path="$STATE_DIR/body" \
	--http-fastcgi-temp-path="$STATE_DIR/fastcgi" \
	--http-proxy-temp-path="$STATE_DIR/proxy" \
	--http-scgi-temp-path="$STATE_DIR/scgi" \
	--http-uwsgi-temp-path="$STATE_DIR/uwsgi" \
	--with-http_v2_module \
	--with-http_ssl_module \
	--with-http_realip_module \
	--with-ld-opt="-Wl,-rpath,/usr/local/lib" \
	--add-module="$BUILD_DIR/ndk" \
	--add-module="$BUILD_DIR/lua-nginx-module" \
	--add-module="$BUILD_DIR/headers-more"
make -j"$(nproc)"
make install

# --- 5. Pure-Lua resty libs. NOT compiled into nginx — nginx loads them at
# runtime from /usr/local/share/lua/5.1 (lua_package_path in nginx.conf).
# lua-resty-core is MANDATORY: nginx refuses to start without it. ---
cd "$BUILD_DIR"
fetch "https://github.com/openresty/lua-resty-core/archive/refs/tags/v${LUA_RESTY_CORE_VERSION}.tar.gz" "lua-resty-core.tar.gz"
fetch "https://github.com/openresty/lua-resty-lrucache/archive/refs/tags/v${LUA_RESTY_LRUCACHE_VERSION}.tar.gz" "lua-resty-lrucache.tar.gz"
for pair in "lua-resty-core.tar.gz:lua-resty-core" "lua-resty-lrucache.tar.gz:lua-resty-lrucache"; do
	tarball="${pair%%:*}"
	dir="${pair##*:}"
	rm -rf "$dir"
	mkdir "$dir"
	tar -xzf "$tarball" -C "$dir" --strip-components=1
	make -C "$dir" install LUA_LIB_DIR=/usr/local/share/lua/5.1
done

# --- 5b. lua-cjson C module. NOT bundled with vanilla nginx — it ships in the
# OpenResty distribution we deliberately don't use. Built against luajit2's
# headers; installs cjson.so into /usr/local/lib/lua/5.1 (on the default cpath).
# persist.lua and admin.lua require("cjson.safe"); without this nginx crashes at
# init_by_lua. ---
fetch "https://github.com/openresty/lua-cjson/archive/refs/tags/${LUA_CJSON_VERSION}.tar.gz" "lua-cjson.tar.gz"
rm -rf "lua-cjson"
mkdir "lua-cjson"
tar -xzf "lua-cjson.tar.gz" -C "lua-cjson" --strip-components=1
make -C "lua-cjson" LUA_INCLUDE_DIR=/usr/local/include/luajit-2.1
make -C "lua-cjson" install
ldconfig

# --- 6. Install the committed stack: conf, lua, html — at the stock nginx paths
# (/etc/nginx, /usr/share/nginx/html). These are the SAME files the test harness
# exercises, so green compose == the guest's behavior. ---
install -d "$CONF_DIR" "$LUA_DIR" "$HTML_DIR"
install -m 0644 "$SRC_DIR/conf/nginx.conf"  "$CONF_DIR/nginx.conf"
install -m 0644 "$SRC_DIR/conf/mime.types"  "$CONF_DIR/mime.types"
install -m 0644 "$SRC_DIR/lua/router.lua"   "$LUA_DIR/router.lua"
install -m 0644 "$SRC_DIR/lua/admin.lua"    "$LUA_DIR/admin.lua"
install -m 0644 "$SRC_DIR/lua/persist.lua"  "$LUA_DIR/persist.lua"
install -m 0644 "$SRC_DIR/html/not_found.html" "$HTML_DIR/not_found.html"

# --- 7. Runtime dirs + cert layout, all under the stock nginx state/run/log dirs
# (/var/lib/nginx, /run/nginx, /var/log/nginx). Certs are region-scoped on disk
# (certs/<region>/{fullchain,privkey}.pem — Atlas pushes them there, §7.3), but
# nginx's static ssl_certificate can't interpolate the region, so it reads a flat
# certs/{fullchain,privkey}.pem SYMLINK that points into the active region's dir.
# build.sh doesn't know the real region yet (build_proxy writes it afterwards and
# repoints the symlink), so the placeholder lives under a "_placeholder" region
# and the flat symlinks point at it — enough for nginx -t and a first boot before
# Atlas pushes the real wildcard. ---
install -d -m 0750 "$RUN_DIR"
install -d -m 0755 "$LOG_DIR"
install -d -m 0750 "$STATE_DIR" "$STATE_DIR/certs" "$STATE_DIR/acme"
: > "$STATE_DIR/region"
install -d -m 0750 "$STATE_DIR/certs/_placeholder"
if [ ! -f "$STATE_DIR/certs/_placeholder/fullchain.pem" ]; then
	openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
		-keyout "$STATE_DIR/certs/_placeholder/privkey.pem" \
		-out "$STATE_DIR/certs/_placeholder/fullchain.pem" \
		-subj "/CN=nginx-placeholder"
	chmod 0640 "$STATE_DIR/certs/_placeholder/privkey.pem"
fi
# Point the flat path nginx reads at the placeholder region (repointed by
# build_proxy once the real region is known). -n so we replace the symlink
# itself, not follow it into the target dir on a re-run.
ln -sfn _placeholder/fullchain.pem "$STATE_DIR/certs/fullchain.pem"
ln -sfn _placeholder/privkey.pem   "$STATE_DIR/certs/privkey.pem"

# --- 8. Guest unit + tmpfiles, named `nginx` so `systemctl status nginx` /
# `journalctl -u nginx` work by reflex. Enable but do not start (this may be a
# chroot / container build with no live systemd). ---
install -m 0644 "$SRC_DIR/guest/nginx.service" /etc/systemd/system/nginx.service
install -d /etc/tmpfiles.d
install -m 0644 "$SRC_DIR/guest/tmpfiles.d/nginx.conf" /etc/tmpfiles.d/nginx.conf
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl enable nginx.service
else
	# No live systemd (Docker build): enable by symlink so a real boot starts it.
	ln -sf /etc/systemd/system/nginx.service \
		/etc/systemd/system/multi-user.target.wants/nginx.service
fi

# --- 9. Validate the config compiles. The smoke test the build itself can do. ---
"$SBIN_PATH" -t -c "$CONF_DIR/nginx.conf"

echo "nginx proxy stack built: nginx ${NGINX_VERSION} + lua-nginx-module ${LUA_NGINX_MODULE_VERSION}."
