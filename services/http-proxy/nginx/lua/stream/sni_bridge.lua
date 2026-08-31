local cjson = require("cjson.safe")
local sni_persist = require("sni_persist")

local domains = ngx.shared.domains

-- HTTP admin updates use this short private line protocol to reach stream{}.
local function normalize_backend(address)
	if address:sub(1, 1) == "[" then
		if address:match("%]:%d+$") then
			return address
		end
		return address .. ":443"
	end
	return "[" .. address .. "]:443"
end

local function valid_address(address)
	return type(address) == "string" and address ~= ""
end

local function replace_map(desired)
	local existing = domains:get_keys(0)
	local keep = {}
	for key, address in pairs(desired) do
		if type(key) ~= "string" or not valid_address(address) then
			return false
		end
		keep[key] = true
	end
	for key, address in pairs(desired) do
		domains:set(key, normalize_backend(address))
	end
	for _, key in ipairs(existing) do
		if not keep[key] then
			domains:delete(key)
		end
	end
	return true
end

local function read_json(socket)
	local line, err = socket:receive("*l")
	if not line then
		return nil, err
	end
	local value, decode_error = cjson.decode(line)
	if not value then
		return nil, decode_error or "invalid JSON"
	end
	return value
end

local socket = assert(ngx.req.socket())
socket:settimeouts(5000, 5000, 5000)
local command, err = socket:receive("*l")
if not command then
	ngx.log(ngx.ERR, "sni_bridge: command read failed: ", err)
	return ngx.exit(ngx.ERROR)
end

local payload, payload_error = read_json(socket)
if type(payload) ~= "table" then
	ngx.print("error: ", payload_error or "body must be a JSON object\n")
	return
end

if command == "SYNC" then
	if not replace_map(payload) then
		ngx.print("error: body must be an object of domain->address strings\n")
		return
	end
elseif command == "PATCH" then
	if type(payload.key) ~= "string" or not valid_address(payload.address) then
		ngx.print("error: body must contain string key and address\n")
		return
	end
	domains:set(payload.key, normalize_backend(payload.address))
elseif command == "DELETE" then
	if type(payload.key) ~= "string" then
		ngx.print("error: body must contain a string key\n")
		return
	end
	domains:delete(payload.key)
else
	ngx.print("error: unknown command\n")
	return
end

sni_persist.schedule_dump()
ngx.print("ok\n")
