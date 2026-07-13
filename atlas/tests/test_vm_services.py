"""The VM service seam (spec/28 §3A) — the boundary contract, from Atlas's side.

Two invariants are proven here without any service app installed:

  1. An EMPTY registry is a pure no-op: a bare Atlas still boots, provisions, and
     terminates a VM exactly as before the seam existed.
  2. A registered service is called at every lifecycle hook point, in order, with
     the right effect: its `validate` can reject an insert, its `provision_variables`
     are merged into the provision Task, its `on_provision` / `on_status_change` /
     `teardown` fire at provision / status-change / terminate.

The service here is a hand-rolled spy (not satellite) — the point is the CORE wiring.
The satellite side proves the same contract from the service's end.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import vm_services
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task


class _SpyService:
	"""Records every seam call, in order. `extra_vars` are merged by
	provision_variables; `validate_error` makes validate reject."""

	def __init__(self, name: str = "spy", extra_vars: dict | None = None, validate_error: str | None = None):
		self.name = name
		self.calls: list[tuple] = []
		self.extra_vars = extra_vars or {}
		self.validate_error = validate_error

	def applies_to(self, vm) -> bool:
		return True

	def validate(self, vm) -> None:
		self.calls.append(("validate", vm.name))
		if self.validate_error:
			frappe.throw(self.validate_error)

	def provision_variables(self, vm) -> dict:
		self.calls.append(("provision_variables", vm.name))
		return dict(self.extra_vars)

	def on_provision(self, vm) -> None:
		self.calls.append(("on_provision", vm.name))

	def on_status_change(self, vm, old: str, new: str) -> None:
		self.calls.append(("on_status_change", old, new))

	def teardown(self, vm) -> None:
		self.calls.append(("teardown", vm.name))

	def kinds(self) -> list[str]:
		return [call[0] for call in self.calls]


class TestVMServiceSeam(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		# Free the /124 IPv6 range between tests.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_empty_registry_is_a_noop(self) -> None:
		"""Bare Atlas: no services registered, and the whole VM lifecycle still works."""
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		with vm_services.use_services([]):
			vm = _new_vm()
			with patch.object(module, "run_task", return_value=fake_task(name="t-prov")):
				vm.provision()
			vm.reload()
			self.assertEqual(vm.status, "Running")
			with patch.object(module, "run_task", return_value=fake_task(name="t-term")):
				vm.terminate()
			vm.reload()
			self.assertEqual(vm.status, "Terminated")

	def test_service_hooks_fire_across_the_lifecycle(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		spy = _SpyService(extra_vars={"SPY_VAR": "1"})
		with vm_services.use_services([spy]):
			# insert → validate
			vm = _new_vm()
			self.assertIn(("validate", vm.name), spy.calls)

			# provision → provision_variables merged into the Task + on_provision
			with patch.object(module, "run_task", return_value=fake_task(name="t-prov")) as mocked:
				vm.provision()
			self.assertEqual(mocked.call_args.kwargs["variables"].get("SPY_VAR"), "1")
			self.assertIn(("on_provision", vm.name), spy.calls)

			# provision set Running → on_status_change(Pending, Running)
			self.assertIn(("on_status_change", "Pending", "Running"), spy.calls)

			# terminate → teardown
			with patch.object(module, "run_task", return_value=fake_task(name="t-term")):
				vm.terminate()
			self.assertIn(("teardown", vm.name), spy.calls)

		# on_provision must run AFTER provision_variables (the env is built first).
		self.assertLess(
			spy.kinds().index("provision_variables"), spy.kinds().index("on_provision")
		)

	def test_service_validate_can_reject_an_insert(self) -> None:
		spy = _SpyService(validate_error="satellite says no")
		with vm_services.use_services([spy]):
			with self.assertRaises(frappe.ValidationError):
				_new_vm()

	def test_applies_to_gates_every_hook(self) -> None:
		"""A service whose applies_to is False is never called past the gate."""

		class _Inapplicable(_SpyService):
			def applies_to(self, vm) -> bool:
				return False

		spy = _Inapplicable()
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		with vm_services.use_services([spy]):
			vm = _new_vm()
			with patch.object(module, "run_task", return_value=fake_task(name="t-prov")):
				vm.provision()
		self.assertEqual(spy.calls, [])
