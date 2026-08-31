local lookup = {}

function lookup.get(dict, host)
	local value = dict:get(host)
	if value then
		return value
	end

	for index = 1, #host do
		local separator = host:sub(index, index)
		if separator == "." or separator == "-" then
			value = dict:get("*" .. host:sub(index))
			if value then
				return value
			end
		end
	end
end

return lookup
