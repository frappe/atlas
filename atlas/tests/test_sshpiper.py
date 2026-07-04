"""Dedicated SSHPiper gateway control-plane tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas._ssh.transport import Connection
from atlas.atlas import sshpiper


class TestSSHPiperLookup(IntegrationTestCase):
	def test_returns_unique_running_vm_for_valid_gateway_token(self) -> None:
		target = frappe._dict(
			name="target-uuid",
			title="vm1",
			server="server-uuid",
			ipv6_address="2001:db8::10",
			ssh_public_key="ssh-ed25519 AAAA user\n# ignored\n",
		)
		with (
			patch.object(sshpiper, "get_decrypted_password", return_value="secret"),
			patch.object(
				sshpiper.frappe.db,
				"get_value",
				return_value=frappe._dict(is_sshpiper=1, server="server-uuid"),
			),
			patch.object(sshpiper.frappe, "get_all", return_value=[target]) as get_all,
		):
			result = sshpiper.lookup_virtual_machine_ssh("gateway-uuid", "vm1", api_key="secret")
		self.assertEqual(result["host"], "2001:db8::10")
		self.assertEqual(result["public_keys"], ["ssh-ed25519 AAAA user"])
		self.assertEqual(get_all.call_args.kwargs["filters"]["server"], "server-uuid")

	def test_duplicate_titles_fail_closed(self) -> None:
		with (
			patch.object(sshpiper, "get_decrypted_password", return_value="secret"),
			patch.object(
				sshpiper.frappe.db,
				"get_value",
				return_value=frappe._dict(is_sshpiper=1, server="server-uuid"),
			),
			patch.object(
				sshpiper.frappe,
				"get_all",
				return_value=[frappe._dict(name="a"), frappe._dict(name="b")],
			),
		):
			with self.assertRaises(frappe.PermissionError):
				sshpiper.lookup_virtual_machine_ssh("gateway-uuid", "vm1", api_key="secret")

	def test_token_for_wrong_gateway_is_rejected(self) -> None:
		with (
			patch.object(sshpiper, "get_decrypted_password", return_value="other-secret"),
			patch.object(
				sshpiper.frappe.db,
				"get_value",
				return_value=frappe._dict(is_sshpiper=1, server="server-uuid"),
			),
		):
			with self.assertRaises(frappe.PermissionError):
				sshpiper.lookup_virtual_machine_ssh("gateway-uuid", "vm1", api_key="secret")


class TestConfigureSSHPiper(IntegrationTestCase):
	def test_requires_reserved_ipv4(self) -> None:
		vm = SimpleNamespace(
			name="gateway-uuid", is_sshpiper=1, status="Running", public_ipv4=None
		)
		with patch.object(sshpiper.frappe, "get_doc", return_value=vm):
			with self.assertRaises(frappe.ValidationError):
				sshpiper.configure_gateway(vm.name)

	def test_writes_runtime_secrets_over_management_port(self) -> None:
		vm = SimpleNamespace(
			name="gateway-uuid",
			server="server-uuid",
			is_sshpiper=1,
			status="Running",
			public_ipv4="203.0.113.10",
			db_set=MagicMock(),
		)
		base = Connection(host="2001:db8::20", ssh_private_key="KEY")
		with (
			patch.object(sshpiper.frappe, "get_doc", return_value=vm),
			patch.object(sshpiper, "_ensure_gateway_token", return_value="token"),
			patch.object(sshpiper, "_read_server_private_key", return_value="SERVER_PRIVATE\n") as read_key,
			patch.object(sshpiper, "connection_for_guest", return_value=base),
			patch.object(sshpiper, "get_url", return_value="https://atlas.example"),
			patch.object(sshpiper, "ssh_key_file") as key_file,
			patch.object(sshpiper, "_write_guest_file") as write,
			patch.object(sshpiper, "run_ssh", return_value=("active\n", "", 0)) as run,
			patch.object(sshpiper, "_record_guest_task"),
		):
			key_file.return_value.__enter__.return_value = "/tmp/key"
			sshpiper.configure_gateway(vm.name)
		read_key.assert_called_once_with("server-uuid")
		self.assertEqual(run.call_args.args[0].port, 222)
		self.assertEqual(write.call_count, 2)
		self.assertEqual(write.call_args_list[0].args[3], "SERVER_PRIVATE\n")
		vm.db_set.assert_called_once_with("sshpiper_configured", 1)
