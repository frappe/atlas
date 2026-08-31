
local pages = require("pages")

local sites = ngx.shared.sites
local domains_http = ngx.shared.domains_http

local host = ngx.var.host or ""
host = host:lower():gsub(":%d+$", "")

local subdomain
if atlas_root_domain and atlas_root_domain ~= "" then
	local suffix = "." .. atlas_root_domain
	if host:sub(-#suffix) == suffix then
		subdomain = host:sub(1, #host - #suffix)
	end
else
	subdomain = host:match("^([^.]+)%.")
end

if subdomain and subdomain ~= "" then
	local address = sites:get(subdomain)
	if not address then
		return pages.serve("not_found", ngx.HTTP_NOT_FOUND)
	end
	if address == "-" then
		return pages.serve("not_found", ngx.HTTP_SERVICE_UNAVAILABLE)
	end
	ngx.var.vm_upstream = "http://[" .. address .. "]:80"
	return
end

local backend = domains_http:get(host)
if not backend then
	return pages.serve("domain_unconfigured", ngx.HTTP_NOT_FOUND)
end
if backend:sub(1, 1) == "[" then
	backend = backend:match("^(%b[])%:%d+$") or backend
else
	backend = "[" .. backend .. "]"
end
ngx.var.vm_upstream = "http://" .. backend .. ":80"
