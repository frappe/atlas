local cjson = require("cjson.safe")
local persist = require("persist")
local domains_http_persist = require("domains_http_persist")

local sites = ngx.shared.sites
local domains_http = ngx.shared.domains_http
local meta = ngx.shared.meta
local SNI_SOCKET = "unix:/run/nginx/sni-bridge.sock"

local function table_size(value)
	local count = 0
	for _ in pairs(value) do
		count = count + 1
	end
	return count
end

local function send(status, body, content_type)
	ngx.status = status
	ngx.header["Content-Type"] = content_type or "text/plain"
	if body then
		ngx.print(body)
	end
	return ngx.exit(status)
end

local function send_json(status, body)
	return send(status, cjson.encode(body), "application/json")
end

local function read_body()
	ngx.req.read_body()
	local body = ngx.req.get_body_data()
	if body then
		return body
	end
	local path = ngx.req.get_body_file()
	if not path then
		return ""
	end
	local file = io.open(path, "r")
	if not file then
		return ""
	end
	local data = file:read("*a")
	file:close()
	return data
end

local function decode_object(body, error_message)
	local value = cjson.decode(body)
	if type(value) ~= "table" then
		return nil, error_message
	end
	for key, item in pairs(value) do
		if type(key) ~= "string" or type(item) ~= "string" then
			return nil, error_message
		end
	end
	return value
end

local function decode_address()
	local body = cjson.decode(read_body())
	if type(body) ~= "table" or type(body.address) ~= "string" then
		return nil, "body must be {\"address\":\"...\"}"
	end
	local address = body.address:gsub("%s+$", "")
	if address == "" then
		return nil, "empty address"
	end
	return address
end

local function replace_map(dict, desired, count_key)
	local existing = dict:get_keys(0)
	local keep = {}
	for key, value in pairs(desired) do
		dict:set(key, value)
		keep[key] = true
	end
	for _, key in ipairs(existing) do
		if not keep[key] then
			dict:delete(key)
		end
	end
	local entries = table_size(desired)
	meta:set(count_key, entries)
	return entries
end

local function sni_request(command, payload)
	local socket, err = ngx.socket.tcp()
	if not socket then
		return false, err
	end
	socket:settimeouts(5000, 5000, 5000)
	local ok, connect_error = socket:connect(SNI_SOCKET)
	if not ok then
		socket:close()
		return false, connect_error
	end
	local sent, send_error = socket:send(command .. "\n" .. cjson.encode(payload) .. "\n")
	if not sent then
		socket:close()
		return false, send_error
	end
	local reply, receive_error = socket:receive("*l")
	socket:close()
	if not reply then
		return false, receive_error
	end
	if reply ~= "ok" then
		return false, reply
	end
	return true
end

local function full_map(method, dict, store, error_message, bridge, count_key)
	if method == "GET" then
		return send(200, store.serialize(), "application/json")
	end
	if method ~= "PUT" then
		return send_json(405, { error = "method not allowed" })
	end
	local desired, err = decode_object(read_body(), error_message)
	if not desired then
		return send_json(400, { error = err })
	end
	if bridge then
		for _, address in pairs(desired) do
			if address == "" or address == "-" then
				return send_json(400, { error = "domain addresses cannot be tombstones" })
			end
		end
		local ok, bridge_error = sni_request("SYNC", desired)
		if not ok then
			return send_json(503, { error = "SNI bridge unavailable", detail = bridge_error })
		end
	end
	local entries = replace_map(dict, desired, count_key)
	store.schedule_dump()
	return send_json(200, { synced = true, entries = entries })
end

local function one_mapping(method, dict, store, key, kind, bridge, count_key)
	if method == "GET" then
		local address = dict:get(key)
		if not address then
			return send_json(404, { error = "no such " .. kind })
		end
		return send_json(200, { [kind] = key, address = address })
	end
	if method == "PATCH" then
		local address, err = decode_address()
		if not address then
			return send_json(400, { error = err })
		end
		if bridge and address == "-" then
			return send_json(400, { error = "domain addresses cannot be tombstones" })
		end
		if bridge then
			local ok, bridge_error = sni_request("PATCH", { key = key, address = address })
			if not ok then
				return send_json(503, { error = "SNI bridge unavailable", detail = bridge_error })
			end
		end
		if not dict:get(key) then
			meta:incr(count_key, 1, 0)
		end
		dict:set(key, address)
		store.schedule_dump()
		return send_json(200, { [kind] = key, address = address })
	end
	if method == "DELETE" then
		if bridge then
			local ok, bridge_error = sni_request("DELETE", { key = key })
			if not ok then
				return send_json(503, { error = "SNI bridge unavailable", detail = bridge_error })
			end
		end
		if dict:get(key) then
			dict:delete(key)
			meta:incr(count_key, -1, 0)
		end
		store.schedule_dump()
		return send(204)
	end
	return send_json(405, { error = "method not allowed" })
end

local function health()
	local site_count = meta:get("sites_count") or 0
	return send_json(200, {
		ok = true,
		boot_id = atlas_boot_id,
		entries = site_count,
		sites = site_count,
		domains = meta:get("domains_http_count") or 0,
		last_dump = persist.last_dump(),
		last_sites_dump = persist.last_dump(),
		last_domains_dump = domains_http_persist.last_dump(),
	})
end

local method = ngx.req.get_method()
local uri = ngx.var.uri

if method == "GET" and uri == "/v1/healthz" then
	return health()
end
if uri == "/v1/sites" then
	return full_map(method, sites, persist, "body must be a JSON object of subdomain->address strings", false, "sites_count")
end
if uri == "/v1/domains" then
	return full_map(method, domains_http, domains_http_persist, "body must be a JSON object of domain->address strings", true, "domains_http_count")
end
if uri == "/v1/sites/sync" then
	if method ~= "POST" then
		return send_json(405, { error = "method not allowed" })
	end
	return full_map("PUT", sites, persist, "body must be a JSON object of subdomain->address strings", false, "sites_count")
end
if uri == "/v1/domains/sync" then
	if method ~= "POST" then
		return send_json(405, { error = "method not allowed" })
	end
	return full_map("PUT", domains_http, domains_http_persist, "body must be a JSON object of domain->address strings", true, "domains_http_count")
end
if uri == "/v1/dump" and method == "POST" then
	local dumped = persist.dump()
	return send_json(dumped and 200 or 500, { dumped = dumped })
end

local collection, key = uri:match("^/v1/(sites|domains)/(.+)$")
if collection and key then
	if collection == "sites" then
		return one_mapping(method, sites, persist, key, "site", false, "sites_count")
	end
	return one_mapping(method, domains_http, domains_http_persist, key, "domain", true, "domains_http_count")
end

return send_json(404, { error = "unknown route" })
