"""Unit tests for the provider registry.

These tests stub out `frappe.get_doc` so they exercise the registry shape
without requiring the `Provider` DocType to exist yet.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import providers
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	Provider,
	ProvisionRequest,
	ProvisionResult,
)


class _StubProvider(Provider):
	provider_type = "Stub"

	def authenticate(self) -> AuthResult:
		return AuthResult(ok=True, account_label="stub")

	def discover(self) -> Capabilities:
		return Capabilities(sizes=(), images=())

	def provision(self, request: ProvisionRequest) -> ProvisionResult:
		return ProvisionResult(
			provider_resource_id="stub-id",
			size=request.size,
			image=request.image,
			ready=False,
		)

	def describe(self, provider_resource_id: str) -> ProvisionResult:
		return ProvisionResult(
			provider_resource_id=provider_resource_id,
			size="",
			image="",
			ready=True,
		)

	def destroy(self, provider_resource_id: str) -> None:
		return None


class TestProviderRegistry(IntegrationTestCase):
	def setUp(self) -> None:
		# Register the stub for the duration of each test.
		providers._REGISTRY["Stub"] = _StubProvider

	def tearDown(self) -> None:
		providers._REGISTRY.pop("Stub", None)

	def test_register_decorator_stores_class(self) -> None:
		@providers.register
		class _DecoratorStub(Provider):
			provider_type = "DecoratorStub"

			def authenticate(self) -> AuthResult:
				return AuthResult(ok=True)

			def discover(self) -> Capabilities:
				return Capabilities(sizes=(), images=())

			def provision(self, request):
				return ProvisionResult(provider_resource_id="", size="", image="", ready=True)

			def describe(self, provider_resource_id):
				return ProvisionResult(provider_resource_id=provider_resource_id, size="", image="", ready=True)

			def destroy(self, provider_resource_id):
				return None

		try:
			self.assertIs(providers._REGISTRY["DecoratorStub"], _DecoratorStub)
		finally:
			providers._REGISTRY.pop("DecoratorStub", None)

	def test_for_provider_instantiates_active_class(self) -> None:
		row = SimpleNamespace(is_active=1, provider_type="Stub", name="stub-provider")
		with patch.object(providers, "_load_implementations", lambda: None), \
				patch.object(frappe, "get_doc", return_value=row):
			instance = providers.for_provider("stub-provider")
		self.assertIsInstance(instance, _StubProvider)

	def test_for_provider_throws_on_archived(self) -> None:
		row = SimpleNamespace(is_active=0, provider_type="Stub", name="stub-provider")
		with patch.object(providers, "_load_implementations", lambda: None), \
				patch.object(frappe, "get_doc", return_value=row):
			with self.assertRaises(frappe.ValidationError) as raised:
				providers.for_provider("stub-provider")
		self.assertIn("archived", str(raised.exception))

	def test_for_provider_throws_on_unknown_type(self) -> None:
		row = SimpleNamespace(is_active=1, provider_type="Unregistered", name="x")
		with patch.object(providers, "_load_implementations", lambda: None), \
				patch.object(frappe, "get_doc", return_value=row):
			with self.assertRaises(frappe.ValidationError) as raised:
				providers.for_provider("x")
		self.assertIn("No implementation", str(raised.exception))
