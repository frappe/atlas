-- acme_persist.lua — the :80 custom-domain ACME map's snapshot twin
-- (spec/12-proxy.md § The stream front-door; spec/13-tls.md § Custom domains).
--
-- The http{}-side twin of persist.lua, pointed at the `acme_domains` shared dict
-- (the :80 ACME-passthrough map: custom-domain host -> "[<v6>]" the VM's bare v6,
-- bracketed) and acme-map.json instead of `sites` / map.json. It lives in http{}
-- because the :80 ACME fork is a Host-header decision in the http subsystem, and
-- http{} lua_shared_dicts are a separate address space from stream{}'s `domains`.
--
-- This is the SECOND physical copy of the one logical custom-domain map: the
-- stream-side `domains` dict (sni_persist) holds the :443 SNI map, and this http-side
-- `acme_domains` dict holds the :80 ACME map. Both carry the SAME row set (every active
-- custom domain — there is no readiness gate); they differ only in value shape (the SNI
-- map appends :443, the ACME map is the bare bracketed v6 so the VM can run HTTP-01).
-- Cross-subsystem dict sharing is unimplemented, so the controller writes both copies
-- on the same reconcile.
--
-- The value is the VM's bare v6 (e.g. "[2400::a]") — the :80 server's proxy_pass
-- brackets it into "http://[<v6>]:80". Serialization is byte-identical to the
-- controller's canonical_json (sorted keys, 2-space indent, trailing newline).

local cjson = require("cjson.safe")

local MAP_PATH = "/var/lib/nginx/acme-map.json"
local TMP_PATH = MAP_PATH .. ".tmp"

local persist = {}

local dump_scheduled = false

-- Distinct from persist.lua's last-dump key — they share the http `meta` dict but
-- track separate maps, so the keys must not collide.
local LAST_DUMP_KEY = "acme_last_dump"

function persist.serialize()
	local keys = ngx.shared.acme_domains:get_keys(0)
	table.sort(keys)
	if #keys == 0 then
		return "{}\n"
	end
	local parts = {}
	for i = 1, #keys do
		local key = keys[i]
		local value = ngx.shared.acme_domains:get(key)
		parts[i] = '  ' .. cjson.encode(key) .. ': ' .. cjson.encode(value)
	end
	return '{\n' .. table.concat(parts, ',\n') .. '\n}\n'
end

function persist.dump()
	local body = persist.serialize()
	local f, err = io.open(TMP_PATH, "w")
	if not f then
		ngx.log(ngx.ERR, "acme_persist: cannot open ", TMP_PATH, ": ", err)
		return false
	end
	f:write(body)
	f:close()
	local ok, rename_err = os.rename(TMP_PATH, MAP_PATH)
	if not ok then
		ngx.log(ngx.ERR, "acme_persist: rename failed: ", rename_err)
		return false
	end
	ngx.shared.meta:set(LAST_DUMP_KEY, ngx.now())
	return true
end

function persist.last_dump()
	return ngx.shared.meta:get(LAST_DUMP_KEY)
end

function persist.schedule_dump()
	if dump_scheduled then
		return
	end
	dump_scheduled = true
	local ok, err = ngx.timer.at(1, function()
		dump_scheduled = false
		persist.dump()
	end)
	if not ok then
		dump_scheduled = false
		ngx.log(ngx.ERR, "acme_persist: timer failed: ", err, " — dumping inline")
		persist.dump()
	end
end

function persist.load()
	local f = io.open(MAP_PATH, "r")
	if not f then
		return
	end
	local body = f:read("*a")
	f:close()
	local map = cjson.decode(body)
	if type(map) ~= "table" then
		ngx.log(ngx.ERR, "acme_persist: acme-map.json is not an object; ignoring")
		return
	end
	for domain, backend in pairs(map) do
		ngx.shared.acme_domains:set(domain, backend)
	end
end

return persist
