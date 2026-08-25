"""The core callback registry and its one-way seam (spec P3): core fires an
event by name, services handlers registered at boot do the PaaS work, and core
never imports services."""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.core import callbacks


class TestCallbackRegistry(IntegrationTestCase):
	def setUp(self) -> None:
		# Load the real services handlers FIRST, then snapshot — so restoring in
		# tearDown keeps the boot registrations intact for later tests (these tests
		# use unique `test.*` event names to avoid colliding with the real ones).
		callbacks._ensure_loaded()
		self._saved = {k: list(v) for k, v in callbacks._REGISTRY.items()}

	def tearDown(self) -> None:
		callbacks._REGISTRY.clear()
		callbacks._REGISTRY.update(self._saved)

	def test_run_invokes_every_handler_in_order_and_returns_results(self) -> None:
		seen = []
		callbacks.register("test.evt", lambda x: seen.append(("a", x)) or "a")
		callbacks.register("test.evt", lambda x: seen.append(("b", x)) or "b")
		results = callbacks.run("test.evt", 7)
		self.assertEqual(results, ["a", "b"])
		self.assertEqual(seen, [("a", 7), ("b", 7)])

	def test_register_is_idempotent_per_handler(self) -> None:
		def handler():
			return 1

		callbacks.register("test.idem", handler)
		callbacks.register("test.idem", handler)
		self.assertEqual(callbacks.run("test.idem"), [1])

	def test_run_propagates_handler_exceptions(self) -> None:
		def boom():
			raise ValueError("nope")

		callbacks.register("test.boom", boom)
		with self.assertRaisesRegex(ValueError, "nope"):
			callbacks.run("test.boom")

	def test_run_first_returns_first_non_none(self) -> None:
		callbacks.register("test.first", lambda: None)
		callbacks.register("test.first", lambda: "answer")
		callbacks.register("test.first", lambda: "later")
		self.assertEqual(callbacks.run_first("test.first"), "answer")
		self.assertIsNone(callbacks.run_first("test.unregistered"))

	def test_registered_reflects_state(self) -> None:
		self.assertFalse(callbacks.registered("test.none"))
		callbacks.register("test.some", lambda: None)
		self.assertTrue(callbacks.registered("test.some"))

	def test_services_callbacks_hook_wires_vm_address_changed(self) -> None:
		# The boot hook must actually register the services handler — this is the
		# proof the one-way seam is connected, not just declared.
		self.assertIn("atlas.atlas.services.callbacks_register", frappe.get_hooks("services_callbacks"))
		for event in (
			"vm.address_changed",
			"vm.terminated",
			"vm.deploy_gateway",
			"vm.read_proxy_maps",
			"vm.status_suppressed",
			"vm.payload_augment",
		):
			self.assertTrue(callbacks.registered(event), f"{event} not registered")
