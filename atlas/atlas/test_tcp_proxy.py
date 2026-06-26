import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import tcp_proxy
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.placement import atlas_region


def _purge() -> None:
	# Tasks are NOT purged (append-only audit rows; assertions filter by the
	# per-test VM name, a fresh UUID, so stale Tasks can never match) — same
	# reasoning as test_proxy._purge.
	for name in frappe.get_all("Port Mapping", pluck="name"):
		frappe.delete_doc("Port Mapping", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


def _make_mapping(vm: str, target_port: int = 22, **overrides):
	doc = {
		"doctype": "Port Mapping",
		"virtual_machine": vm,
		"target_port": target_port,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _proxy_vm():
	"""A VM marked as a proxy — the reconcile target. The fleet is global now."""
	return _new_vm(is_proxy=1)


@contextlib.contextmanager
def _mock_ssh(responses):
	"""Patch the guest-SSH plumbing tcp_proxy.py uses (mirrors test_proxy._mock_ssh).
	`responses` is a list of (stdout, stderr, exit_code) tuples returned by
	successive run_ssh calls. Yields the run_ssh MagicMock so a test can assert the
	commands/stdin sent."""
	run_ssh = MagicMock(side_effect=list(responses))
	key_cm = MagicMock()
	key_cm.__enter__ = MagicMock(return_value="/tmp/fake.key")
	key_cm.__exit__ = MagicMock(return_value=False)
	with (
		patch.object(tcp_proxy, "run_ssh", run_ssh),
		patch.object(tcp_proxy, "ssh_key_file", return_value=key_cm),
		patch.object(tcp_proxy, "connection_for_guest", return_value=MagicMock(ssh_private_key="KEY")),
	):
		yield run_ssh


class TestReconcile(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def _desired(self, mapping) -> str:
		return tcp_proxy.canonical_json(
			{str(mapping.public_port): f"[{mapping.address}]:{mapping.target_port}"}
		)

	def test_no_drift_skips_sync(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		mapping = _make_mapping(site_vm.name)
		desired = self._desired(mapping)
		# The guest's live map already equals desired → no SYNC.
		with _mock_ssh([(desired, "", 0)]) as run_ssh:
			synced = tcp_proxy.reconcile_proxy(proxy_vm.name)
		self.assertFalse(synced)
		self.assertEqual(run_ssh.call_count, 1)  # GET only

	def test_drift_triggers_bulk_sync_with_canonical_body(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		mapping = _make_mapping(site_vm.name)
		desired = self._desired(mapping)
		# Live map is empty (fresh proxy) → drifted → SYNC.
		with _mock_ssh([("{}\n", "", 0), ("ok\n", "", 0)]) as run_ssh:
			synced = tcp_proxy.reconcile_proxy(proxy_vm.name)
		self.assertTrue(synced)
		self.assertEqual(run_ssh.call_count, 2)
		# The second call is the SYNC; its stdin body is the canonical desired map.
		_, sync_kwargs = run_ssh.call_args_list[1]
		self.assertEqual(sync_kwargs["stdin"], desired)
		sync_command = run_ssh.call_args_list[1].args[2]
		self.assertIn("SYNC", sync_command)
		self.assertIn("stream-admin", sync_command)

	def test_get_command_uses_stream_admin(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("ok\n", "", 0)]) as run_ssh:
			tcp_proxy.reconcile_proxy(proxy_vm.name)
		get_command = run_ssh.call_args_list[0].args[2]
		self.assertIn("stream-admin", get_command)
		self.assertIn("GET", get_command)

	def test_drift_records_a_task_row(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("ok\n", "", 0)]):
			tcp_proxy.reconcile_proxy(proxy_vm.name)
		tasks = frappe.get_all(
			"Task",
			filters={"virtual_machine": proxy_vm.name, "script": "tcp-proxy-sync"},
			pluck="status",
		)
		self.assertEqual(tasks, ["Success"])

	def test_sync_nonzero_exit_raises_and_records_failure(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("", "boom", 1)]):
			with self.assertRaises(frappe.ValidationError):
				tcp_proxy.reconcile_proxy(proxy_vm.name)
		status = frappe.get_all(
			"Task",
			filters={"virtual_machine": proxy_vm.name, "script": "tcp-proxy-sync"},
			pluck="status",
		)
		self.assertEqual(status, ["Failure"])

	def test_sync_error_reply_raises_even_on_clean_exit(self) -> None:
		# The client can exit 0 but print "error...\n" (e.g. malformed body). That is
		# still a failed sync — record + raise loudly, never treat as success.
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("error: incomplete body\n", "", 0)]):
			with self.assertRaises(frappe.ValidationError):
				tcp_proxy.reconcile_proxy(proxy_vm.name)

	def test_reconcile_proxies_targets_every_proxy_vm(self) -> None:
		# One global fleet: reconcile_proxies syncs every proxy VM, no region scoping.
		proxy_a = _proxy_vm()
		proxy_b = _proxy_vm()
		proxy_c = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("ok\n", "", 0)] * 3):
			synced = tcp_proxy.reconcile_proxies()
		self.assertEqual(set(synced), {proxy_a.name, proxy_b.name, proxy_c.name})

	def test_reconcile_proxies_isolates_one_unreachable_proxy(self) -> None:
		proxy_a = _proxy_vm()
		proxy_b = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)

		def ssh_side_effect(connection, key_path, command, timeout_seconds, stdin=None):
			if connection.host == "DEAD":
				raise RuntimeError("guest unreachable")
			if "GET" in command:
				return ("{}\n", "", 0)
			return ("ok\n", "", 0)

		def conn_for(vm):
			host = "DEAD" if vm.name == proxy_a.name else "OK"
			return MagicMock(host=host, ssh_private_key="KEY")

		key_cm = MagicMock()
		key_cm.__enter__ = MagicMock(return_value="/tmp/fake.key")
		key_cm.__exit__ = MagicMock(return_value=False)
		with (
			patch.object(tcp_proxy, "run_ssh", MagicMock(side_effect=ssh_side_effect)),
			patch.object(tcp_proxy, "ssh_key_file", return_value=key_cm),
			patch.object(tcp_proxy, "connection_for_guest", side_effect=conn_for),
		):
			synced = tcp_proxy.reconcile_proxies()
		self.assertEqual(synced, [proxy_b.name])

	def test_get_uses_60s_and_sync_uses_120s_timeout(self) -> None:
		# tcp_proxy deliberately reads with a fast 60s GET and writes with a slower
		# 120s SYNC (a large map can take longer to apply). A regression to a shared or
		# short timeout would silently break big syncs, so pin both — the same split
		# proxy.py uses for the http reconcile.
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("ok\n", "", 0)]) as run_ssh:
			tcp_proxy.reconcile_proxy(proxy_vm.name)
		get_kwargs = run_ssh.call_args_list[0].kwargs
		sync_kwargs = run_ssh.call_args_list[1].kwargs
		self.assertEqual(get_kwargs["timeout_seconds"], 60)
		self.assertEqual(sync_kwargs["timeout_seconds"], 120)

	def test_reconcile_proxies_with_no_proxies_returns_empty_and_makes_no_ssh(self) -> None:
		# An empty proxy fleet is not an error: reconcile_proxies returns []
		# and opens no SSH connection (tolerates an empty fleet, same as the http
		# reconcile — proxies that aren't built yet must not throw).
		site_vm = _new_vm()
		_make_mapping(site_vm.name)  # a mapping exists, but no proxy serves it
		with _mock_ssh([]) as run_ssh:
			synced = tcp_proxy.reconcile_proxies()
		self.assertEqual(synced, [])
		run_ssh.assert_not_called()

	def test_drift_records_region_in_task_variables(self) -> None:
		# The recorded Task carries the region in its variables (the audit row's
		# payload). The region is this Atlas's single region (atlas_region()), no
		# longer denormalized on the VM/mapping.
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_mapping(site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("ok\n", "", 0)]):
			tcp_proxy.reconcile_proxy(proxy_vm.name)
		task = frappe.get_all(
			"Task",
			filters={"virtual_machine": proxy_vm.name, "script": "tcp-proxy-sync"},
			fields=["variables"],
		)[0]
		self.assertIn(atlas_region(), task["variables"])
