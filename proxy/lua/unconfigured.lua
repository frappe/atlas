-- unconfigured.lua — the custom-domain fallback page (spec/12-proxy.md § The
-- stream front-door; spec/13-tls.md § Custom domains).
--
-- Runs in content_by_lua on the loopback dummy-cert terminator (`listen
-- 127.0.0.1:8446 ssl proxy_protocol`). The :443 front-door (sni_router.lua) sends
-- a connection here when its SNI is a real hostname that is NEITHER a live wildcard
-- subdomain NOR a registered custom domain — i.e. a custom domain whose DNS points
-- at us but which isn't connected to a site (typo, stale DNS after deregister, or a
-- registration that hasn't happened). We hold NO trusted cert for that name (the
-- spec/13 trust boundary keeps the custom-domain cert on the VM), so terminating here
-- means presenting the self-signed _placeholder cert (its Subject DN carries the
-- "connect this domain" copy so the browser's cert-details pane shows it — see
-- build.sh — the
-- :8446 block pins that fixed path directly, NOT the flat certs/ symlink, which
-- push_cert repoints to the real region wildcard): the client sees a browser cert
-- warning, and AFTER clicking through gets this branded page instead of a dropped
-- socket. Empty/bare-IP/junk SNI is still dropped at L4 by sni_router — only a
-- NAMED miss reaches here, so this page is never the response to a scanner.
--
-- The wildcard-subdomain miss (a non-existent *.<region> sub) keeps its own page
-- (router.lua's not_found.html) served under the VALID wildcard cert — that path is
-- unchanged and warning-free. This file is ONLY the custom-domain analogue, forced
-- onto the dummy cert because we cannot hold a trusted cert for a domain we don't
-- control. Body cached after first read, same idiom as router.lua.

local PAGE = "/usr/share/nginx/html/domain_unconfigured.html"
local body  -- cached after first read

if not body then
	local f = io.open(PAGE, "r")
	if f then
		body = f:read("*a")
		f:close()
	else
		body = "Domain not configured.\n"
	end
end

ngx.status = ngx.HTTP_NOT_FOUND
ngx.header["Content-Type"] = "text/html; charset=utf-8"
ngx.print(body)
return ngx.exit(ngx.HTTP_NOT_FOUND)
