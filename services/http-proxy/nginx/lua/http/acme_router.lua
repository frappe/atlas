local domains_http = ngx.shared.domains_http
local domain_lookup = require("domain_lookup")

local host = ngx.var.host or ""
host = host:lower():gsub(":%d+$", "")

if atlas_root_domain and atlas_root_domain ~= "" then
	local suffix = "." .. atlas_root_domain
	if host:sub(-#suffix) == suffix then
		return ngx.exec("@acme_local")
	end
end

local backend = domain_lookup.get(domains_http, host)
if backend then
	if backend:sub(1, 1) == "[" then
		backend = backend:match("^(%b[])%:%d+$") or backend
	else
		backend = "[" .. backend .. "]"
	end
	ngx.var.acme_upstream = "http://" .. backend .. ":80"
	return
end

return ngx.exec("@acme_local")
