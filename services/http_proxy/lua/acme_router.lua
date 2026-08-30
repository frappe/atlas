-- acme_router.lua — the :80 ACME-challenge Host fork (spec/12-proxy.md § The
-- stream front-door; spec/13-tls.md § Custom domains).
--
-- Runs in access_by_lua on the :80 server's /.well-known/acme-challenge/ location
-- ONLY. A custom domain's backend VM completes its own HTTP-01 challenge, so the
-- challenge GET (which arrives in cleartext on :80, Host = the custom domain) must
-- reach the VM. But a *.<region>.frappe.dev challenge must NEVER reach a VM — the
-- proxy owns the regional wildcard cert and answers those itself, so no tenant VM
-- can satisfy a challenge for the regional wildcard and have a CA issue it a
-- *.<region> cert (spec/13, the wildcard guard).
--
--   Host under *.<region>.frappe.dev  -> ngx.exec("@acme_local") (the webroot
--                                        serves it; NEVER reaches a VM)
--   Host is a known custom domain      -> set acme_upstream = http://[vm_v6]:80 and
--                                        fall through to the block's proxy_pass
--   Host is anything else              -> ngx.exec("@acme_local") (webroot 404;
--                                        we never proxy an unknown name)
--
-- The wildcard-vs-custom test is the SAME host-suffix predicate router.lua and
-- sni_router.lua use. acme_domains holds the bare bracketed v6 ("[<v6>]"); we build
-- the http URL here. ngx.exec internally redirects to the @acme_local named location
-- (root webroot), so a request never has BOTH a proxy_pass and a root active — which
-- sidesteps the "proxy_pass inside if" footgun.

local acme_domains = ngx.shared.acme_domains

local host = ngx.var.host or ""
host = host:lower():gsub(":%d+$", "")

-- A name under the regional wildcard: the proxy answers the challenge itself from
-- its own webroot. NEVER proxy these to a VM (the wildcard guard). atlas_root_domain
-- is the FULL regional wildcard zone (e.g. "blr1.frappe.dev" or
-- "aditya-blr3.x.frappe.dev"); strip that exact zone, not region .. ".frappe.dev".
if atlas_root_domain and atlas_root_domain ~= "" then
	local suffix = "." .. atlas_root_domain
	if host:sub(-#suffix) == suffix then
		return ngx.exec("@acme_local")
	end
end

-- A known custom domain: forward the challenge to the VM, which serves it from its
-- own in-guest webroot (pilot's setup-letsencrypt). The dict value is the bare
-- bracketed v6 "[<v6>]"; build the cleartext :80 URL the block's proxy_pass dials.
local backend = acme_domains:get(host)
if backend then
	ngx.var.acme_upstream = "http://" .. backend .. ":80"
	return
end

-- Unknown / non-custom name: serve locally (the webroot 404s). We do not proxy a
-- name we don't recognise.
return ngx.exec("@acme_local")
