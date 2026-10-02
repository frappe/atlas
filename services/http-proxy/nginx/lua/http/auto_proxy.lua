local auto_proxy = {}

local BASE = 36
local HEXTET_MODULUS = 65536

local function digit_value(byte)
	if byte >= string.byte("0") and byte <= string.byte("9") then
		return byte - string.byte("0")
	end
	if byte >= string.byte("a") and byte <= string.byte("z") then
		return byte - string.byte("a") + 10
	end
end

local function append_digit(address_parts, digit)
	-- Keep the 48-bit value in three 16-bit address parts.
	local carry = digit
	for position = #address_parts, 1, -1 do
		local value = address_parts[position] * BASE + carry
		address_parts[position] = value % HEXTET_MODULUS
		carry = (value - address_parts[position]) / HEXTET_MODULUS
	end
	return carry == 0
end

local function decode_token(token)
	local address_parts = { 0, 0, 0 }
	for position = 1, #token do
		local digit = digit_value(token:byte(position))
		if not digit or not append_digit(address_parts, digit) then
			return nil
		end
	end
	return address_parts
end

local function matches_prefix(host_prefix, configured_prefix)
	if host_prefix == configured_prefix then
		return true
	end
	if configured_prefix:sub(1, 1) ~= "*" then
		return false
	end
	local suffix = configured_prefix:sub(2)
	-- A wildcard prefix needs text before its configured suffix.
	return #host_prefix > #suffix and host_prefix:sub(-#suffix) == suffix
end

local function is_allowed_prefix(host_prefix, host_prefixes)
	for _, configured_prefix in ipairs(host_prefixes or {}) do
		if matches_prefix(host_prefix, configured_prefix) then
			return true
		end
	end
	return false
end

local function format_address(address_prefix, address_parts)
	-- The label puts the VM number before the tenant, and the address puts it after.
	return string.format("%s:%x:%x::%x", address_prefix, address_parts[2], address_parts[3], address_parts[1])
end

function auto_proxy.get_virtual_machine_address(subdomain, address_prefix, host_prefixes)
	if not address_prefix or address_prefix == "" then
		return nil
	end

	local host_prefix, token = subdomain:match("^(.-)([0-9a-z]+)$")
	-- A label has one canonical base-36 spelling.
	if not token or #token > 1 and token:sub(1, 1) == "0" then
		return nil
	end

	if not is_allowed_prefix(host_prefix, host_prefixes) then
		return nil
	end

	local address_parts = decode_token(token)
	if not address_parts then
		return nil
	end

	return format_address(address_prefix, address_parts)
end

return auto_proxy
