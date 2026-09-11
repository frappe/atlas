import unittest
from itertools import count
from unittest.mock import patch

import frappe
import orjson
from pydantic import BaseModel
from werkzeug.wrappers import Response

from atlas.api.core.base import (
	ApiResult,
	Router,
	build_response,
	normalize_prefix,
	resolve_binding,
)
from atlas.api.core.docs import DocsConfig, api_docs, render_api_reference
from atlas.api.tests.test_support import api_request as http_request
from atlas.api.tests.test_support import call_route, error_body

PREFIX_COUNTER = count()


class Machine(BaseModel):
	name: str
	cores: int = 1


class MachineFilter(BaseModel):
	region: str
	limit: int = 20


def make_router(**options) -> Router:
	"""Return a router on a prefix that no other test uses."""
	return Router(prefix=f"test-api-{next(PREFIX_COUNTER)}", **options)


class TestPrefix(unittest.TestCase):
	def test_prefix_is_placed_below_api(self):
		self.assertEqual(normalize_prefix("atlas"), "/api/atlas")
		self.assertEqual(normalize_prefix("/atlas/"), "/api/atlas")

	def test_prefix_normalization_is_repeatable(self):
		self.assertEqual(normalize_prefix(normalize_prefix("atlas/vm")), "/api/atlas/vm")

	def test_frappe_rest_prefixes_are_rejected(self):
		for prefix in ("v1", "v2", "v1/machines"):
			with self.assertRaises(ValueError):
				normalize_prefix(prefix)

	def test_empty_prefix_is_rejected(self):
		with self.assertRaises(ValueError):
			normalize_prefix("/")

	def test_prefix_that_only_starts_with_api_is_kept(self):
		self.assertEqual(normalize_prefix("apiary"), "/api/apiary")


class TestBinding(unittest.TestCase):
	def test_model_and_list_annotations_are_accepted(self):
		def route(payload: Machine, query: MachineFilter): ...

		payload = resolve_binding(route, "payload")
		self.assertIs(payload.model, Machine)
		self.assertFalse(payload.is_list)
		self.assertIs(resolve_binding(route, "query").model, MachineFilter)

		def bulk(payload: list[Machine]): ...

		binding = resolve_binding(bulk, "payload")
		self.assertIs(binding.model, Machine)
		self.assertTrue(binding.is_list)
		self.assertEqual(binding.empty_value, [])

	def test_missing_parameter_has_no_binding(self):
		def route(): ...

		self.assertIsNone(resolve_binding(route, "payload"))

	def test_unsupported_annotations_are_rejected(self):
		def unannotated(payload): ...

		def union(payload: Machine | None): ...

		def scalar(payload: int): ...

		def mapping(payload: dict): ...

		def raw_list(payload: list): ...

		def scalar_list(payload: list[int]): ...

		for route in (unannotated, union, scalar, mapping, raw_list, scalar_list):
			with self.assertRaises(TypeError):
				resolve_binding(route, "payload")


class TestRouteRegistration(unittest.TestCase):
	def test_routes_are_listed_once_for_every_call(self):
		router = make_router()
		child = router.subrouter("machines", name="Machines")

		@router.get("health")
		def health(): ...

		@child.get("")
		def list_machines(): ...

		self.assertEqual(len(router.get_routes()), 2)
		self.assertEqual(len(router.get_routes()), 2)
		self.assertEqual(child.prefix, f"{router.prefix}/machines")

	def test_payload_on_a_bodyless_method_is_rejected(self):
		router = make_router()

		with self.assertRaises(TypeError):

			@router.get("machines")
			def broken(payload: Machine): ...

	def test_unsupported_method_is_rejected(self):
		router = make_router()

		with self.assertRaises(ValueError):
			router.request("TRACE", "machines")

	def test_a_public_get_route_needs_no_session_or_tenant_header(self):
		router = make_router(docs=DocsConfig())

		@router.get("keys", public=True)
		def keys():
			return {"keys": []}

		previous_user = frappe.session.user
		frappe.session.user = "Guest"
		try:
			with http_request("GET"):
				status, body = call_route(keys)
		finally:
			frappe.session.user = previous_user

		operation = router.openapi_specification["paths"][f"{router.prefix}/keys"]["get"]
		self.assertEqual((status, body), (200, {"keys": []}))
		self.assertEqual(operation["security"], [])
		self.assertNotIn("parameters", operation)


class TestRequestDecoding(unittest.TestCase):
	def test_payload_is_validated_into_the_model(self):
		router = make_router()

		@router.post("machines")
		def create(payload: Machine):
			return {"name": payload.name, "cores": payload.cores}

		with http_request("POST", json={"name": "vm-1", "cores": 4}):
			response = create()

		self.assertEqual(orjson.loads(response.get_data()), {"name": "vm-1", "cores": 4})

	def test_query_values_are_coerced(self):
		router = make_router()

		@router.get("machines")
		def search(query: MachineFilter):
			return {"region": query.region, "limit": query.limit}

		with http_request("GET", query_string={"region": "in-mumbai", "limit": "5"}):
			response = search()

		self.assertEqual(orjson.loads(response.get_data()), {"region": "in-mumbai", "limit": 5})

	def test_invalid_payload_reports_the_failing_fields(self):
		router = make_router()

		@router.post("machines")
		def create(payload: Machine):
			return payload

		with http_request("POST", json={"cores": "many"}):
			status, body = error_body(create)

		self.assertEqual(status, 400)
		self.assertEqual(body["error"]["code"], "invalid_request")
		self.assertEqual({field["name"] for field in body["error"]["fields"]}, {"name", "cores"})

	def test_body_that_is_not_json_is_rejected(self):
		router = make_router()

		@router.post("machines")
		def create(payload: Machine):
			return payload

		with http_request("POST", data="not json"):
			status, body = error_body(create)

		self.assertEqual(status, 400)
		self.assertEqual(body["error"]["code"], "invalid_request")
		self.assertEqual(body["error"]["message"], "The request body is not valid JSON.")

	def test_explicit_arguments_are_not_overwritten(self):
		router = make_router()

		@router.post("machines")
		def create(payload: Machine):
			return {"name": payload.name}

		with http_request("POST", json={"name": "from-request"}):
			response = create(payload=Machine(name="from-caller"))

		self.assertEqual(orjson.loads(response.get_data())["name"], "from-caller")

	def test_path_arguments_the_route_ignores_are_dropped(self):
		router = make_router()

		@router.get("machines/<name>")
		def show():
			return {"ok": True}

		with http_request("GET"):
			response = show(name="vm-1")

		self.assertEqual(response.status_code, 200)


class TestResponseBuilding(unittest.TestCase):
	def test_default_headers_reach_json_responses(self):
		response = build_response({"ok": True}, {"X-Region": "in-mumbai"})
		self.assertEqual(response.headers["X-Region"], "in-mumbai")
		self.assertEqual(response.mimetype, "application/json")

	def test_status_code_can_be_returned_with_the_body(self):
		self.assertEqual(build_response(({"ok": True}, 201), {}).status_code, 201)

	def test_model_results_are_serialized(self):
		response = build_response(Machine(name="vm-1"), {})
		self.assertEqual(orjson.loads(response.get_data()), {"name": "vm-1", "cores": 1})

	def test_none_becomes_an_empty_response(self):
		self.assertEqual(build_response(None, {}).status_code, 204)

	def test_a_returned_response_is_kept(self):
		original = Response("ok", mimetype="text/plain")
		self.assertIs(build_response(original, {}), original)

	def test_api_result_carries_its_status_and_headers(self):
		response = build_response(
			ApiResult({"id": "vm-1"}, status=201, headers={"Location": "/api/atlas/vm-1"}), {}
		)

		self.assertEqual(response.status_code, 201)
		self.assertEqual(response.headers["Location"], "/api/atlas/vm-1")
		self.assertEqual(orjson.loads(response.get_data()), {"id": "vm-1"})


class TestSpecification(unittest.TestCase):
	def build_specification(self) -> dict:
		router = make_router(
			name="Machines",
			description="Machine operations",
			docs=DocsConfig(title="Atlas API", version="2.0.0"),
		)

		@router.post("machines")
		@api_docs(
			request_example={"name": "vm-1"},
			responses={201: {"description": "Created", "example": {"name": "vm-1"}}},
		)
		def create_machine(payload: Machine):
			"""
			Create a machine.

			The machine starts after the host accepts the request.
			"""
			return payload

		@router.get("machines/<name>")
		def show_machine(query: MachineFilter): ...

		return router.openapi_specification

	def setUp(self):
		self.specification = self.build_specification()
		self.paths = self.specification["paths"]

	def test_document_metadata(self):
		self.assertEqual(self.specification["openapi"], "3.1.0")
		self.assertEqual(self.specification["info"], {"title": "Atlas API", "version": "2.0.0"})
		self.assertIn({"name": "Machines", "description": "Machine operations"}, self.specification["tags"])

	def test_nested_tags_include_their_parent(self):
		parent = make_router(name="Virtual Machines", tag_kind="nav")
		parent.subrouter("actions", name="VM Actions", tag_parent="Virtual Machines")

		self.assertEqual(
			parent.documentation_tags,
			[
				{"name": "Virtual Machines", "kind": "nav"},
				{"name": "VM Actions", "parent": "Virtual Machines"},
			],
		)

	def test_only_the_reference_and_the_document_are_served(self):
		documentation_paths = [path for path in self.paths if "docs" in path]
		self.assertEqual(documentation_paths, [])

		router_paths = {path.rsplit("/", 1)[-1] for path in self.paths}
		self.assertNotIn("swagger", router_paths)
		self.assertNotIn("redoc", router_paths)

	def test_docstring_becomes_the_summary_and_description(self):
		operation = self.operation("machines", "post")
		self.assertEqual(operation["summary"], "Create a machine")
		self.assertEqual(operation["description"], "The machine starts after the host accepts the request.")

	def test_payload_model_is_referenced_from_components(self):
		operation = self.operation("machines", "post")
		content = operation["requestBody"]["content"]["application/json"]
		self.assertEqual(content["schema"], {"$ref": "#/components/schemas/Machine"})
		self.assertEqual(content["example"], {"name": "vm-1"})
		self.assertIn("Machine", self.specification["components"]["schemas"])

	def test_only_declared_responses_are_included(self):
		responses = self.operation("machines", "post")["responses"]
		self.assertEqual(responses["201"]["description"], "Created")
		self.assertNotIn("404", responses)

	def test_path_and_query_parameters_are_described(self):
		operation = self.operation("machines/{name}", "get")
		parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
		self.assertTrue(parameters["X-Tenant-ID"]["required"])
		self.assertEqual(parameters["name"]["in"], "path")
		self.assertTrue(parameters["name"]["required"])
		self.assertTrue(parameters["region"]["required"])
		self.assertFalse(parameters["limit"]["required"])
		self.assertEqual(parameters["limit"]["schema"]["type"], "integer")

	def test_operation_ids_are_unique(self):
		operation_ids = [
			operation["operationId"] for methods in self.paths.values() for operation in methods.values()
		]
		self.assertEqual(len(operation_ids), len(set(operation_ids)))

	def operation(self, suffix: str, method: str) -> dict:
		"""Return the operation registered for a path that ends with suffix."""
		for path, methods in self.paths.items():
			if path.endswith(suffix) and method in methods:
				return methods[method]

		raise AssertionError(f"no {method} operation for a path ending with {suffix}")


class TestApiReference(unittest.TestCase):
	def test_reference_page_points_at_the_document(self):
		page = render_api_reference("Atlas & Co", "/api/atlas/docs/openapi.json")
		self.assertIn("<title>Atlas &amp; Co</title>", page)
		self.assertIn('url: "/api/atlas/docs/openapi.json"', page)
		self.assertIn("Scalar.createApiReference", page)

	def test_router_registers_docs_outside_resource_metadata(self):
		from frappe.api import API_URL_MAP

		router = make_router(docs=DocsConfig())
		paths = {rule.rule for rule in API_URL_MAP.iter_rules()}

		self.assertEqual(router.get_routes(), [])
		self.assertIn(f"{router.prefix}/docs", paths)
		self.assertIn(f"{router.prefix}/docs/openapi.json", paths)

	def test_documentation_routes_serve_a_guest(self):
		from frappe.api import API_URL_MAP

		router = make_router(docs=DocsConfig())
		paths = [f"{router.prefix}/docs", f"{router.prefix}/docs/openapi.json"]
		handlers = {rule.rule: rule.endpoint for rule in API_URL_MAP.iter_rules() if rule.rule in paths}

		for path in paths:
			with self.subTest(path=path), http_request(method="GET", path=path):
				with patch.object(frappe.local, "session", frappe._dict(user="Guest")):
					response = handlers[path]()

				self.assertEqual(response.status_code, 200)
