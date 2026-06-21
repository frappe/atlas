-- router.lua — the request path (proxy-design.md §6.1).
--
-- One dict read per request, no allocation beyond the host parse. Runs in
-- access_by_lua (the two proxy locations only, never the not_found location, so
-- there is no redirect cycle): on a hit it sets ngx.var.vm_upstream and returns
-- (the location's proxy_pass $vm_upstream takes over); on a miss it serves the
-- branded page with status 404 (unknown subdomain) or 503 (tombstoned). The
-- region (the ".<region>.frappe.dev" suffix) is loaded once at init into the
-- global atlas_region (see nginx.conf).
--
-- We render the branded page FROM LUA rather than via error_page. (error_page
-- CAN intercept a Lua-phase ngx.exit(<4xx/5xx>) issued before output — a stock
-- error_page 404/503 = /not_found.html wiring would also work — but serving it
-- inline keeps the body cached, shares the ngx.exit idiom with admin.lua, and
-- sidesteps error_page's status-preservation footgun.) Read once and cached.

local sites = ngx.shared.sites

local NOT_FOUND_PAGE = "/usr/share/nginx/html/not_found.html"
local not_found_body  -- cached after first read

local function serve_not_found(status)
    if not not_found_body then
        local f = io.open(NOT_FOUND_PAGE, "r")
        if f then
            not_found_body = f:read("*a")
            f:close()
        else
            not_found_body = "Site not found.\n"
        end
    end
    ngx.status = status
    ngx.header["Content-Type"] = "text/html; charset=utf-8"
    ngx.print(not_found_body)
    return ngx.exit(status)
end

-- Host without port, lowercased: "Acme.Blr1.Frappe.Dev:443" -> "acme.blr1.frappe.dev".
local host = ngx.var.host or ""
host = host:lower():gsub(":%d+$", "")

-- Strip the ".<region>.frappe.dev" suffix to get the bare subdomain. If the
-- region is configured we match it exactly; otherwise fall back to the first
-- label (everything before the first dot) so a misconfigured region still
-- routes by the leftmost label rather than 500ing.
local subdomain
if atlas_region and atlas_region ~= "" then
    local suffix = "." .. atlas_region .. ".frappe.dev"
    if host:sub(-#suffix) == suffix then
        subdomain = host:sub(1, #host - #suffix)
    end
else
    subdomain = host:match("^([^.]+)%.")
end

if not subdomain or subdomain == "" then
    return serve_not_found(ngx.HTTP_NOT_FOUND)
end

local addr = sites:get(subdomain)
if not addr then
    return serve_not_found(ngx.HTTP_NOT_FOUND)
end

-- Tombstone: a known-but-suspended subdomain (§6.5) stores "-" so we can serve
-- 503 "preparing" rather than 404 "no such site". 404-only is also valid; the
-- branded page renders both.
if addr == "-" then
    return serve_not_found(ngx.HTTP_SERVICE_UNAVAILABLE)
end

-- Site is reached over public IPv6, port 80, plaintext. The address is a bare
-- v6 literal; bracket it for the URL.
ngx.var.vm_upstream = "http://[" .. addr .. "]:80"
