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
# Idempotent (spec taste #14: retry = re-run). Re-running force-reinstalls the held
# apt nginx (unhold + --reinstall, §1) AND rebuilds the modules from the pinned
# sources in the same run, so the binary and the modules are always one coordinated
# build — never a stale binary under freshly-compiled modules. Already-present source
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
STATE_DIR="/var/lib/nginx"           # 100% Atlas state (map.json/region/certs/acme); the
                                     # nginx.org pkg uses /var/cache/nginx for its temp dirs
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
# --reinstall (after clearing any prior hold) is LOAD-BEARING on a re-bake, not
# cosmetic: the dynamic modules below are ABI-bound to the EXACT nginx binary they
# were compiled against, and on a re-run the previous bake has already installed +
# held this same version — a plain `apt install nginx=<held version>` is then a
# no-op that leaves the OLD binary in place while we recompile the modules against a
# freshly-fetched nginx source. That ships a binary and a module set from two
# different build moments — the silent base/module mismatch this whole stack pins to
# avoid (a mismatched dynamic module can still load + pass `nginx -t` yet misbehave
# at runtime). unhold→--reinstall forces the binary to be laid down fresh every run,
# so the binary and the modules are always one coordinated set.
apt-mark unhold nginx 2>/dev/null || true
apt-get install -y --reinstall --no-install-recommends "nginx=${NGINX_PKG_VERSION}"
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

# fetch <url> <output> — download once, reuse on re-run. Retries a few times so a
# transient GitHub/codeload blip on a fresh droplet (a single 400/redirect hiccup,
# the same flaky egress that makes apt time out on first boot) doesn't abort the
# whole multi-minute bake under `set -e`. Still fails loud once the retries run out.
fetch() {
	local url="$1" out="$2" attempt
	if [ -f "$out" ]; then
		echo "  reuse $out"
		return
	fi
	echo "  fetch $url"
	for attempt in 1 2 3 4 5; do
		if curl -fsSL --retry 3 --retry-all-errors --output "$out.part" "$url"; then
			mv "$out.part" "$out"
			return
		fi
		echo "  fetch attempt $attempt failed, retrying in $((attempt * 3))s ..." >&2
		rm -f "$out.part"
		sleep "$((attempt * 3))"
	done
	echo "FATAL: could not fetch $url after 5 attempts" >&2
	exit 1
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

# --- 3b. Patch nginx's stream ssl_preread so preread_by_lua can read the SNI.
# nginx 1.25.5 (Apr 2024) reworked the stream phase engine: when `ssl_preread on`
# successfully parses the ClientHello its handler returns NGX_OK, which now ENDS the
# preread phase — so `preread_by_lua` (also a preread-phase handler) never runs, and
# `proxy_pass $sni_upstream` fires with the variable still unset ("no host in
# upstream """ → connection reset). That kills the :443 SNI front-door
# (sni_router.lua / sni_passthrough.lua), which routes raw TLS by reading
# $ssl_preread_server_name inside preread_by_lua (spec/12 § The stream front-door).
#
# OpenResty ships the fix as a one-line nginx patch (their bundle's
# nginx-<ver>-stream_ssl_preread_no_skip.patch): on a successful SNI parse,
# ssl_preread returns NGX_DECLINED instead of NGX_OK, so the preread phase KEEPS
# running and the lua handler gets its turn with the SNI already populated. The
# committed copy (proxy/patches/) is OpenResty's exact hunk; the changed lines are
# byte-identical across nginx stable, so it applies clean on the pinned base — and
# build.sh fails loud (`patch` under set -e) if a base bump ever moves them. The
# compose gate (test_custom_domain_proxy.py) asserts the :443 fork end-to-end.
patch -p1 -d nginx < "$SRC_DIR/patches/nginx-stream_ssl_preread_no_skip.patch"

# --- 4. Build nginx (patched binary) + the DYNAMIC module .so's, one configure.
# We compile our OWN nginx binary here — NOT just `make modules` — because the §3b
# ssl_preread fix lives in nginx CORE (ssl_preread is --with-stream_ssl_preread_module,
# static, compiled INTO the binary), so the apt binary can't carry it. The apt nginx
# is still installed in §1 (it owns OpenSSL, the repo, the deps, the held package
# metadata and the stock dir layout); §4b overwrites only its binary FILE with this
# same-version patched recompile. The configure mirrors the apt build's flags + paths
# so the result is behavior-identical apart from the one-line ssl_preread fix. Order
# still matters: NDK before lua-nginx.
#
# --with-compat is load-bearing: it gives every nginx build the same module-ABI
# signature, so the .so's compiled HERE load into this binary. The rpath wires the lua
# .so to libluajit-5.1.so in /usr/local/lib. `make` (not `make modules`) emits BOTH
# objs/nginx and objs/*.so from one configure, so binary + modules are one coordinated
# ABI set. ---
cd "$BUILD_DIR/nginx"
# The --prefix and --*-path flags MUST mirror the apt nginx.org package's own build
# (its `nginx -V`: --prefix=/etc/nginx, --sbin-path=/usr/sbin/nginx,
# --modules-path=/usr/lib/nginx/modules, …) so our recompiled binary (§4b) resolves
# the SAME stock paths the apt binary did — load_module's relative `modules/…` is
# relative to --prefix, the conf/log/pid/temp paths are baked in, and the snapshot
# layout is unchanged. Without these the recompile would default to --prefix=
# /usr/local/nginx and `nginx -t` would look for modules + logs in the wrong place.
LUAJIT_LIB=/usr/local/lib LUAJIT_INC=/usr/local/include/luajit-2.1 \
./configure \
	--prefix=/etc/nginx \
	--sbin-path=/usr/sbin/nginx \
	--modules-path=/usr/lib/nginx/modules \
	--conf-path=/etc/nginx/nginx.conf \
	--error-log-path=/var/log/nginx/error.log \
	--http-log-path=/var/log/nginx/access.log \
	--pid-path=/run/nginx.pid \
	--lock-path=/run/nginx.lock \
	--http-client-body-temp-path=/var/cache/nginx/client_temp \
	--http-proxy-temp-path=/var/cache/nginx/proxy_temp \
	--http-fastcgi-temp-path=/var/cache/nginx/fastcgi_temp \
	--http-uwsgi-temp-path=/var/cache/nginx/uwsgi_temp \
	--http-scgi-temp-path=/var/cache/nginx/scgi_temp \
	--user=nginx \
	--group=nginx \
	--with-compat \
	--with-file-aio \
	--with-threads \
	--with-http_addition_module \
	--with-http_auth_request_module \
	--with-http_dav_module \
	--with-http_flv_module \
	--with-http_gunzip_module \
	--with-http_gzip_static_module \
	--with-http_mp4_module \
	--with-http_random_index_module \
	--with-http_realip_module \
	--with-http_secure_link_module \
	--with-http_slice_module \
	--with-http_ssl_module \
	--with-http_stub_status_module \
	--with-http_sub_module \
	--with-http_v2_module \
	--with-stream \
	--with-stream_realip_module \
	--with-stream_ssl_module \
	--with-stream_ssl_preread_module \
	--with-ld-opt="-Wl,-rpath,/usr/local/lib" \
	--add-dynamic-module="$BUILD_DIR/ndk" \
	--add-dynamic-module="$BUILD_DIR/lua-nginx-module" \
	--add-dynamic-module="$BUILD_DIR/stream-lua-nginx-module" \
	--add-dynamic-module="$BUILD_DIR/headers-more"
# Build BOTH the nginx binary and the dynamic .so's (not `make modules` alone). We
# now ship our OWN nginx binary because the ssl_preread patch (§3b) lives in nginx
# CORE, not in a module — ssl_preread is `--with-stream_ssl_preread_module` (static,
# no `=dynamic`), so it is compiled INTO the binary. The apt nginx.org binary cannot
# carry the patch; only a binary we compile from the patched source can. The full
# `make` produces objs/nginx (patched) alongside objs/*.so. apt still owns the deps,
# OpenSSL, repo and stock paths; we replace only the binary FILE with a same-version
# patched recompile (§4b).
make -j"$(nproc)"
install -d "$MODULES_DIR"
# NDK builds no runtime .so of its own (it's linked into the lua modules); the
# http-lua, stream-lua, and headers-more .so's land here. The stream-lua module
# is the L4 sibling of http-lua (spec/17-tcp-proxy.md) — same dynamic-module ABI,
# loaded by the apt binary via a load_module line in nginx.conf. Copy whatever
# objs/ produced.
install -m 0644 objs/*.so "$MODULES_DIR/"

# --- 4b. Replace the apt nginx BINARY with our patched recompile of the SAME
# version. §3b's ssl_preread fix is in nginx core, which the apt binary can't carry,
# so the patched objs/nginx must be the binary that runs. We overwrite only the
# binary file at the apt path — apt still owns OpenSSL, the repo, the deps, the
# stock dir layout and the (held) package metadata; `nginx -V` still reads 1.30.3.
# The recompile uses the same flags `nginx -V` reported, plus --with-compat, so the
# dynamic .so's above load into it unchanged. Replace-by-rename (mv old aside, then
# install) so it works even if the path is busy on a re-bake. ABI-bound: binary and
# .so's are now one coordinated build from one configure, same as before.
install -m 0755 objs/nginx "$SBIN_PATH.atlas-patched"
mv -f "$SBIN_PATH.atlas-patched" "$SBIN_PATH"
# Confirm the running binary is genuinely our patched recompile (its `nginx -V`
# configure line carries the --add-dynamic-module args the apt binary never had) and
# still reports the pinned version.
"$SBIN_PATH" -V 2>&1 | grep -q -- "--add-dynamic-module=.*stream-lua" \
	|| { echo "FATAL: patched nginx not the recompile — wrong binary installed" >&2; exit 1; }


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
# No custom mime.types: nginx.conf `include /etc/nginx/mime.types` reads the
# nginx.org package's own ~90-entry conffile (installed in §1). The proxy serves
# every location via proxy_pass (upstreams set Content-Type) and the one local
# file not_found.html sets its Content-Type from Lua, so the map governs nothing
# we emit — the stock file is strictly a superset of what a custom one would.
install -m 0644 "$SRC_DIR/lua/router.lua"   "$LUA_DIR/router.lua"
install -m 0644 "$SRC_DIR/lua/admin.lua"    "$LUA_DIR/admin.lua"
install -m 0644 "$SRC_DIR/lua/persist.lua"  "$LUA_DIR/persist.lua"
# The custom-domain :80 ACME map (spec/12 § The stream front-door, spec/18 Phase 2):
# the http{}-side Host fork for a custom domain's HTTP-01 challenge + its persist.
install -m 0644 "$SRC_DIR/lua/acme_router.lua"  "$LUA_DIR/acme_router.lua"
install -m 0644 "$SRC_DIR/lua/acme_persist.lua" "$LUA_DIR/acme_persist.lua"
# The stream{}-side trio (spec/17-tcp-proxy.md): the L4 forwarder's router,
# line-protocol admin, and persist. Separate files because stream{} Lua runs in a
# separate subsystem (own lua_shared_dict address space) from the http{} trio.
install -m 0644 "$SRC_DIR/lua/stream_router.lua"  "$LUA_DIR/stream_router.lua"
install -m 0644 "$SRC_DIR/lua/stream_admin.lua"   "$LUA_DIR/stream_admin.lua"
install -m 0644 "$SRC_DIR/lua/stream_persist.lua" "$LUA_DIR/stream_persist.lua"
# The custom-domain :443 SNI front-door (spec/12 § The stream front-door, spec/18
# Phase 2): the SNI fork router, the strip-path passthrough router, and the `domains`
# map persist. Stream-side because ssl_preread is a stream module.
install -m 0644 "$SRC_DIR/lua/sni_router.lua"      "$LUA_DIR/sni_router.lua"
install -m 0644 "$SRC_DIR/lua/sni_passthrough.lua" "$LUA_DIR/sni_passthrough.lua"
install -m 0644 "$SRC_DIR/lua/sni_persist.lua"     "$LUA_DIR/sni_persist.lua"
# unconfigured.lua: the dummy-cert terminator that serves the branded "Domain not
# configured" page for a custom domain pointed here but not yet routed (the :8446
# fork sni_router.lua takes on a named miss instead of dropping at L4).
install -m 0644 "$SRC_DIR/lua/unconfigured.lua"    "$LUA_DIR/unconfigured.lua"
install -m 0644 "$SRC_DIR/html/not_found.html" "$HTML_DIR/not_found.html"
install -m 0644 "$SRC_DIR/html/domain_unconfigured.html" "$HTML_DIR/domain_unconfigured.html"
# The nginx.org package drops conf.d/default.conf, included by ITS nginx.conf. Our
# nginx.conf does NOT include conf.d (see the note there), so it never loads — we
# leave the dpkg-owned conffile in place rather than hand-deleting it and desyncing
# dpkg's bookkeeping. test_build.py asserts conf.d stays unincluded so a future
# re-include is caught.

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
# $LOG_DIR (/var/log/nginx) is created+owned by the nginx.org .deb at mode 0755 in
# §1 (with logrotate), so we don't re-create it. $STATE_DIR (/var/lib/nginx) is
# all-Atlas state the package never makes.
#
# Workers run as the `nginx` user (nginx.conf `user nginx;`) and WRITE the live
# snapshots map.json / stream-map.json here from a worker timer (persist.dump /
# stream_persist.dump: write .tmp then rename). A rename needs write+exec on the
# PARENT dir, so $STATE_DIR itself is group-writable by nginx — but root stays the
# OWNER (root:nginx, 0770) so only the controller-as-root rewrites the tree
# wholesale. The snapshot files persist.dump creates inherit the worker's nginx
# ownership; the dir's group-write is what lets the rename land. certs/ is tighter
# (root:root 0750, set below) — the privkey is read by the MASTER (root) at config
# parse, never by a worker, so it never needs group-read (CIS 4.1.3). acme/ is a
# worker READ (the .well-known root), so root:nginx 0750 is enough. region is a
# MASTER-only read (init_by_lua) → leave it root:root, no chown.
# Create certs first (default root:root), then re-create the parent with the group
# bits so $STATE_DIR's 0770 root:nginx sticks while certs keeps 0750 root:root.
install -d -m 0750 "$STATE_DIR/certs"
install -d -o root -g nginx -m 0770 "$STATE_DIR"
install -d -o root -g nginx -m 0750 "$STATE_DIR/acme"
: > "$STATE_DIR/region"
# certs/ and the privkey stay root-only (0750 dir / 0640 placeholder key; the real
# key push_cert writes is 0600). The SSL core reads them at CONFIG PARSE, which runs
# in the MASTER (root) — a worker never opens the key — so dropping workers to
# `nginx` does NOT require the key to be group-readable. Leaving it root-only keeps
# the wildcard private key off every lower-priv principal on the box (CIS 4.1.3).
install -d -m 0750 "$STATE_DIR/certs/_placeholder"
# Regenerate every bake (no `[ ! -f ]` guard): the cert is a self-signed throwaway
# the :8446 unconfigured-domain terminator presents, so a fresh one each re-bake
# costs nothing and — unlike a guard — lets the Subject copy below actually take
# effect instead of being pinned to whatever the first bake happened to write. NOT
# the wildcard key (that's push_cert's, untouched).
#
# The Subject is the message. A browser never trusts this cert, so the visitor lands
# on the "your connection is not private" interstitial and can open "view certificate"
# — the ONLY channel a self-signed cert has to a human, since the interstitial itself
# renders the typed hostname, not our fields. So every readable DN field is a line of
# copy the details pane shows verbatim (and, self-signed ⇒ issuer==subject, "Issued By"
# mirrors it): CN is the headline, O says who we are, OU is the next step + URL. Keep
# it plain ASCII ≤64 chars/field (RFC 5280 DN bound; PrintableString/UTF8String — no
# emoji, some cert UIs mangle them). The old CN was "atlas-unconfigured", which read
# like a bug; this reads like an answer. KEEP THIS -subj byte-identical to
# atlas.proxy.PLACEHOLDER_CERT_SUBJECT, which regenerate_placeholder_cert runs to push a
# copy change to a live proxy without a full re-bake — the two must write the same cert.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
	-keyout "$STATE_DIR/certs/_placeholder/privkey.pem" \
	-out "$STATE_DIR/certs/_placeholder/fullchain.pem" \
	-subj "/CN=This domain is not connected to a site yet/O=Frappe Cloud/OU=Connect it in your dashboard: frappe.dev\/domains"
chmod 0640 "$STATE_DIR/certs/_placeholder/privkey.pem"
# Point the flat path nginx reads at the placeholder region (repointed by
# build_proxy once the real region is known). -n so we replace the symlink
# itself, not follow it into the target dir on a re-run.
ln -sfn _placeholder/fullchain.pem "$STATE_DIR/certs/fullchain.pem"
ln -sfn _placeholder/privkey.pem   "$STATE_DIR/certs/privkey.pem"

# --- 8. systemd: a thin drop-in over the package's OWN nginx.service, NOT a full
# shadow. The nginx.org unit ships Type=forking, PIDFile, ExecStart=-c ${CONFFILE}
# (CONFFILE defaults to our /etc/nginx/nginx.conf), ExecReload (kill -HUP), ExecStop
# and WantedBy=multi-user.target, so leaving it authoritative lets `apt upgrade
# nginx` keep shipping base-unit fixes. The drop-in carries ONLY the deltas with no
# stock equivalent: After=atlas-network.service (order after the guest's static /128
# v6 is up), ExecStartPre=nginx -t (the package unit ships NO precheck — needed so a
# bad config refuses to start instead of restart-looping under Restart=on-failure),
# Restart=on-failure, LimitNOFILE (the ~20000-listener pool — the conf sets no
# worker_rlimit_nofile), and RuntimeDirectory=nginx/RuntimeDirectoryMode=0750
# (creates the 0750-root /run/nginx admin-socket dir, subsuming the old tmpfiles.d
# file). `systemctl status nginx` / `journalctl -u nginx` keep working by reflex.
# Enable but do not start (this may be a chroot / container build with no live
# systemd). ---
install -d /etc/systemd/system/nginx.service.d
install -m 0644 "$SRC_DIR/guest/nginx.service.d/atlas.conf" \
	/etc/systemd/system/nginx.service.d/atlas.conf
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl enable nginx.service
else
	# No live systemd (Docker build): enable the PACKAGE unit by symlink so a real
	# boot starts it (the drop-in is read automatically alongside it). The package
	# unit lives at /lib/systemd/system/nginx.service.
	install -d /etc/systemd/system/multi-user.target.wants
	ln -sf /lib/systemd/system/nginx.service \
		/etc/systemd/system/multi-user.target.wants/nginx.service
fi

# --- 9. Validate the config compiles. The smoke test the build itself can do —
# now ALSO proves the three load_module lines resolve the dynamic .so's and that
# require("cjson.safe") + lua-resty-core load at init. ---
# `nginx -t` actually opens every listen socket, so the stream{} block's
# 10000-19999 pool (×2 for v4+v6, ~20000 listeners) needs ~20000 fds. At runtime
# the nginx.service.d drop-in's LimitNOFILE=1048576 covers this, but this build-time
# test runs as a bare process under the build shell's default RLIMIT_NOFILE (1024),
# where socket() fails "(24: Too many open files)". Raise the soft limit to the hard
# limit for the test alone (matches the unit's LimitNOFILE intent).
ulimit -n "$(ulimit -Hn)"
"$SBIN_PATH" -t -c "$CONF_DIR/nginx.conf"

echo "nginx proxy stack built: stock nginx ${NGINX_VERSION} (apt) + dynamic lua-nginx-module ${LUA_NGINX_MODULE_VERSION} + stream-lua ${STREAM_LUA_MODULE_REF} + headers-more ${HEADERS_MORE_VERSION} (HTTP + L4 TCP forwarder)."
