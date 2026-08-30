-- sni_persist.lua — the custom-domain SNI map's snapshot twin (spec/12-proxy.md
-- § The stream front-door; spec/18 Phase 2).
--
-- The byte-for-byte twin of stream_persist.lua, pointed at the `domains` shared
-- dict (the custom-domain SNI map: full host -> "[<v6>]:443") and sni-map.json
-- instead of `ports` / stream-map.json. It lives in the stream{} subsystem
-- alongside stream_persist because the custom-domain :443 SNI map is stream-side
-- (ssl_preread is a stream module) and stream{} lua_shared_dicts are a separate
-- address space from http{}'s.
--
-- The serialization MUST be byte-identical to the controller's
-- json.dumps(map, sort_keys=True, indent=2) (atlas.atlas.proxy.canonical_json),
-- so the reconcile "in sync?" check is a plain byte compare — the same canonical
-- form persist.lua / stream_persist.lua emit. lua-cjson guarantees neither key
-- order nor indent, so we encode the object by hand and use cjson only to escape
-- the string values (ASCII "[<v6>]:443" literals — no Unicode divergence).

local cjson = require("cjson.safe")

local MAP_PATH = "/var/lib/nginx/sni-map.json"
local TMP_PATH = MAP_PATH .. ".tmp"

local persist = {}

-- A debounce flag so a burst of writes coalesces into one dump.
local dump_scheduled = false

-- Epoch seconds of the last successful dump, kept in the cross-worker `stream_meta`
-- shared dict (shared with stream_persist under a DISTINCT key so the two stream
-- maps' dump times never collide). Exposed via persist.last_dump() for the
-- stream_admin STAT-SNI verb.
local LAST_DUMP_KEY = "sni_last_dump"

-- Serialize the whole `domains` dict to canonical JSON bytes:
--   {}                                       (empty)
--   {\n  "shop.acme.com": "[2400::a]:443"\n}\n
function persist.serialize()
	local keys = ngx.shared.domains:get_keys(0)
	table.sort(keys)
	if #keys == 0 then
		return "{}\n"
	end
	local parts = {}
	for i = 1, #keys do
		local key = keys[i]
		local value = ngx.shared.domains:get(key)
		parts[i] = '  ' .. cjson.encode(key) .. ': ' .. cjson.encode(value)
	end
	return '{\n' .. table.concat(parts, ',\n') .. '\n}\n'
end

-- Atomic dump: write temp, fsync via rename. Never a torn file.
function persist.dump()
	local body = persist.serialize()
	local f, err = io.open(TMP_PATH, "w")
	if not f then
		ngx.log(ngx.ERR, "sni_persist: cannot open ", TMP_PATH, ": ", err)
		return false
	end
	f:write(body)
	f:close()
	local ok, rename_err = os.rename(TMP_PATH, MAP_PATH)
	if not ok then
		ngx.log(ngx.ERR, "sni_persist: rename failed: ", rename_err)
		return false
	end
	ngx.shared.stream_meta:set(LAST_DUMP_KEY, ngx.now())
	return true
end

-- Epoch seconds of the most recent successful dump (any worker), or nil if none
-- has happened yet (e.g. a fresh boot that has only loaded). For the STAT-SNI verb.
function persist.last_dump()
	return ngx.shared.stream_meta:get(LAST_DUMP_KEY)
end

-- Debounced dump: schedule a single dump 1s out, collapsing a write burst.
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
		ngx.log(ngx.ERR, "sni_persist: timer failed: ", err, " — dumping inline")
		persist.dump()
	end
end

-- Load sni-map.json into the `domains` dict at worker init. Absent file (fresh
-- image) is fine — the controller's next reconcile refills the dict. Only ever
-- called at start.
function persist.load()
	local f = io.open(MAP_PATH, "r")
	if not f then
		return
	end
	local body = f:read("*a")
	f:close()
	local map = cjson.decode(body)
	if type(map) ~= "table" then
		ngx.log(ngx.ERR, "sni_persist: sni-map.json is not an object; ignoring")
		return
	end
	for domain, backend in pairs(map) do
		ngx.shared.domains:set(domain, backend)
	end
end

return persist
