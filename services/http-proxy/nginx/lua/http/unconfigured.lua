
local pages = require("pages")

return pages.serve("domain_unconfigured", ngx.HTTP_NOT_FOUND)
