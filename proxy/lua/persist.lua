-- persist.lua — snapshot the live dict to disk and reload it (proxy-design.md §6.3).
--
-- The shared dict is the source of truth in-process, but shared memory is wiped
-- on restart/reboot/rebuild. So we DUMP it to /var/lib/atlas-proxy/map.json
-- (atomic temp + rename) and LOAD it back at worker init. The file is read ONLY
-- at start; Atlas's reconcile (§7) is the durable backstop if the file is lost.
--
-- The serialization MUST be byte-identical to the Atlas side's
-- json.dumps(map, sort_keys=True, indent=2): sorted keys, 2-space indent, one
-- key per line, trailing newline. That makes "in sync?" a plain byte-equality
-- check (§7.2). lua-cjson guarantees neither key order nor indent, so we encode
-- the object by hand and use cjson only to escape the string values.

local cjson = require("cjson.safe")

local MAP_PATH = "/var/lib/atlas-proxy/map.json"
local TMP_PATH = MAP_PATH .. ".tmp"

local persist = {}

-- A debounce flag so a burst of writes coalesces into one dump (§6.3).
local dump_scheduled = false

-- Epoch seconds of the last successful dump, kept in the cross-worker `meta`
-- shared dict (not a worker-local upvalue) so GET /healthz reports the dump
-- regardless of which worker handled it. Exposed via persist.last_dump() (§6.2).
local LAST_DUMP_KEY = "last_dump"

-- Encode one string value exactly as Python's json does for a plain string:
-- cjson.encode("x") yields the quoted, escaped JSON string. Site addresses are
-- ASCII v6 literals (or "-"), so there is no Unicode-escaping divergence to
-- worry about between cjson and Python here.
local function encode_value(value)
    return cjson.encode(value)
end

-- Serialize the whole dict to canonical JSON bytes:
--   {}                      (empty)
--   {\n  "a": "1",\n  "b": "2"\n}\n
function persist.serialize()
    local keys = ngx.shared.sites:get_keys(0)
    table.sort(keys)
    if #keys == 0 then
        return "{}\n"
    end
    local parts = {}
    for i = 1, #keys do
        local key = keys[i]
        local value = ngx.shared.sites:get(key)
        parts[i] = '  ' .. cjson.encode(key) .. ': ' .. encode_value(value)
    end
    return '{\n' .. table.concat(parts, ',\n') .. '\n}\n'
end

-- Atomic dump: write temp, fsync via rename. Never a torn file.
function persist.dump()
    local body = persist.serialize()
    local f, err = io.open(TMP_PATH, "w")
    if not f then
        ngx.log(ngx.ERR, "persist: cannot open ", TMP_PATH, ": ", err)
        return false
    end
    f:write(body)
    f:close()
    local ok, rename_err = os.rename(TMP_PATH, MAP_PATH)
    if not ok then
        ngx.log(ngx.ERR, "persist: rename failed: ", rename_err)
        return false
    end
    ngx.shared.meta:set(LAST_DUMP_KEY, ngx.now())
    return true
end

-- Epoch seconds of the most recent successful dump (any worker), or nil if none
-- has happened yet (e.g. a fresh boot that has only loaded). For GET /healthz.
function persist.last_dump()
    return ngx.shared.meta:get(LAST_DUMP_KEY)
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
        ngx.log(ngx.ERR, "persist: timer failed: ", err, " — dumping inline")
        persist.dump()
    end
end

-- Load map.json into the dict at worker init. Absent file (fresh image) is fine
-- — Atlas's next reconcile refills the dict. Only ever called at start.
function persist.load()
    local f = io.open(MAP_PATH, "r")
    if not f then
        return
    end
    local body = f:read("*a")
    f:close()
    local map = cjson.decode(body)
    if type(map) ~= "table" then
        ngx.log(ngx.ERR, "persist: map.json is not an object; ignoring")
        return
    end
    for subdomain, addr in pairs(map) do
        ngx.shared.sites:set(subdomain, addr)
    end
end

return persist
