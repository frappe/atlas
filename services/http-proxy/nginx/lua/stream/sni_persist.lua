local cjson = require("cjson.safe")

local MAP_PATH = "/var/lib/nginx/sni-map.json"
local TMP_PATH = MAP_PATH .. ".tmp"

local LAST_DUMP_KEY = "sni_last_dump"

local persist = {}

local dump_scheduled = false

-- Stream and HTTP have separate dictionaries, so stream state has its own file.
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

function persist.last_dump()
	return ngx.shared.stream_meta:get(LAST_DUMP_KEY)
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
		ngx.log(ngx.ERR, "sni_persist: timer failed: ", err, " - dumping inline")
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
		ngx.log(ngx.ERR, "sni_persist: sni-map.json is not an object; ignoring")
		return
	end
	for domain, backend in pairs(map) do
		ngx.shared.domains:set(domain, backend)
	end
end

return persist
