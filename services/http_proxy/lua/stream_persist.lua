-- stream_persist.lua — the stream{}-side twin of persist.lua (spec/17-tcp-proxy.md).
--
-- The byte-for-byte twin of persist.lua, pointed at the `ports` shared dict (the
-- TCP port→backend map) and stream-map.json instead of `sites` / map.json. It
-- lives in the stream{} subsystem because http{} and stream{} lua_shared_dicts
-- are SEPARATE address spaces (OpenResty tracks cross-subsystem sharing as
-- unimplemented), so the TCP map needs its own persist module in stream{}.
--
-- The serialization MUST be byte-identical to the Atlas side's
-- json.dumps(map, sort_keys=True, indent=2): sorted keys, 2-space indent, one
-- key per line, trailing newline — the SAME canonical_json proxy.py / tcp_proxy.py
-- emit, so the reconcile "in sync?" check is a plain byte compare (§7.2 / §control
-- plane). lua-cjson guarantees neither key order nor indent, so we encode the
-- object by hand and use cjson only to escape the string values.
--
-- The values are ready-to-dial "[<v6>]:<port>" literals (ASCII), so there is no
-- Unicode-escaping divergence between cjson and Python to worry about.

local cjson = require("cjson.safe")

local MAP_PATH = "/var/lib/nginx/stream-map.json"
local TMP_PATH = MAP_PATH .. ".tmp"

local persist = {}

-- A debounce flag so a burst of writes coalesces into one dump.
local dump_scheduled = false

-- Epoch seconds of the last successful dump, kept in the cross-worker `stream_meta`
-- shared dict (not a worker-local upvalue) so a STAT from any worker reports the
-- dump any worker made — the twin of persist.lua's last_dump tracking, the one
-- piece the stream side previously lacked. The dict is `stream_meta`, not `meta`,
-- because a lua_shared_dict zone NAME is global to nginx (it would collide with
-- http{}'s `meta`). Exposed via persist.last_dump() for the stream_admin STAT verb.
local LAST_DUMP_KEY = "last_dump"

-- Serialize the whole `ports` dict to canonical JSON bytes:
--   {}                      (empty)
--   {\n  "10000": "[2400::a]:22",\n  "10001": "[2400::b]:3306"\n}\n
function persist.serialize()
	local keys = ngx.shared.ports:get_keys(0)
	table.sort(keys)
	if #keys == 0 then
		return "{}\n"
	end
	local parts = {}
	for i = 1, #keys do
		local key = keys[i]
		local value = ngx.shared.ports:get(key)
		parts[i] = '  ' .. cjson.encode(key) .. ': ' .. cjson.encode(value)
	end
	return '{\n' .. table.concat(parts, ',\n') .. '\n}\n'
end

-- Atomic dump: write temp, fsync via rename. Never a torn file.
function persist.dump()
	local body = persist.serialize()
	local f, err = io.open(TMP_PATH, "w")
	if not f then
		ngx.log(ngx.ERR, "stream_persist: cannot open ", TMP_PATH, ": ", err)
		return false
	end
	f:write(body)
	f:close()
	local ok, rename_err = os.rename(TMP_PATH, MAP_PATH)
	if not ok then
		ngx.log(ngx.ERR, "stream_persist: rename failed: ", rename_err)
		return false
	end
	ngx.shared.stream_meta:set(LAST_DUMP_KEY, ngx.now())
	return true
end

-- Epoch seconds of the most recent successful dump (any worker), or nil if none
-- has happened yet (e.g. a fresh boot that has only loaded). For the STAT verb.
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
		ngx.log(ngx.ERR, "stream_persist: timer failed: ", err, " — dumping inline")
		persist.dump()
	end
end

-- Load stream-map.json into the `ports` dict at worker init. Absent file (fresh
-- image) is fine — Atlas's next reconcile refills the dict. Only ever called at
-- start.
function persist.load()
	local f = io.open(MAP_PATH, "r")
	if not f then
		return
	end
	local body = f:read("*a")
	f:close()
	local map = cjson.decode(body)
	if type(map) ~= "table" then
		ngx.log(ngx.ERR, "stream_persist: stream-map.json is not an object; ignoring")
		return
	end
	for port, backend in pairs(map) do
		ngx.shared.ports:set(port, backend)
	end
end

return persist
