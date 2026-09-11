from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from string import Template
from typing import TYPE_CHECKING, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel

if TYPE_CHECKING:
	from atlas.api.core.base import RouteMeta, Router
	from atlas.api.core.binding import ParameterBinding

DEFAULT_RESPONSES: dict[int, str] = {}
OPENAPI_VERSION = "3.1.0"
REFERENCE_TEMPLATE = "#/components/schemas/{model}"
PATH_PARAMETER_PATTERN = re.compile(r"<(?:(\w+):)?(\w+)>")
WERKZEUG_CONVERTER_TYPES = {
	"int": "integer",
	"float": "number",
	"string": "string",
	"path": "string",
}
SECURITY_SCHEMES: dict[str, dict[str, str]] = {
	"Token Based Authentication": {
		"type": "apiKey",
		"in": "header",
		"name": "Authorization",
		"description": "Use: token api_key:api_secret. [Learn more](https://docs.frappe.io/framework/user/en/api/rest#1-token-based-authentication)",
	},
	"Access Token Authentication": {
		"type": "http",
		"scheme": "bearer",
		"bearerFormat": "token",
		"description": "Use: Bearer <access_token>. [Learn more](https://docs.frappe.io/framework/user/en/api/rest#3-access-token)",
	},
	"Service Token Authentication": {
		"type": "http",
		"scheme": "bearer",
		"bearerFormat": "JWT",
		"description": "Use a Central or regional Atlas service token.",
	},
}
SCALAR_SCRIPT_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference"
API_REFERENCE_TEMPLATE = Template("""<!doctype html>
<html lang="en">
	<head>
		<title>$title</title>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
	</head>
	<body>
		<div id="app"></div>
		<script src="$script_url"></script>
		<script>
			Scalar.createApiReference("#app", {
				url: $specification_url,
				agent: {
					disabled: true,
				}
			});
		</script>
	</body>
</html>
""")


@dataclass
class RouteDocs:
	"""Documentation that the api_docs decorator attaches to a route function."""

	request_example: Any | None = None
	responses: dict[int, dict[str, Any]] = field(default_factory=dict)
	tags: list[str] = field(default_factory=list)
	parameters: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DocsConfig:
	"""Settings for the API reference page and the OpenAPI document."""

	title: str = "API Documentation"
	version: str = "1.0.0"
	show_authorization: bool = True
	default_responses: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_RESPONSES))


def api_docs(
	*,
	request_example: Any | None = None,
	responses: dict[int, dict[str, Any]] | None = None,
	tags: list[str] | None = None,
	parameters: list[dict[str, Any]] | None = None,
):
	"""Attach request and response examples to a route function."""

	def decorator(function: Callable) -> Callable:
		function.__api_docs__ = RouteDocs(
			request_example=request_example,
			responses=responses or {},
			tags=tags or [],
			parameters=parameters or [],
		)
		return function

	return decorator


def generate_specification(router: Router) -> dict[str, Any]:
	"""Build the OpenAPI document for a router and each nested router."""
	config = router.docs
	if config is None:
		raise ValueError(f"Router {router.prefix} has no DocsConfig")

	schemas: dict[str, Any] = {}
	paths: dict[str, dict[str, Any]] = {}
	operation_ids: set[str] = set()
	for route in router.get_routes():
		path, path_parameters = convert_path(route.path)
		for method in route.methods:
			operation = build_operation(route, path_parameters, schemas, config.default_responses)
			if route.public:
				operation["security"] = []
			operation["operationId"] = pick_operation_id(route.function.__name__, operation_ids)
			paths.setdefault(path, {})[method.lower()] = operation

	components: dict[str, Any] = {"schemas": schemas}
	if config.show_authorization:
		components["securitySchemes"] = SECURITY_SCHEMES

	return {
		"openapi": OPENAPI_VERSION,
		"info": {"title": config.title, "version": config.version},
		"tags": router.documentation_tags,
		"paths": paths,
		"components": components,
		"security": [{name: []} for name in SECURITY_SCHEMES] if config.show_authorization else [],
	}


def build_operation(
	route: RouteMeta,
	path_parameters: list[dict[str, Any]],
	schemas: dict[str, Any],
	default_responses: dict[int, str],
) -> dict[str, Any]:
	"""Build the OpenAPI operation object for one route."""
	docs = route.documentation
	operation: dict[str, Any] = {}
	summary, description = split_docstring(route.function)
	if summary:
		operation["summary"] = summary
	if description:
		operation["description"] = description

	tags = [tag for tag in [route.tag, *(docs.tags if docs else [])] if tag]
	if tags:
		operation["tags"] = list(dict.fromkeys(tags))

	parameters = (
		path_parameters
		+ list(route.parameters)
		+ (list(docs.parameters) if docs else [])
		+ build_query_parameters(route.query, schemas)
	)
	if parameters:
		operation["parameters"] = parameters

	if route.payload:
		content: dict[str, Any] = {"schema": build_body_schema(route.payload, schemas)}
		if docs and docs.request_example is not None:
			content["example"] = docs.request_example
		operation["requestBody"] = {"required": True, "content": {"application/json": content}}

	operation["responses"] = build_responses(route.function, docs, default_responses, schemas)
	return operation


def build_body_schema(binding: ParameterBinding, schemas: dict[str, Any]) -> dict[str, Any]:
	"""Return the request body schema and register the models that it uses."""
	if binding.model and not binding.is_list:
		return {"$ref": register_model(binding.model, schemas)}

	schema = binding.adapter.json_schema(ref_template=REFERENCE_TEMPLATE)
	lift_definitions(schema, schemas)
	return schema


def build_query_parameters(binding: ParameterBinding | None, schemas: dict[str, Any]) -> list[dict[str, Any]]:
	"""Return one query parameter for each field of a query model."""
	if binding is None or binding.model is None or binding.is_list:
		return []

	schema = binding.model.model_json_schema(ref_template=REFERENCE_TEMPLATE)
	lift_definitions(schema, schemas)
	required = set(schema.get("required", []))
	return [
		{"name": name, "in": "query", "required": name in required, "schema": field_schema}
		for name, field_schema in schema.get("properties", {}).items()
	]


def build_responses(
	function: Callable,
	docs: RouteDocs | None,
	default_responses: dict[int, str],
	schemas: dict[str, Any],
) -> dict[str, Any]:
	"""Merge the router default responses with the responses declared on a route."""
	responses = {status: {"description": description} for status, description in default_responses.items()}
	for status, declared in (docs.responses if docs else {}).items():
		response = responses.setdefault(status, {"description": ""})
		response["description"] = declared.get("description", response["description"])
		if "example" in declared:
			response["content"] = {"application/json": {"example": declared["example"]}}

	model = get_response_model(function)
	if model:
		status = next(
			(status for status in (docs.responses if docs else {}) if 200 <= status < 300),
			200,
		)
		responses.setdefault(status, {"description": "OK"}).setdefault(
			"content", {"application/json": {"schema": {"$ref": register_model(model, schemas)}}}
		)
	return {str(status): response for status, response in sorted(responses.items())}


def get_response_model(function: Callable) -> type[BaseModel] | None:
	"""Return the Pydantic response model in a route return annotation."""
	from atlas.api.core.base import ApiResult

	annotation = get_type_hints(function).get("return")
	if get_origin(annotation) is ApiResult:
		annotation = next(iter(get_args(annotation)), None)

	if isinstance(annotation, type) and issubclass(annotation, BaseModel):
		return annotation
	return None


def register_model(model: type[BaseModel], schemas: dict[str, Any]) -> str:
	"""Add a model schema to the components section and return its reference."""
	reference = REFERENCE_TEMPLATE.format(model=model.__name__)
	if model.__name__ in schemas:
		return reference

	schema = model.model_json_schema(ref_template=REFERENCE_TEMPLATE)
	lift_definitions(schema, schemas)
	if schema.get("$ref") != reference:
		schemas[model.__name__] = schema
	return reference


def lift_definitions(schema: dict[str, Any], schemas: dict[str, Any]) -> None:
	"""Move nested model definitions out of a schema into the components section."""
	for name, definition in schema.pop("$defs", {}).items():
		schemas.setdefault(name, definition)


def convert_path(path: str) -> tuple[str, list[dict[str, Any]]]:
	"""Convert a Werkzeug rule into OpenAPI path parameters."""
	parameters: list[dict[str, Any]] = []

	def replace(match: re.Match) -> str:
		converter = match.group(1) or "string"
		name = match.group(2)
		parameters.append(
			{
				"name": name,
				"in": "path",
				"required": True,
				"schema": {"type": WERKZEUG_CONVERTER_TYPES.get(converter, "string")},
			}
		)
		return f"{{{name}}}"

	return PATH_PARAMETER_PATTERN.sub(replace, path), parameters


def split_docstring(function: Callable) -> tuple[str | None, str | None]:
	"""Return the summary line and the remaining function documentation."""
	summary, _, description = (inspect.getdoc(function) or "").partition("\n")
	return summary.strip().removesuffix(".") or None, description.strip() or None


def pick_operation_id(name: str, used: set[str]) -> str:
	"""Return an operation ID that the specification does not use."""
	operation_id = name
	suffix = 2
	while operation_id in used:
		operation_id = f"{name}_{suffix}"
		suffix += 1
	used.add(operation_id)
	return operation_id


def render_api_reference(title: str, specification_url: str) -> str:
	"""Return the HTML page that shows an OpenAPI document with Scalar."""
	return API_REFERENCE_TEMPLATE.substitute(
		title=escape(title),
		script_url=SCALAR_SCRIPT_URL,
		specification_url=json.dumps(specification_url),
	)
