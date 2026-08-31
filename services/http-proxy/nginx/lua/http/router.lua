local pages = require("pages")

local sites = ngx.shared.sites

local host = ngx.var.host or ""
-- The region file contains the complete zone, not only the region label.
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

if not subdomain or subdomain == "" then
	return pages.serve("not_found", ngx.HTTP_NOT_FOUND)
end

local address = sites:get(subdomain)
if not address then
	return pages.serve("not_found", ngx.HTTP_NOT_FOUND)
end

if address == "-" then
	return pages.serve("not_found", ngx.HTTP_SERVICE_UNAVAILABLE)
end

ngx.var.vm_upstream = "http://[" .. address .. "]:80"
