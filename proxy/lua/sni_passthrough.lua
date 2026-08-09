-- sni_passthrough.lua — the custom-domain strip-path router (spec/12-proxy.md §
-- The stream front-door; spec/18 Phase 2).
--
-- Runs in preread_by_lua on the loopback strip-path server (`listen 127.0.0.1:8445
-- proxy_protocol; ssl_preread on`). The PUBLIC :443 front-door forwarded this
-- connection here with a PROXY v1 header (carrying the real client IP); the strip
-- server's `listen ... proxy_protocol` RECEIVES and CONSUMES that header, so what
-- this Lua and the subsequent proxy_pass see is the RAW TLS stream that followed it.
-- We re-read the SNI (the ClientHello survived the strip intact) and look up the
-- backend VM's :443 in the `domains` dict, then proxy_pass the raw bytes there with
-- NO PROXY header (proxy_protocol off on this server), so the VM terminates a clean
-- handshake under its own cert.
--
-- This server is reached ONLY from the front-door (loopback bind), and only for
-- SNIs the front-door already confirmed are in `domains`, so a miss here is a
-- race (the row was deregistered between the two preread reads) — drop cleanly.

local domains = ngx.shared.domains

local sni = ngx.var.ssl_preread_server_name or ""
sni = sni:lower():gsub(":%d+$", "")

if sni == "" then
	return ngx.exit(ngx.ERROR)
end

-- The ready-to-dial "[<v6>]:443" literal the controller's reconcile wrote.
local backend = domains:get(sni)
if not backend then
	-- Deregistered between the front-door's lookup and ours: drop.
	return ngx.exit(ngx.ERROR)
end

ngx.var.passthrough_upstream = backend
