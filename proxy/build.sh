#!/usr/bin/env bash
# Build the Atlas reverse proxy stack — run INSIDE a freshly-provisioned Ubuntu
# guest (proxy-design.md §3.1). Installs the stock nginx binary from the official
# nginx.org apt repo, then compiles ONLY the modules apt cannot supply (OpenResty
# luajit2 + the Lua/headers-more nginx modules, as dynamic .so's built against the
# exact installed nginx), installs the committed conf/lua/html and the guest unit
# at the stock `nginx`-package paths (/usr/sbin/nginx, /etc/nginx, /var/log/nginx,
# …), and enables nginx.service. The built VM is then snapshotted by Atlas — that
# snapshot is the reusable "proxy image".
#
# Why apt for the base, source for the modules: installing the real nginx from
# nginx.org's repo gives us a signed apt transaction that OWNS the stock paths,
# makes `nginx -V` genuinely truthful, ships current stable nginx + an OpenSSL we
# don't hand-build, and keeps the base off the C toolchain. The modules (luajit2,
# lua-nginx, headers-more) ship in NO apt repo, so they stay compiled — but as
# dynamic modules (`--add-dynamic-module`, `--with-compat`) loaded by the apt
# binary via `load_module` in nginx.conf. We own the frozen, mutually-compatible
# MODULE set; apt owns the base binary + OpenSSL version.
#
# This is the AUTHORITATIVE build. The docker-compose test harness (proxy/test)
# runs this same script so the tested stack and the shipped stack are identical.
#
# Idempotent (spec taste #14: retry = re-run). Re-running reinstalls the held apt
# nginx and rebuilds the modules from the pinned sources; already-present source
# tarballs are reused.
#
# Run as root. Reads the committed tree from the directory this script lives in.

set -euo pipefail

# --- Pinned versions (proxy-design.md §3.1; verified released + mutually
# compatible). EVERYTHING the binary is made of is pinned, so two bakes a year
# apart produce the same stack: the apt nginx base AND our compiled modules.
# Bumping any of these is a deliberate stack update rolled as a new proxy snapshot.
#
# The nginx BASE is pinned to an exact nginx.org package version (NOT floated to
# "whatever stable is latest"), because the dynamic modules below are compiled
# against this exact nginx source — a base bump without a matching module rebuild
# is exactly the incompatible-binary case we refuse to ship. The nginx.org repo
# keeps old stable versions (apt-cache madison lists 1.26→1.30), so this pin stays
# installable across releases; if it ever can't be served, the `apt install
# nginx=<pin>` below fails loud rather than silently installing a different base.
NGINX_VERSION="1.30.3"               # nginx.org STABLE (even minor); base binary + OpenSSL
NGINX_PKG_RELEASE="1"                # the "-N~<codename>" deb revision (bump for a repackage)
LUAJIT2_REF="v2.1-20250529"          # OpenResty's fork (NOT upstream LuaJIT)
LUA_NGINX_MODULE_VERSION="0.10.29"
# stream-lua-nginx-module is the L4 sibling of lua-nginx-module (spec/17-tcp-proxy.md):
# a SEPARATE module — both must be compiled in — for the TCP forwarder's stream{}
# Lua (preread router + line-protocol admin). The version is NOT free to pick: the
# pinned lua-resty-core (0.1.32) asserts an EXACT subsystem version at startup —
# its base.lua requires ngx_stream_lua_module == 0.0.17 (and ngx_http_lua_module
# == 0.10.29), not ">=". So 0.0.17 is the stream tag that matches the already-
# pinned resty-core + lua-nginx-module 0.10.29 set; a newer stream-lua (e.g.
# 0.0.19rc4) compiles fine but nginx then ALERTS "ngx_stream_lua_module 0.0.17
# required" and refuses to start. The compose release gate caught this version
# lock — bumping any one of the three is a coordinated stack update, rolled as a
# new proxy snapshot, same discipline as the rest of the pins.
STREAM_LUA_MODULE_REF="v0.0.17"
NDK_VERSION="0.3.4"                   # ngx_devel_kit — MUST precede both lua modules
LUA_RESTY_CORE_VERSION="0.1.32"      # mandatory — nginx won't start without it
                                     # (0.1.33 was never cut as a stable tag —
                                     # only RCs exist; 0.1.32 is the last stable)
LUA_RESTY_LRUCACHE_VERSION="0.15"    # dependency of lua-resty-core
LUA_CJSON_VERSION="2.1.0.14"         # cjson C module — NOT bundled with vanilla
                                     # nginx (it ships in the OpenResty distro we
                                     # deliberately don't use); persist/admin need it
HEADERS_MORE_VERSION="0.39"          # more_set_headers

# --- Paths are the stock nginx.org/Debian `nginx` package paths. apt OWNS these
# now (binary /usr/sbin/nginx, --prefix /usr/share/nginx, config /etc/nginx, logs
# /var/log/nginx, pid /run/nginx.pid); we only ADD app-specific bits under
# clearly-nginx-named dirs (Lua modules in /etc/nginx/lua, the dynamic .so's in
# /etc/nginx/modules, the admin socket in /run/nginx, the live map + region +
# certs in /var/lib/nginx). No /opt, no bespoke prefix. ---
CONF_DIR="/etc/nginx"
HTML_DIR="/usr/share/nginx/html"
LUA_DIR="/etc/nginx/lua"
MODULES_DIR="/etc/nginx/modules"      # dynamic .so's live here (load_module reads it)
SBIN_PATH="/usr/sbin/nginx"
RUN_DIR="/run/nginx"                  # admin socket dir (pid is /run/nginx.pid)
LOG_DIR="/var/log/nginx"
STATE_DIR="/var/lib/nginx"           # deb temp dirs (body/…) + our map.json/region/certs/acme
BUILD_DIR="/usr/local/src/nginx-build"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive

# --- 1. Base nginx from the official nginx.org stable repo, PINNED to an exact
# version. One signed apt transaction installs the binary + OpenSSL and owns the
# stock paths; `nginx -V` is then genuinely an apt nginx's. `stable` (not
# `mainline`) — conservative for a TLS front door. apt-hold freezes it in the
# snapshot; the immutable-snapshot model never `apt upgrade`s in place. The
# toolchain on the second line stays — we still compile the modules + luajit2
# against the installed binary. ---
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://nginx.org/keys/nginx_signing.key \
	| gpg --batch --yes --dearmor -o /usr/share/keyrings/nginx-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] https://nginx.org/packages/ubuntu $(lsb_release -cs) nginx" \
	> /etc/apt/sources.list.d/nginx.list
apt-get update
# Exact pin: "<version>-<release>~<codename>" (e.g. 1.30.3-1~noble). Pinning the
# full version string makes the base unambiguous — Ubuntu's own repo also ships an
# `nginx` at a different version, and a bare `apt install nginx` would just pick
# the highest available. The pin can ONLY resolve to the nginx.org package, and if
# the repo can't serve it the install fails loud (no silent base substitution).
NGINX_PKG_VERSION="${NGINX_VERSION}-${NGINX_PKG_RELEASE}~$(lsb_release -cs)"
apt-get install -y --no-install-recommends "nginx=${NGINX_PKG_VERSION}"
apt-mark hold nginx          # frozen in the snapshot; bump = deliberate rebake

# Belt-and-suspenders: confirm the binary the pin installed is the version we
# compile the modules against. A dynamic module is ABI-bound to the exact nginx
# version it was built against (even with --with-compat), so a mismatch here would
# ship modules that can't load. This catches a repo serving something unexpected
# under the pinned name before we waste a compile.
INSTALLED_VERSION="$("$SBIN_PATH" -v 2>&1 | sed 's#.*nginx/##')"
if [ "$INSTALLED_VERSION" != "$NGINX_VERSION" ]; then
	echo "FATAL: pinned nginx ${NGINX_VERSION} but installed ${INSTALLED_VERSION}" >&2
	exit 1
fi
echo "installed stock nginx ${NGINX_VERSION} (${NGINX_PKG_VERSION}) from nginx.org"

# Compiler toolchain for luajit2 + the dynamic modules. PCRE2/zlib/OpenSSL -dev
# headers must match what the apt nginx was built against (the module .so's are
# compiled against the same nginx source, which #includes these).
apt-get install -y --no-install-recommends \
	build-essential \
	libpcre2-dev zlib1g-dev libssl-dev \
	python3
# python3: the stdlib-only `stream-admin` client (spec/17-tcp-proxy.md) the
# controller runs over SSH to drive the stream{} line-protocol admin socket — the
# L4 analogue of `curl --unix-socket` for the http admin. Stock Ubuntu guests ship
# it; install it explicitly so a from-scratch build container (and the compose
# release gate) has it too. (ca-certificates/curl already came in with the
# nginx.org repo setup above.)

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
# LuaJIT, and it ships in no apt repo. Install to /usr/local; the lua module .so
# links against it via rpath (set in the configure step below). ---
fetch "https://github.com/openresty/luajit2/archive/refs/tags/${LUAJIT2_REF}.tar.gz" "luajit2.tar.gz"
rm -rf "luajit2-src"
mkdir luajit2-src
tar -xzf luajit2.tar.gz -C luajit2-src --strip-components=1
make -C luajit2-src -j"$(nproc)"
make -C luajit2-src install
ldconfig

# --- 3. nginx source MATCHING the installed binary, plus the module sources
# (NDK before lua-nginx-module). We don't install this nginx — we only build its
# modules against it. ---
fetch "https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz" "nginx.tar.gz"
fetch "https://github.com/vision5/ngx_devel_kit/archive/refs/tags/v${NDK_VERSION}.tar.gz" "ndk.tar.gz"
fetch "https://github.com/openresty/lua-nginx-module/archive/refs/tags/v${LUA_NGINX_MODULE_VERSION}.tar.gz" "lua-nginx-module.tar.gz"
fetch "https://github.com/openresty/stream-lua-nginx-module/archive/refs/tags/${STREAM_LUA_MODULE_REF}.tar.gz" "stream-lua-nginx-module.tar.gz"
fetch "https://github.com/openresty/headers-more-nginx-module/archive/refs/tags/v${HEADERS_MORE_VERSION}.tar.gz" "headers-more.tar.gz"

for pair in "nginx.tar.gz:nginx" "ndk.tar.gz:ndk" \
	"lua-nginx-module.tar.gz:lua-nginx-module" \
	"stream-lua-nginx-module.tar.gz:stream-lua-nginx-module" "headers-more.tar.gz:headers-more"; do
	tarball="${pair%%:*}"
	dir="${pair##*:}"
	rm -rf "$dir"
	mkdir "$dir"
	tar -xzf "$tarball" -C "$dir" --strip-components=1
done

# --- 4. Build the modules as DYNAMIC .so's against the apt binary. The pivot
# from the old all-source build: instead of compiling nginx + modules into one
# binary, we `make modules` only. Order still matters: NDK before lua-nginx.
#
# --with-compat is load-bearing: it gives every nginx build the same module-ABI
# signature, so a .so compiled HERE loads into the separately-installed apt
# binary. Without it the module is rejected at load. The rpath wires the lua .so
# to libluajit-5.1.so in /usr/local/lib. We pass the SAME http feature flags the
# stock nginx was built with (`nginx -V` shows v2/ssl/realip) so the module build
# sees the same module set — but emit only the .so's, never `make install`. ---
cd "$BUILD_DIR/nginx"
LUAJIT_LIB=/usr/local/lib LUAJIT_INC=/usr/local/include/luajit-2.1 \
./configure \
	--with-compat \
	--with-http_v2_module \
	--with-http_ssl_module \
	--with-http_realip_module \
	--with-stream \
	--with-stream_ssl_preread_module \
	--with-ld-opt="-Wl,-rpath,/usr/local/lib" \
	--add-dynamic-module="$BUILD_DIR/ndk" \
	--add-dynamic-module="$BUILD_DIR/lua-nginx-module" \
	--add-dynamic-module="$BUILD_DIR/stream-lua-nginx-module" \
	--add-dynamic-module="$BUILD_DIR/headers-more"
make -j"$(nproc)" modules
install -d "$MODULES_DIR"
# NDK builds no runtime .so of its own (it's linked into the lua modules); the
# http-lua, stream-lua, and headers-more .so's land here. The stream-lua module
# is the L4 sibling of http-lua (spec/17-tcp-proxy.md) — same dynamic-module ABI,
# loaded by the apt binary via a load_module line in nginx.conf. Copy whatever
# objs/ produced.
install -m 0644 objs/*.so "$MODULES_DIR/"


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
# headers; installs cjson.so into /usr/local/lib/lua/5.1 (the lua_package_cpath
# in nginx.conf points here). persist.lua and admin.lua require("cjson.safe");
# without this nginx crashes at init_by_lua — the compose gate asserts it. ---
fetch "https://github.com/openresty/lua-cjson/archive/refs/tags/${LUA_CJSON_VERSION}.tar.gz" "lua-cjson.tar.gz"
rm -rf "lua-cjson"
mkdir "lua-cjson"
tar -xzf "lua-cjson.tar.gz" -C "lua-cjson" --strip-components=1
make -C "lua-cjson" LUA_INCLUDE_DIR=/usr/local/include/luajit-2.1
make -C "lua-cjson" install
ldconfig

# --- 6. Install the committed stack: conf, lua, html — at the stock nginx paths
# (/etc/nginx, /usr/share/nginx/html). These are the SAME files the test harness
# exercises, so green compose == the guest's behavior. The nginx.org package
# ships its OWN default /etc/nginx/nginx.conf (with a conf.d/*.conf include and a
# default server we don't want); we OVERWRITE it with our committed single-file
# config, which carries the load_module lines for the dynamic modules above. ---
install -d "$CONF_DIR" "$LUA_DIR" "$HTML_DIR"
install -m 0644 "$SRC_DIR/conf/nginx.conf"  "$CONF_DIR/nginx.conf"
install -m 0644 "$SRC_DIR/conf/mime.types"  "$CONF_DIR/mime.types"
install -m 0644 "$SRC_DIR/lua/router.lua"   "$LUA_DIR/router.lua"
install -m 0644 "$SRC_DIR/lua/admin.lua"    "$LUA_DIR/admin.lua"
install -m 0644 "$SRC_DIR/lua/persist.lua"  "$LUA_DIR/persist.lua"
# The stream{}-side trio (spec/17-tcp-proxy.md): the L4 forwarder's router,
# line-protocol admin, and persist. Separate files because stream{} Lua runs in a
# separate subsystem (own lua_shared_dict address space) from the http{} trio.
install -m 0644 "$SRC_DIR/lua/stream_router.lua"  "$LUA_DIR/stream_router.lua"
install -m 0644 "$SRC_DIR/lua/stream_admin.lua"   "$LUA_DIR/stream_admin.lua"
install -m 0644 "$SRC_DIR/lua/stream_persist.lua" "$LUA_DIR/stream_persist.lua"
install -m 0644 "$SRC_DIR/html/not_found.html" "$HTML_DIR/not_found.html"
# The nginx.org package drops conf.d/default.conf, included by ITS nginx.conf.
# Ours doesn't include conf.d, so this is dead weight — remove it so a curious
# engineer doesn't think it's live.
rm -f "$CONF_DIR/conf.d/default.conf"

# The stream-admin line-protocol client (spec/17-tcp-proxy.md): the controller
# runs `stream-admin GET` / `SYNC` over SSH-to-the-guest to reconcile the TCP port
# map, the L4 analogue of `curl --unix-socket` for the http admin. On PATH so the
# controller invokes it by bare name; the compose gate runs the identical binary.
install -m 0755 "$SRC_DIR/guest/stream-admin" /usr/local/bin/stream-admin

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
# `journalctl -u nginx` work by reflex. We install OUR unit over whatever the apt
# package dropped (the package's unit doesn't run our -t precheck / paths). Enable
# but do not start (this may be a chroot / container build with no live systemd).
# The package's own unit (lib/systemd) is shadowed by our /etc/systemd one. ---
install -m 0644 "$SRC_DIR/guest/nginx.service" /etc/systemd/system/nginx.service
install -d /etc/tmpfiles.d
install -m 0644 "$SRC_DIR/guest/tmpfiles.d/nginx.conf" /etc/tmpfiles.d/nginx.conf
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl enable nginx.service
else
	# No live systemd (Docker build): enable by symlink so a real boot starts it.
	install -d /etc/systemd/system/multi-user.target.wants
	ln -sf /etc/systemd/system/nginx.service \
		/etc/systemd/system/multi-user.target.wants/nginx.service
fi

# --- 9. Validate the config compiles. The smoke test the build itself can do —
# now ALSO proves the three load_module lines resolve the dynamic .so's and that
# require("cjson.safe") + lua-resty-core load at init. ---
"$SBIN_PATH" -t -c "$CONF_DIR/nginx.conf"

echo "nginx proxy stack built: stock nginx ${NGINX_VERSION} (apt) + dynamic lua-nginx-module ${LUA_NGINX_MODULE_VERSION} + stream-lua ${STREAM_LUA_MODULE_REF} + headers-more ${HEADERS_MORE_VERSION} (HTTP + L4 TCP forwarder)."
