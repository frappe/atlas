-- sni_router.lua — the :443 SNI front-door fork (spec/12-proxy.md § The stream
-- front-door; spec/13-tls.md § Custom domains; spec/18 Phase 2).
--
-- Runs in preread_by_lua on the PUBLIC :443 stream server (`ssl_preread on`,
-- `proxy_protocol on`). That server accepts the connection straight from the
-- internet, so $remote_addr is the genuine client; with `proxy_protocol on` it
-- prepends a PROXY v1 header carrying that real client IP to whichever loopback
-- target we pick — so the true client survives the loopback hop (no L4 IP-loss
-- regression). The only routing key a raw TLS connection gives us is the SNI in the
-- ClientHello (ngx.var.ssl_preread_server_name) — no decrypt. We fork on it:
--
--   SNI under the regional wildcard (*.<region>.frappe.dev)
--       -> 127.0.0.1:8443  (the http{} terminator, `listen ... ssl proxy_protocol`,
--                           which RECEIVES the PROXY header and recovers the client
--                           IP, then terminates TLS under the wildcard cert — the
--                           existing L7 path, unchanged).
--   SNI is a known custom domain (full host in the `domains` dict)
--       -> 127.0.0.1:8445  (the strip-path: it RECEIVES + consumes the PROXY header,
--                           then forwards the RAW TLS stream to the backend VM's :443
--                           with NO PROXY header, so the VM terminates a clean
--                           handshake with its OWN cert). sni_passthrough.lua there
--                           re-reads the SNI to pick the VM.
--   miss (no SNI, or an unknown name)
--       -> drop the connection (ngx.exit(ngx.ERROR)). An unregistered or deregistered
--          custom name is simply absent from `domains`, so its handshake is dropped
--          rather than forwarded to no backend (spec/13 § Custom domains). A REGISTERED
--          domain is in `domains` the moment it is created — there is no readiness gate.
--
-- Both loopback targets EXPECT the PROXY header the edge emits (8443 receives it,
-- 8445 consumes+strips it), so `proxy_protocol on` on this edge is correct for ALL
-- forks. The wildcard-vs-custom test is the SAME host-suffix predicate router.lua
-- uses, reused here at L4.
--
-- Phase-ordering note (stream-lua #153): both `ssl_preread` and `preread_by_lua`
-- run in the preread phase, and Lua can otherwise run BEFORE ssl_preread has parsed
-- the ClientHello (empty SNI). nginx.conf keeps `preread_by_lua_no_postpone` at its
-- default (off) so this file runs at the END of the preread phase, after ssl_preread
-- has populated ssl_preread_server_name. Do not set it on.

local domains = ngx.shared.domains

-- The http{} wildcard terminator (loopback, receives PROXY) and the custom strip-path
-- (loopback, consumes PROXY then forwards raw). Both are nginx.conf servers below.
local WILDCARD_TERMINATOR = "127.0.0.1:8443"
local CUSTOM_STRIP_PATH = "127.0.0.1:8445"

-- The SNI, lowercased and port-stripped to a bare host. ssl_preread gives the raw
-- SNI; normalize it the same way router.lua normalizes Host so the suffix test and
-- the dict lookup match what the controller stored.
local sni = ngx.var.ssl_preread_server_name or ""
sni = sni:lower():gsub(":%d+$", "")

if sni == "" then
	-- No SNI (a bare-IP TLS client, or a probe): nothing to route at L4. Drop.
	return ngx.exit(ngx.ERROR)
end

-- A name under the regional wildcard terminates AT the proxy (the L7 path). The
-- suffix predicate is the one router.lua uses; atlas_region is loaded once at init
-- from /var/lib/nginx/region (nginx.conf stream init_by_lua), the same file the
-- http subsystem reads.
if atlas_region and atlas_region ~= "" then
	local suffix = "." .. atlas_region .. ".frappe.dev"
	if sni:sub(-#suffix) == suffix then
		ngx.var.sni_upstream = WILDCARD_TERMINATOR
		return
	end
end

-- A known custom domain: hand to the strip-path, which re-reads the SNI and dials
-- the VM raw. Every registered (active) custom domain is present here — no readiness
-- gate — so an unknown/deregistered name misses and is dropped below.
if domains:get(sni) then
	ngx.var.sni_upstream = CUSTOM_STRIP_PATH
	return
end

-- Unknown name (unregistered / deregistered): drop. No branded page — this is L4,
-- the client just sees a closed socket.
return ngx.exit(ngx.ERROR)
