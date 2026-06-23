import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.reserved_ip import reserved_ip as module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.providers.base import ReservedIp
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_provider, make_server


def _make_reserved_ip(server: str, ip_address: str, **overrides) -> "frappe.model.document.Document":
	doc = {
		"doctype": "Reserved IP",
		"ip_address": ip_address,
		"server": server,
		"provider_resource_id": "do-reserved-1",
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


@contextlib.contextmanager
def _mock_host_side():
	"""Patch the two host/vendor side effects attach()/detach() now perform — the
	provider (assign/unassign on the droplet) and the host NAT Task — so the
	invariant-focused tests don't reach a real droplet or SSH. Yields
	(provider, run_task) MagicMocks so a test can assert the call shapes (assert
	INSIDE the `with`: the patch is undone on exit)."""
	provider = MagicMock()
	run_task = MagicMock(return_value=fake_task(name="task-rip"))
	with (
		patch.object(module, "for_provider_type", return_value=provider),
		patch.object(module, "run_task", run_task),
	):
		yield provider, run_task


def _purge_reserved_ips_and_vms() -> None:
	"""Drop all Reserved IP and VM rows. `on_trash` refuses to delete an
	attached IP, so null the link first (a previous test may have left one
	attached)."""
	for name in frappe.get_all("Reserved IP", pluck="name"):
		frappe.db.set_value("Reserved IP", name, "virtual_machine", None, update_modified=False)
		frappe.delete_doc("Reserved IP", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestReservedIP(IntegrationTestCase):
	def setUp(self) -> None:
		server = _ensure_test_server()
		# A real Active DO server always carries its droplet id; attach() needs it
		# to bind the reserved IP to the droplet at the vendor.
		frappe.db.set_value("Server", server, "provider_resource_id", "droplet-test")
		_ensure_test_image()
		_purge_reserved_ips_and_vms()

	def test_new_ip_is_allocated_and_unattached(self) -> None:
		rip = _make_reserved_ip(_ensure_test_server(), "203.0.113.5")
		self.assertEqual(rip.status, "Allocated")
		self.assertFalse(rip.virtual_machine)

	def test_attach_binds_vm_and_denormalizes_address(self) -> None:
		server = _ensure_test_server()
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.6")
		with _mock_host_side():
			rip.attach(vm.name)
		rip.reload()
		self.assertEqual(rip.status, "Attached")
		self.assertEqual(rip.virtual_machine, vm.name)
		vm.reload()
		self.assertEqual(vm.public_ipv4, "203.0.113.6")

	def test_attach_binds_vendor_and_runs_host_nat_task(self) -> None:
		"""attach() binds the reserved IP to the Server's droplet at the vendor and
		dispatches the host 1:1-NAT Task with attach action."""
		server = _ensure_test_server()
		frappe.db.set_value("Server", server, "provider_resource_id", "droplet-attach")
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.20")
		with _mock_host_side() as (provider, run_task):
			rip.attach(vm.name)
			provider.assign_reserved_ip.assert_called_once_with("do-reserved-1", "droplet-attach")
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["script"], "vm-reserved-ip.py")
		self.assertEqual(kwargs["variables"]["ACTION"], "attach")
		self.assertEqual(kwargs["variables"]["RESERVED_IPV4"], "203.0.113.20")
		self.assertEqual(kwargs["variables"]["VIRTUAL_MACHINE_NAME"], vm.name)

	def test_attach_throws_when_server_has_no_droplet_id(self) -> None:
		server = _ensure_test_server()
		frappe.db.set_value("Server", server, "provider_resource_id", None)
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.21")
		with _mock_host_side():
			with self.assertRaises(frappe.ValidationError) as raised:
				rip.attach(vm.name)
		self.assertIn("provider_resource_id", str(raised.exception))

	def test_attach_rejects_vm_on_different_server(self) -> None:
		other_server = make_server(
			make_provider("rip-other-provider"),
			"rip-other-server",
			ipv4_address="10.0.0.98",
			ipv6_address="2001:db8:2::1",
			ipv6_prefix="2001:db8:2::/64",
			ipv6_virtual_machine_range="2001:db8:2::/124",
			status="Active",
		)
		vm = _new_vm()  # on the default test server
		rip = _make_reserved_ip(other_server.name, "203.0.113.7")
		with _mock_host_side():
			with self.assertRaises(frappe.ValidationError) as raised:
				rip.attach(vm.name)
		self.assertIn("different Server", str(raised.exception))

	def test_attach_rejects_when_already_attached(self) -> None:
		server = _ensure_test_server()
		first = _new_vm()
		second = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.8")
		with _mock_host_side():
			rip.attach(first.name)
			with self.assertRaises(frappe.ValidationError) as raised:
				rip.attach(second.name)
		self.assertIn("already attached", str(raised.exception))

	def test_attach_rejects_vm_with_existing_ipv4(self) -> None:
		server = _ensure_test_server()
		vm = _new_vm()
		with _mock_host_side():
			_make_reserved_ip(server, "203.0.113.9").attach(vm.name)
			second_ip = _make_reserved_ip(server, "203.0.113.10")
			with self.assertRaises(frappe.ValidationError) as raised:
				second_ip.attach(vm.name)
		self.assertIn("already has a public IPv4", str(raised.exception))

	def test_detach_clears_vm_and_address(self) -> None:
		server = _ensure_test_server()
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.11")
		with _mock_host_side():
			rip.attach(vm.name)
			rip.detach()
		rip.reload()
		self.assertEqual(rip.status, "Allocated")
		self.assertFalse(rip.virtual_machine)
		vm.reload()
		self.assertFalse(vm.public_ipv4)

	def test_detach_tears_down_host_nat_and_unbinds_vendor(self) -> None:
		server = _ensure_test_server()
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.22")
		with _mock_host_side() as (provider, run_task):
			rip.attach(vm.name)
			provider.reset_mock()
			run_task.reset_mock()
			rip.detach()
			provider.unassign_reserved_ip.assert_called_once_with("do-reserved-1")
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["script"], "vm-reserved-ip.py")
		self.assertEqual(kwargs["variables"]["ACTION"], "detach")

	def test_detach_rejects_unattached(self) -> None:
		rip = _make_reserved_ip(_ensure_test_server(), "203.0.113.12")
		with self.assertRaises(frappe.ValidationError) as raised:
			rip.detach()
		self.assertIn("not attached", str(raised.exception))

	def test_terminate_detaches_reserved_ip(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		server = _ensure_test_server()
		vm = _new_vm()
		vm.status = "Running"
		vm.last_started = frappe.utils.now_datetime()
		vm.save(ignore_permissions=True)
		rip = _make_reserved_ip(server, "203.0.113.13")
		with _mock_host_side():
			rip.attach(vm.name)
		# attach() denormalized public_ipv4 onto the VM row via db_set, so our
		# handle's timestamp is stale; reload before terminate() saves it.
		vm.reload()
		# terminate-vm.py (vm_module.run_task) AND the reserved-IP detach Task +
		# vendor unbind (the reserved_ip module's run_task / for_provider_type) all fire;
		# patch both modules' side effects.
		with (
			patch.object(vm_module, "run_task", return_value=fake_task(name="task-term")),
			_mock_host_side(),
		):
			vm.terminate()
		rip.reload()
		self.assertEqual(rip.status, "Allocated")
		self.assertFalse(rip.virtual_machine)

	def test_ip_address_is_immutable(self) -> None:
		rip = _make_reserved_ip(_ensure_test_server(), "203.0.113.14")
		rip.ip_address = "203.0.113.99"
		with self.assertRaises(frappe.ValidationError) as raised:
			rip.save(ignore_permissions=True)
		self.assertIn("ip_address is immutable", str(raised.exception))


class TestReservedIPAllocateDiscover(IntegrationTestCase):
	def setUp(self) -> None:
		server = _ensure_test_server()
		# The refuse-while-attached tests attach() first, which binds at the vendor
		# (needs the droplet id). Tests that probe the missing-id path make their own
		# server / set the value explicitly, so this default is safe for them.
		frappe.db.set_value("Server", server, "provider_resource_id", "droplet-test")
		_purge_reserved_ips_and_vms()

	def test_allocate_creates_row_from_vendor(self) -> None:
		server = _ensure_test_server()
		provider = MagicMock()
		provider.allocate_reserved_ip.return_value = ReservedIp(
			ip_address="203.0.113.50", provider_resource_id="203.0.113.50"
		)
		with patch.object(module, "for_provider_type", return_value=provider):
			name = module.allocate(server)
		rip = frappe.get_doc("Reserved IP", name)
		self.assertEqual(rip.ip_address, "203.0.113.50")
		self.assertEqual(rip.server, server)
		self.assertEqual(rip.provider_resource_id, "203.0.113.50")
		self.assertEqual(rip.status, "Allocated")
		# Resolved the provider via the Server's own provider row.
		provider.allocate_reserved_ip.assert_called_once_with()

	def test_discover_imports_only_this_droplets_ips_and_skips_known(self) -> None:
		server = _ensure_test_server()
		frappe.db.set_value("Server", server, "provider_resource_id", "droplet-1")
		# One already-modelled IP; discover must skip it.
		_make_reserved_ip(server, "203.0.113.60")
		provider = MagicMock()
		provider.list_reserved_ips.return_value = [
			ReservedIp("203.0.113.60", "203.0.113.60", droplet_resource_id="droplet-1"),  # known
			ReservedIp("203.0.113.61", "203.0.113.61", droplet_resource_id="droplet-1"),  # new, this host
			ReservedIp("203.0.113.62", "203.0.113.62", droplet_resource_id="droplet-2"),  # other host
			ReservedIp("203.0.113.63", "203.0.113.63", droplet_resource_id=None),  # floating
		]
		with patch.object(module, "for_provider_type", return_value=provider):
			created = module.discover(server)
		self.assertEqual(len(created), 1)
		new = frappe.get_doc("Reserved IP", created[0])
		self.assertEqual(new.ip_address, "203.0.113.61")
		self.assertEqual(new.server, server)
		# The other-host and floating IPs were not imported.
		self.assertFalse(frappe.db.exists("Reserved IP", {"ip_address": "203.0.113.62"}))
		self.assertFalse(frappe.db.exists("Reserved IP", {"ip_address": "203.0.113.63"}))

	def test_discover_throws_without_droplet_id(self) -> None:
		server = make_server(
			make_provider("rip-noid-provider"),
			"rip-noid-server",
			ipv4_address="10.0.0.97",
			ipv6_address="2001:db8:3::1",
			ipv6_prefix="2001:db8:3::/64",
			ipv6_virtual_machine_range="2001:db8:3::/124",
			status="Active",
			provider_resource_id=None,
		)
		with self.assertRaises(frappe.ValidationError) as raised:
			module.discover(server.name)
		self.assertIn("provider_resource_id", str(raised.exception))

	def test_release_destroys_vendor_ip_and_deletes_row(self) -> None:
		server = _ensure_test_server()
		rip = _make_reserved_ip(server, "203.0.113.70")
		provider = MagicMock()
		with patch.object(module, "for_provider_type", return_value=provider):
			rip.release()
		provider.release_reserved_ip.assert_called_once_with("do-reserved-1")
		self.assertFalse(frappe.db.exists("Reserved IP", rip.name))

	def test_release_refuses_while_attached(self) -> None:
		server = _ensure_test_server()
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.71")
		with _mock_host_side():
			rip.attach(vm.name)
		provider = MagicMock()
		with patch.object(module, "for_provider_type", return_value=provider):
			with self.assertRaises(frappe.ValidationError) as raised:
				rip.release()
		self.assertIn("Detach", str(raised.exception))
		provider.release_reserved_ip.assert_not_called()

	def test_delete_row_does_not_touch_vendor(self) -> None:
		# Deleting the Frappe row is a local drop; the vendor IP survives.
		server = _ensure_test_server()
		rip = _make_reserved_ip(server, "203.0.113.72")
		with patch.object(module, "for_provider_type") as for_provider_type:
			frappe.delete_doc("Reserved IP", rip.name, ignore_permissions=True)
		for_provider_type.assert_not_called()

	def test_delete_refuses_while_attached(self) -> None:
		server = _ensure_test_server()
		vm = _new_vm()
		rip = _make_reserved_ip(server, "203.0.113.73")
		with _mock_host_side():
			rip.attach(vm.name)
		with self.assertRaises(frappe.ValidationError) as raised:
			frappe.delete_doc("Reserved IP", rip.name, ignore_permissions=True)
		self.assertIn("Detach", str(raised.exception))
