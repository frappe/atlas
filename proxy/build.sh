#!/usr/bin/env bash
# Build the Atlas reverse proxy stack — run INSIDE a freshly-provisioned Ubuntu
# guest (proxy-design.md §3.1). Compiles vanilla nginx + OpenResty luajit2 +
# lua-nginx-module from pinned sources, installs the committed conf/lua/html and
# the guest unit, and enables atlas-proxy.service. The built VM is then
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

PREFIX="/opt/atlas-proxy"
BUILD_DIR="/usr/local/src/atlas-proxy-build"
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
LUAJIT_LIB=/usr/local/lib LUAJIT_INC=/usr/local/include/luajit-2.1 \
./configure \
	--prefix="$PREFIX" \
	--sbin-path="$PREFIX/sbin/nginx" \
	--conf-path="$PREFIX/conf/nginx.conf" \
	--pid-path=/run/atlas-proxy/nginx.pid \
	--error-log-path=/var/log/atlas-proxy/error.log \
	--http-log-path=/var/log/atlas-proxy/access.log \
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

# --- 6. Install the committed stack: conf, lua, html. These are the SAME files
# the test harness exercises, so green compose == the guest's behavior. ---
install -d "$PREFIX/conf" "$PREFIX/lua" "$PREFIX/html"
install -m 0644 "$SRC_DIR/conf/nginx.conf"  "$PREFIX/conf/nginx.conf"
install -m 0644 "$SRC_DIR/conf/mime.types"  "$PREFIX/conf/mime.types"
install -m 0644 "$SRC_DIR/lua/router.lua"   "$PREFIX/lua/router.lua"
install -m 0644 "$SRC_DIR/lua/admin.lua"    "$PREFIX/lua/admin.lua"
install -m 0644 "$SRC_DIR/lua/persist.lua"  "$PREFIX/lua/persist.lua"
install -m 0644 "$SRC_DIR/html/not_found.html" "$PREFIX/html/not_found.html"

# --- 7. Runtime dirs. The cert dir gets a self-signed placeholder so nginx -t
# and a first boot succeed before Atlas pushes the real wildcard (§7.3). ---
install -d -m 0750 /run/atlas-proxy
install -d -m 0755 /var/log/atlas-proxy
install -d -m 0750 /var/lib/atlas-proxy /var/lib/atlas-proxy/certs /var/lib/atlas-proxy/acme
: > /var/lib/atlas-proxy/region
if [ ! -f /var/lib/atlas-proxy/certs/fullchain.pem ]; then
	openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
		-keyout /var/lib/atlas-proxy/certs/privkey.pem \
		-out /var/lib/atlas-proxy/certs/fullchain.pem \
		-subj "/CN=atlas-proxy-placeholder"
	chmod 0640 /var/lib/atlas-proxy/certs/privkey.pem
fi

# --- 8. Guest unit + tmpfiles. Enable but do not start (this may be a chroot /
# container build with no live systemd). ---
install -m 0644 "$SRC_DIR/guest/atlas-proxy.service" /etc/systemd/system/atlas-proxy.service
install -d /etc/tmpfiles.d
install -m 0644 "$SRC_DIR/guest/tmpfiles.d/atlas-proxy.conf" /etc/tmpfiles.d/atlas-proxy.conf
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl enable atlas-proxy.service
else
	# No live systemd (Docker build): enable by symlink so a real boot starts it.
	ln -sf /etc/systemd/system/atlas-proxy.service \
		/etc/systemd/system/multi-user.target.wants/atlas-proxy.service
fi

# --- 9. Validate the config compiles. The smoke test the build itself can do. ---
"$PREFIX/sbin/nginx" -t -c "$PREFIX/conf/nginx.conf"

echo "Atlas proxy stack built: nginx ${NGINX_VERSION} + lua-nginx-module ${LUA_NGINX_MODULE_VERSION}."
