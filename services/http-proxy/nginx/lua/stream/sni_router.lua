local domains = ngx.shared.domains

local WILDCARD_TERMINATOR = "127.0.0.1:8443"
local CUSTOM_STRIP_PATH = "127.0.0.1:8445"
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

if domains:get(sni) then
	ngx.var.sni_upstream = CUSTOM_STRIP_PATH
	return
end

ngx.var.sni_upstream = UNCONFIGURED_TERMINATOR
