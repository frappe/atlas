
local ROOT = "/usr/share/nginx/html/"

local FALLBACK = {
	not_found = "Site not found.\n",
	domain_unconfigured = "Domain not configured.\n",
}

local cache = {}

local pages = {}
-- Cache each page after its first read so misses do not hit disk repeatedly.

function pages.serve(name, status)
	local body = cache[name]
	if not body then
		local f = io.open(ROOT .. name .. ".html", "r")
		if f then
			body = f:read("*a")
			f:close()
		else
			body = FALLBACK[name]
		end
		cache[name] = body
	end
	ngx.status = status
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.print(body)
	return ngx.exit(status)
end

return pages
