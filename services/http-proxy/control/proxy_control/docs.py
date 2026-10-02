from atlas_control.auth import CONTROL_BEARER_SCHEME
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse

REFERENCE_PATH = "/docs"
SCHEMA_PATH = "/docs/swagger.json"

REFERENCE_PAGE = f"""<!doctype html>
<html lang="en">
	<head>
		<title>Atlas Proxy Control API</title>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
	</head>
	<body>
		<div id="app"></div>
		<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
		<script>
			Scalar.createApiReference("#app", {{
				url: "{SCHEMA_PATH}",
				defaultHttpClient: {{ targetKey: "shell", clientKey: "curl" }},
				persistAuth: false,
				authentication: {{
					preferredSecurityScheme: "{CONTROL_BEARER_SCHEME}",
				}},
				agent: {{
					disabled: true,
				}}
			}});
		</script>
	</body>
</html>
"""


def add_routes(app: FastAPI) -> None:
	"""Add API reference routes."""
	reference_response = HTMLResponse(REFERENCE_PAGE)
	schema_response = JSONResponse(app.openapi())

	@app.get(REFERENCE_PATH, include_in_schema=False)
	async def reference() -> Response:
		return reference_response

	@app.get(SCHEMA_PATH, include_in_schema=False)
	async def schema() -> Response:
		return schema_response
