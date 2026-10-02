from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

from .auth import CONTROL_BEARER_SCHEME

REFERENCE_PATH = "/docs"
SCHEMA_PATH = "/docs/swagger.json"

REFERENCE_PAGE = """<!doctype html>
<html lang="en">
	<head>
		<title>{title} API</title>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
	</head>
	<body>
		<div id="app"></div>
		<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
		<script>
			Scalar.createApiReference("#app", {{
				url: "{schema_path}",
				defaultHttpClient: {{ targetKey: "shell", clientKey: "curl" }},
				persistAuth: false,
				authentication: {{
					preferredSecurityScheme: "{security_scheme}",
				}},
				agent: {{
					disabled: true,
				}}
			}});
		</script>
	</body>
</html>
"""


def operation_id(route: APIRoute) -> str:
	return route.name


def add_routes(app: FastAPI) -> None:
	"""Add API reference routes. The page title comes from the app title."""
	reference_response = HTMLResponse(
		REFERENCE_PAGE.format(title=app.title, schema_path=SCHEMA_PATH, security_scheme=CONTROL_BEARER_SCHEME)
	)
	schema_response = JSONResponse(app.openapi())

	@app.get(REFERENCE_PATH, include_in_schema=False)
	async def reference() -> Response:
		return reference_response

	@app.get(SCHEMA_PATH, include_in_schema=False)
	async def schema() -> Response:
		return schema_response
