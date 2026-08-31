local domains = ngx.shared.domains
local domain_lookup = require("domain_lookup")

local WILDCARD_TERMINATOR = "127.0.0.1:8443"
local UNCONFIGURED_TERMINATOR = "127.0.0.1:8446"

local sni = ngx.var.ssl_preread_server_name or ""
sni = sni:lower():gsub(":%d+$", "")

-- No SNI means there is no safe destination. Drop the connection at layer 4.
if sni == "" then
	return ngx.exit(ngx.ERROR)
end

if atlas_root_domain and atlas_root_domain ~= "" then
	local suffix = "." .. atlas_root_domain
	if sni:sub(-#suffix) == suffix then
		ngx.var.sni_upstream = WILDCARD_TERMINATOR
		return
	end
end

-- Custom-domain TLS uses PROXY v2 so the VM gets the client address.
local backend = domain_lookup.get(domains, sni)
if backend then
	ngx.var.sni_upstream = backend
	return
end

ngx.var.sni_upstream = UNCONFIGURED_TERMINATOR
