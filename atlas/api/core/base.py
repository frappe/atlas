import functools
import inspect
import re
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin, get_type_hints

import frappe
import orjson
from frappe.utils import orjson_dumps
from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict, Field, model_validator
from werkzeug.exceptions import HTTPException
from werkzeug.routing import Rule
from werkzeug.wrappers import Request, Response

from atlas.api.core.binding import CallShape, ParameterBinding, read_json_body, resolve_binding
from atlas.api.core.docs import DocsConfig, RouteDocs, generate_specification, render_api_reference
from atlas.api.core.errors import (
	InvalidRequest,
	ResourceNotFound,
	as_api_error,
	log_unexpected_error,
)
from atlas.atlas.core.tags import find_names_with_tags
from atlas.atlas.core.telemetry.events import count, observe
from atlas.auth.identity import (
	MAXIMUM_TENANT_ID,
	TENANT_HEADER,
	get_current_tenant_id,
	require_central_identity,
)
from atlas.auth.overrides import is_document_visible

DEFAULT_LIST_LIMIT = 20
MAXIMUM_LIST_LIMIT = 100
TENANT_PARAMETERS = (
	{
		"name": TENANT_HEADER,
		"in": "header",
		"required": True,
		"description": (
			"Tenant that owns the resource, from 0 through 4294967295. A regional token must"
			" send the tenant of its own claim, and a Central token (tenant=*) names the tenant"
			" it acts for."
		),
		"schema": {"type": "integer", "minimum": 0, "maximum": MAXIMUM_TENANT_ID},
	},
)


class StrictModel(PydanticBaseModel):
	"""A request model that rejects a field the route does not support."""

	model_config = ConfigDict(extra="forbid")


class PatchPayload(StrictModel):
	"""A partial update that must carry at least one supported field."""

	@model_validator(mode="after")
	def require_one_field(self) -> PatchPayload:
		"""Reject a request that changes nothing."""
		if not self.model_fields_set:
			raise ValueError("The request needs at least one supported field.")
		return self


class ListQuery(StrictModel):
	"""Query values that every list route accepts."""

	offset: int = Field(default=0, ge=0, description="Number of matching resources to skip.")
	limit: int = Field(
		default=DEFAULT_LIST_LIMIT,
		ge=1,
		le=MAXIMUM_LIST_LIMIT,
		description="Maximum number of resources to return.",
	)
	tag: str | None = Field(
		default=None,
		description=(
			"Comma separated key:value tags. A resource must carry every pair, such as"
			" tag=os:Ubuntu,channel:lts."
		),
	)

	@property
	def fetch_limit(self) -> int:
		"""Return the row count that shows whether another page exists."""
		return self.limit + 1

	@property
	def tags(self) -> dict[str, str]:
		"""Return the requested tags as a key to value map."""
		return parse_tag_filter(self.tag)


def parse_tag_filter(value: str | None) -> dict[str, str]:
	"""Read one comma separated key:value list, or report why it is not usable."""
	if not value:
		return {}

	tags: dict[str, str] = {}
	for pair in value.split(","):
		key, separator, tag_value = pair.partition(":")
		key, tag_value = key.strip(), tag_value.strip()
		if not separator or not key:
			raise InvalidRequest(f"Tag filter {pair.strip()!r} needs the form key:value.")
		if key in tags:
			raise InvalidRequest(f"Tag key {key!r} is repeated.")

		tags[key] = tag_value

	return tags


class Page[PageItem](PydanticBaseModel):
	"""One page of API resources."""

	model_config = ConfigDict(
		json_schema_extra={"examples": [{"items": [], "offset": 0, "limit": 20, "has_more": False}]}
	)

	items: list[PageItem] = Field(description="Resources in this page.")
	offset: int = Field(description="Number of matching resources skipped before this page.")
	limit: int = Field(description="Requested maximum page size.")
	has_more: bool = Field(description="Whether another page follows this page.")


def get_owned_document(doctype: str, name: str, label: str | None = None):
	"""Return one document that the tenant of the request may read, or report that it is absent."""
	absent = ResourceNotFound(f"The {label or doctype} does not exist.")
	if not name:
		raise absent

	get_current_tenant_id()

	try:
		document = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		raise absent from None

	if not is_document_visible(document, "read"):
		raise absent

	return document


def add_tag_filter(doctype: str, query: ListQuery, filters: dict[str, Any]) -> bool:
	"""Narrow filters to the documents that carry every tag in query.

	Returns False when no document can match, so the route answers an empty page
	instead of an unfiltered one.
	"""
	tags = query.tags
	if not tags:
		return True

	names = find_names_with_tags(doctype, tags)
	if not names:
		return False

	filters["name"] = ("in", names)
	return True


def build_page[PageItem](rows: list[PageItem], query: ListQuery) -> Page[PageItem]:
	"""Return one list envelope for rows that were read with fetch_limit."""
	return Page(
		items=rows[: query.limit],
		offset=query.offset,
		limit=query.limit,
		has_more=len(rows) > query.limit,
	)


@dataclass(frozen=True, slots=True)
class ApiResult[ResultBody]:
	"""One JSON body with its status code and its extra response headers."""

	body: ResultBody
	status: int = 200
	headers: dict[str, str] = field(default_factory=dict)


@dataclass
class RouteMeta:
	"""One registered route and the information that the specification needs."""

	path: str
	methods: list[str]
	function: Callable
	payload: ParameterBinding | None = None
	query: ParameterBinding | None = None
	tag: str | None = None
	public: bool = False
	parameters: tuple[dict[str, Any], ...] = field(default_factory=lambda: TENANT_PARAMETERS)

	@property
	def documentation(self) -> RouteDocs | None:
		"""Documentation attached by the api_docs decorator."""
		return getattr(self.function, "__api_docs__", None)


class Router:
	"""Registers route functions under one prefix of the Frappe API URL map."""

	def __init__(
		self,
		prefix: str,
		name: str | None = None,
		description: str | None = None,
		default_headers: dict[str, str] | None = None,
		docs: DocsConfig | None = None,
		tag_parent: str | None = None,
		tag_kind: str | None = None,
	):
		self.prefix = normalize_prefix(prefix)
		self.name = name or self.prefix
		self.description = description
		self.default_headers = default_headers or {}
		self.docs = docs
		self.tag_parent = tag_parent
		self.tag_kind = tag_kind
		self.routes: list[RouteMeta] = []
		self.children: list[Router] = []

		if self.docs:
			self.register_documentation_routes()

	def join(self, path: str) -> str:
		"""Return path joined to the prefix of this router."""
		path = path.strip("/")
		return f"{self.prefix}/{path}" if path else self.prefix

	def request(self, method: str, path: str = "", *, central_only: bool = False):
		"""Register a route for one HTTP method."""
		return register_route(self, self.join(path), [method.upper()], central_only=central_only)

	def head(self, path: str = ""):
		return self.request("HEAD", path)

	def get(self, path: str = "", *, public: bool = False, central_only: bool = False):
		return register_route(
			self,
			self.join(path),
			["GET"],
			public=public,
			central_only=central_only,
		)

	def post(self, path: str = "", *, central_only: bool = False):
		return self.request("POST", path, central_only=central_only)

	def put(self, path: str = "", *, central_only: bool = False):
		return self.request("PUT", path, central_only=central_only)

	def patch(self, path: str = "", *, central_only: bool = False):
		return self.request("PATCH", path, central_only=central_only)

	def delete(self, path: str = "", *, central_only: bool = False):
		return self.request("DELETE", path, central_only=central_only)

	def subrouter(
		self,
		subpath: str,
		name: str | None = None,
		description: str | None = None,
		default_headers: dict[str, str] | None = None,
		tag_parent: str | None = None,
		tag_kind: str | None = None,
	) -> "Router":
		"""Create a nested router that shares the error handling of this router."""
		child = Router(
			prefix=self.join(subpath),
			name=name,
			description=description,
			default_headers=default_headers or self.default_headers,
			tag_parent=tag_parent,
			tag_kind=tag_kind,
		)
		self.children.append(child)
		return child

	def get_routes(self) -> list[RouteMeta]:
		"""Return the routes of this router and of every nested router."""
		routes = list(self.routes)
		for child in self.children:
			routes.extend(child.get_routes())

		return routes

	@property
	def documentation_tags(self) -> list[dict[str, str]]:
		"""Tag entries for this router and every nested router."""
		tag = {"name": self.name}
		if self.description:
			tag["description"] = self.description
		if self.tag_parent:
			tag["parent"] = self.tag_parent
		if self.tag_kind:
			tag["kind"] = self.tag_kind

		tags = [tag]
		for child in self.children:
			tags.extend(child.documentation_tags)

		return tags

	@property
	def openapi_specification(self) -> dict[str, Any]:
		"""The OpenAPI document for this router."""
		if not self.docs:
			raise ValueError(f"Router {self.prefix} was created without a DocsConfig")

		return generate_specification(self)

	def register_documentation_routes(self) -> None:
		"""Add the API reference page and the OpenAPI document endpoints."""
		from frappe.api import API_URL_MAP

		specification_path = self.join("docs/openapi.json")
		title = self.docs.title

		def api_reference():
			return Response(render_api_reference(title, specification_path), mimetype="text/html")

		def openapi_specification():
			return self.openapi_specification

		API_URL_MAP.add(
			Rule(
				self.join("docs"),
				endpoint=RouteHandler(self, api_reference),
				methods=["GET"],
			)
		)
		API_URL_MAP.add(
			Rule(
				self.join("docs/openapi.json"),
				endpoint=RouteHandler(self, openapi_specification),
				methods=["GET"],
			)
		)


class RouteHandler:
	"""Runs one route function for an HTTP request and turns its result into a response."""

	def __init__(self, router: Router, function: Callable, central_only: bool = False):
		functools.update_wrapper(self, function)
		self.router = router
		self.function = function
		self.central_only = central_only
		self.shape = CallShape.of(function)
		self.payload = resolve_binding(function, "payload")
		self.query = resolve_binding(function, "query")

	def __call__(self, *args, **kwargs):
		request = getattr(frappe.local, "request", None)
		if request is None or not hasattr(request, "method"):
			return self.function(*args, **kwargs)

		started_at = time.monotonic()
		status = 200
		try:
			if self.central_only:
				require_central_identity()

			kwargs.update(self.read_parameters(request, args, kwargs))
			result = self.function(*args, **self.shape.accepted_keywords(kwargs))
			if frappe.flags.in_test:
				return result

			response = build_response(result, self.router.default_headers)
			status = response.status_code
			return response
		except HTTPException as exception:
			status = (
				exception.response.status_code if exception.response is not None else exception.code or 500
			)
			raise
		except Exception as exception:
			error_response = self.build_error_response(exception)
			status = error_response.status_code
			raise HTTPException(response=error_response)
		finally:
			self.record_request(request.method, status, time.monotonic() - started_at)

	def record_request(self, method: str, status: int, duration: float) -> None:
		"""Count one API request by operation. A change request is also observed."""
		labels = {"operation": self.function.__name__, "method": method, "status": str(status)}
		count("atlas_api_requests_total", **labels)
		if method not in ("GET", "HEAD"):
			observe("atlas_api_request_duration_seconds", duration, **labels)

	def read_parameters(self, request: Request, args: tuple, kwargs: dict) -> dict[str, Any]:
		"""Decode the query and payload parameters that the caller did not supply."""
		values: dict[str, Any] = {}

		if self.query and not self.shape.is_supplied("query", args, kwargs):
			values["query"] = self.query.convert(dict(request.args))

		if self.payload and not self.shape.is_supplied("payload", args, kwargs):
			values["payload"] = self.payload.convert(read_json_body(request, self.payload))

		return values

	def build_error_response(self, exception: Exception) -> Response:
		"""Convert an exception into a structured JSON error response."""
		error = as_api_error(exception)
		log_unexpected_error(error, exception)
		response = jsonify(error.as_dict(), status_code=error.http_status_code)
		for name, value in {**self.router.default_headers, **error.headers}.items():
			response.headers[name] = value

		return response


def normalize_prefix(prefix: str) -> str:
	"""Return prefix as an absolute path below /api that the Frappe REST API does not own."""
	path = re.sub(r"^api(/|$)", "", prefix.strip("/")).strip("/")
	if not path:
		raise ValueError("Router prefix cannot be empty")

	normalized = f"/api/{path}"
	if re.match(r"^/api/v[12](/|$)", normalized):
		raise ValueError(f"Router prefix {normalized} collides with the Frappe REST API")

	return normalized


def register_route(
	router: Router,
	path: str,
	methods: list[str],
	*,
	public: bool = False,
	central_only: bool = False,
):
	"""Return a decorator that adds one route to the Frappe API URL map."""
	from frappe.api import API_URL_MAP

	if not methods:
		raise ValueError(f"Route {path} needs at least one HTTP method")

	unsupported = sorted(set(methods) - {"HEAD", "GET", "POST", "PUT", "PATCH", "DELETE"})
	if unsupported:
		raise ValueError(f"Route {path} uses unsupported HTTP methods: {unsupported}")
	if public and central_only:
		raise ValueError(f"Route {path} cannot be both public and Central-only")

	def decorator(function: Callable) -> RouteHandler:
		handler = RouteHandler(router, function, central_only=central_only)
		if handler.payload and set(methods) <= {"HEAD", "GET"}:
			raise TypeError(f"{function.__name__} cannot accept a payload on {methods}")

		API_URL_MAP.add(Rule(path, endpoint=handler, methods=methods))
		router.routes.append(
			RouteMeta(
				path=path,
				methods=methods,
				function=function,
				payload=handler.payload,
				query=handler.query,
				tag=router.name,
				public=public,
				parameters=() if public or central_only else TENANT_PARAMETERS,
			)
		)
		return handler

	return decorator


def jsonify(data: Any, status_code: int = 200) -> Response:
	"""Return data as a JSON response."""
	if isinstance(data, PydanticBaseModel):
		data = data.model_dump(mode="json")
	return Response(orjson_dumps(data), status=status_code, mimetype="application/json")


def build_response(result: Any, headers: dict[str, str]) -> Response:
	"""Convert the result of a route function into a response."""
	status = None
	if isinstance(result, tuple) and len(result) == 2:
		result, status = result

	if isinstance(result, ApiResult):
		response = jsonify(result.body, status_code=result.status)
		for name, value in result.headers.items():
			response.headers[name] = value
	elif isinstance(result, Response):
		response = result
		if status:
			response.status_code = status
	elif result is None:
		response = Response(status=status or 204)
	elif isinstance(result, PydanticBaseModel):
		response = jsonify(result.model_dump(mode="json"), status_code=status or 200)
	elif isinstance(result, dict | list):
		response = jsonify(result, status_code=status or 200)
	else:
		response = Response(str(result), status=status or 200, mimetype="text/plain")

	for name, value in headers.items():
		response.headers[name] = value

	return response
