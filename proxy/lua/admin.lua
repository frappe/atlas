-- admin.lua — the control path, unix-socket server only (proxy-design.md §6.2).
--
-- Reachable ONLY over /run/nginx/admin.sock (nginx.conf binds it on a unix
-- listener, never TCP). Auth is the socket's file permissions: the only
-- thing that can reach it is a process inside the guest, and the only way Atlas
-- gets there is SSH-to-the-guest. No token.
--
-- Every write updates the shared dict, then schedules a debounced dump so the
-- on-disk snapshot (map.json) follows. GET /map re-emits the canonical sorted
-- JSON so the controller's byte-equality "in sync?" check is meaningful (§7.2).

local cjson = require("cjson.safe")
local persist = require("persist")

local sites = ngx.shared.sites

local function send(status, body, content_type)
    ngx.status = status
    ngx.header["Content-Type"] = content_type or "text/plain"
    if body then
        ngx.print(body)
    end
    return ngx.exit(status)
end

local function send_json(status, tbl)
    return send(status, cjson.encode(tbl), "application/json")
end

local function read_body()
    ngx.req.read_body()
    local body = ngx.req.get_body_data()
    if body then
        return body
    end
    -- Large bodies spill to a temp file rather than memory.
    local path = ngx.req.get_body_file()
    if not path then
        return ""
    end
    local f = io.open(path, "r")
    if not f then
        return ""
    end
    local data = f:read("*a")
    f:close()
    return data
end

local method = ngx.req.get_method()
local uri = ngx.var.uri

-- GET /healthz — nginx up + dict entry count + last-dump time (§6.2). last_dump
-- is epoch seconds of the most recent map.json write by any worker (tracked in
-- the cross-worker `meta` dict), or null if none has happened yet (a fresh boot
-- that has only loaded).
if method == "GET" and uri == "/healthz" then
    local keys = sites:get_keys(0)
    return send_json(200, { ok = true, entries = #keys, last_dump = persist.last_dump() })
end

-- GET /map — the whole dict as canonical sorted pretty JSON (for the diff).
if method == "GET" and uri == "/map" then
    return send(200, persist.serialize(), "application/json")
end

-- POST /dump — force an immediate persist to disk.
if method == "POST" and uri == "/dump" then
    local ok = persist.dump()
    return send_json(ok and 200 or 500, { dumped = ok })
end

-- POST /sync — bulk declarative replace: make the dict match the body exactly
-- (added AND removed), atomically from the caller's view, then dump. This is
-- the primary control path (§7.2): idempotent, self-healing, rebuild-safe.
if method == "POST" and uri == "/sync" then
    local desired = cjson.decode(read_body())
    if type(desired) ~= "table" then
        return send_json(400, { error = "body must be a JSON object" })
    end
    -- Validate the WHOLE body before mutating anything, so a malformed entry
    -- rejects the sync atomically rather than leaving the live map half-written.
    -- Every entry must be string subdomain -> string address: cjson decodes a
    -- JSON array to a Lua table too (e.g. [1,2] -> {[1]=1,[2]=2}), which would
    -- otherwise inject numeric "subdomains"/addresses into the dict. The
    -- controller only ever sends a proper object; this guards a buggy caller.
    for subdomain, addr in pairs(desired) do
        if type(subdomain) ~= "string" or type(addr) ~= "string" then
            return send_json(400, { error = "body must be a JSON object of subdomain->address strings" })
        end
    end
    -- Remove keys not in desired, then upsert desired. flush_all + reinsert is
    -- simpler and the dict is small; get_keys + targeted delete avoids a window
    -- where the dict is empty under concurrent reads, so prefer that.
    local existing = sites:get_keys(0)
    local keep = {}
    for subdomain, addr in pairs(desired) do
        sites:set(subdomain, addr)
        keep[subdomain] = true
    end
    for i = 1, #existing do
        if not keep[existing[i]] then
            sites:delete(existing[i])
        end
    end
    persist.schedule_dump()
    return send_json(200, { synced = true, entries = sites:get_keys(0) and #sites:get_keys(0) or 0 })
end

-- Per-subdomain routes: /map/<sub>
local subdomain = uri:match("^/map/(.+)$")
if subdomain then
    if method == "GET" then
        local addr = sites:get(subdomain)
        if not addr then
            return send_json(404, { error = "no such subdomain" })
        end
        return send_json(200, { subdomain = subdomain, address = addr })
    elseif method == "PUT" then
        local addr = read_body():gsub("%s+$", "")
        if addr == "" then
            return send_json(400, { error = "empty address" })
        end
        sites:set(subdomain, addr)
        persist.schedule_dump()
        return send_json(200, { subdomain = subdomain, address = addr })
    elseif method == "DELETE" then
        sites:delete(subdomain)
        persist.schedule_dump()
        return send_json(200, { deleted = subdomain })
    end
    return send_json(405, { error = "method not allowed" })
end

return send_json(404, { error = "unknown route" })
