-- stream_admin.lua — the TCP control path, unix-socket line-protocol server only
-- (spec/17-tcp-proxy.md § control plane).
--
-- The stream{} sibling of admin.lua. It exists as a SEPARATE admin surface
-- because http{} and stream{} lua_shared_dicts are separate address spaces: the
-- http admin (admin.lua, on /run/nginx/admin.sock) physically cannot write
-- the stream `ports` dict, so the TCP map needs its own admin server inside
-- stream{}, on its own socket /run/nginx/stream-admin.sock.
--
-- It speaks a minimal LINE PROTOCOL, not HTTP: the stream content phase reads raw
-- bytes off ngx.req.socket(), there is no parsed HTTP request, and a 3-verb line
-- protocol is less code than an HTTP parser and just as auditable:
--
--   GET\n                    -> the whole map as canonical JSON
--   SYNC\n<canonical-json>   -> bulk declarative replace (add + remove), then dump
--   DUMP\n                   -> force an immediate persist; reply "ok\n"
--   STAT\n                   -> {"entries":N,"last_dump":epoch|null} as JSON+\n —
--                              the L4 line-protocol analogue of the http admin's
--                              GET /healthz (no http listener exists in stream{}).
--
-- Reachable ONLY over the unix socket (nginx.conf binds it on a unix listener,
-- never TCP). Auth is the socket's file permissions: the only thing that can
-- reach it is a process inside the guest, and the only way Atlas gets there is
-- SSH-to-the-guest. No token — exactly like admin.lua.

local cjson = require("cjson.safe")
local persist = require("stream_persist")

local ports = ngx.shared.ports

-- The largest SYNC body we will accumulate before giving up. The whole regional
-- map is well under this (10000 ports x ~40 bytes ~= 400 KiB); a body that blows
-- past it is a buggy or hostile caller, so cap it rather than grow unbounded.
local MAX_SYNC_BYTES = 16 * 1024 * 1024

local sock = assert(ngx.req.socket())

-- Bound every receive() so a client that opens the socket and then stalls without
-- sending a whole object (or without closing) cannot pin a stream worker on a
-- blocking read — the stream content phase has no built-in request timeout the way
-- http{} does. stream-admin always half-closes after the body, so a well-behaved
-- caller is unaffected; this only fences a slowloris-style hold. Read/send/connect
-- timeouts in ms.
sock:settimeouts(5000, 5000, 5000)

-- The verb is the first line. receive() with no arg reads one line, newline
-- stripped (the cosocket default is line mode "*l").
local verb, err = sock:receive()
if not verb then
	ngx.log(ngx.ERR, "stream_admin: read verb failed: ", err)
	return ngx.exit(ngx.ERROR)
end

if verb == "GET" then
	-- The whole `ports` dict as canonical sorted pretty JSON (for the byte-diff).
	ngx.print(persist.serialize())
	return
end

if verb == "DUMP" then
	local ok = persist.dump()
	ngx.print(ok and "ok\n" or "error\n")
	return
end

if verb == "STAT" then
	-- entry count + last-dump epoch, the line-protocol twin of the http admin's
	-- GET /healthz. last_dump is null until a dump has landed (a fresh boot that has
	-- only load()ed). Lets the operator/controller ask a TCP proxy "how many ports
	-- do you hold, and when did you last persist?" — symmetric to the http side.
	local keys = ports:get_keys(0)
	-- A nil last_dump would be DROPPED from the Lua table (and the key would vanish
	-- from the JSON); emit cjson.null so the field is always present as JSON `null`
	-- until a dump lands — symmetric to the http /healthz shape.
	ngx.print(cjson.encode({ entries = #keys, last_dump = persist.last_dump() or cjson.null }), "\n")
	return
end

if verb == "SYNC" then
	-- The body is the canonical JSON object on the lines AFTER the verb. The
	-- serializer always ends the object with a "}" line (or is the single line
	-- "{}"), so read lines and stop as soon as the accumulated text decodes as a
	-- table — robust against whether the client half-closes or holds the
	-- connection open. cjson.decode on a partial object returns nil, so we keep
	-- reading until it is whole.
	local accumulated = {}
	local accumulated_bytes = 0
	local desired
	while true do
		local line, line_err = sock:receive()
		if not line then
			-- Client closed (or timed out) before we got a whole object. cjson on the
			-- bytes we DID get tells a finite non-object body (a bare scalar like "42"
			-- or "not json") apart from a genuinely truncated object ("{"): the former
			-- decodes to a non-table, the latter to nil. Give each its own diagnostic
			-- so a buggy caller sees the right reason (mirrors admin.lua, which can
			-- distinguish them because it has the whole HTTP body up front).
			local decoded = cjson.decode(table.concat(accumulated, "\n"))
			if decoded ~= nil and type(decoded) ~= "table" then
				ngx.print("error: body must be a JSON object of port->backend strings\n")
				return
			end
			ngx.log(ngx.ERR, "stream_admin: SYNC body read failed: ", line_err)
			ngx.print("error: incomplete body\n")
			return
		end
		accumulated[#accumulated + 1] = line
		accumulated_bytes = accumulated_bytes + #line + 1
		if accumulated_bytes > MAX_SYNC_BYTES then
			-- A body this large is a buggy/hostile caller, never the real map. Reject
			-- rather than accumulate unbounded memory.
			ngx.log(ngx.ERR, "stream_admin: SYNC body exceeded ", MAX_SYNC_BYTES, " bytes")
			ngx.print("error: body too large\n")
			return
		end
		local decoded = cjson.decode(table.concat(accumulated, "\n"))
		if type(decoded) == "table" then
			desired = decoded
			break
		end
	end

	-- Validate the WHOLE body before mutating anything, so a malformed entry
	-- rejects the sync atomically rather than leaving the live map half-written
	-- (mirrors admin.lua's /sync guard). Every entry must be string port ->
	-- string backend: cjson decodes a JSON array to a Lua table too (e.g. [1,2]
	-- -> {[1]=1,[2]=2}), which would otherwise inject numeric "ports"/backends
	-- into the dict — and ports:set() rejects a non-string value outright. The
	-- controller only ever sends a proper object; this guards a buggy caller.
	for port, backend in pairs(desired) do
		if type(port) ~= "string" or type(backend) ~= "string" then
			ngx.print("error: body must be a JSON object of port->backend strings\n")
			return
		end
	end

	-- Bulk declarative replace: make the dict match `desired` exactly (added AND
	-- removed). Upsert desired, then delete keys not in it — never a window where
	-- the dict is empty under a concurrent preread read (the same shape as
	-- admin.lua's /sync). Idempotent, self-healing, rebuild-safe.
	local existing = ports:get_keys(0)
	local keep = {}
	for port, backend in pairs(desired) do
		ports:set(port, backend)
		keep[port] = true
	end
	for i = 1, #existing do
		if not keep[existing[i]] then
			ports:delete(existing[i])
		end
	end
	persist.schedule_dump()
	ngx.print("ok\n")
	return
end

ngx.print("error: unknown verb\n")
