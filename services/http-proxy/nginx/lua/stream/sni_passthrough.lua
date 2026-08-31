local domains = ngx.shared.domains
local domain_lookup = require("domain_lookup")

local sni = ngx.var.ssl_preread_server_name or ""
sni = sni:lower():gsub(":%d+$", "")

if sni == "" then
	return ngx.exit(ngx.ERROR)
end

local backend = domain_lookup.get(domains, sni)
if not backend then
	return ngx.exit(ngx.ERROR)
end

ngx.var.passthrough_upstream = backend
