-- stream_router.lua — the TCP request path (spec/17-tcp-proxy.md § request path).
--
-- Runs in preread_by_lua: the only routing key raw TCP gives us is the local
-- port the connection landed on (ngx.var.server_port). Look it up in the `ports`
-- shared dict and, on a hit, set ngx.var.tcp_upstream to the dialable
-- "[<v6>]:<port>" literal — the server block's `proxy_pass $tcp_upstream` then
-- dials it. There is no resolver and no upstream{} block: the value is always an
-- IP literal (the VM's /128, denormalized into Port Mapping.address), so
-- proxy_pass treats it as a literal address, never a name to resolve.
--
-- One dict read per connection, no allocation. On a miss there is no branded
-- page for raw TCP — ngx.exit with an error status drops the connection (the
-- client just sees a closed socket), which is the right behavior for an unmapped
-- port.

local ports = ngx.shared.ports

-- ngx.var.server_port is the local port the connection landed on — the routing
-- key. The dict is keyed by the port as a STRING (canonical_json keys are JSON
-- object keys, i.e. strings), so look it up as a string.
local port = ngx.var.server_port or ""

local backend = ports:get(port)
if not backend then
	-- Unmapped port: drop the connection. No body, no page — this is L4.
	return ngx.exit(ngx.ERROR)
end

-- Hand the dialable literal to proxy_pass via the per-connection variable.
ngx.var.tcp_upstream = backend
