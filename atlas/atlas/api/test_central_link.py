# Copyright (c) 2026, Frappe and Contributors
# See license.txt
"""Unit tests for the Atlas inbound tunnel-provisioning API (spec/21-tunnel.md).

`run_local_task` is mocked — these assert the orchestration (which scripts run, in
what order), the Central Settings writes, the return shapes, and the System-Manager
guard. The host behaviour of the scripts themselves is covered by the pure-builder
unit tests (test_tunnel / test_firewall) and the two-droplet e2e."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import central_link
from atlas.atlas.secrets import get_secret

PLAIN_USER_EMAIL = "central-link-plain@example.com"

PAYLOAD = {
	"hub_public_key": "HUBPUBKEY=",
	"hub_endpoint": "203.0.113.1:51820",
	"tunnel_ip": "10.88.0.2",
	"tunnel_cidr": "10.88.0.0/16",
	"central_url": "https://central.example",
	"service_api_key": "svc_key",
	"service_api_secret": "svc_secret",
}

TUNNEL_UP_RESULT = {
	"wg_public_key": "SPOKEPUBKEY=",
	"listen_port": 51820,
	"tunnel_ip": "10.88.0.2",
	"interface": "wg0",
}


def _task(result: dict | None = None) -> MagicMock:
	"""A fake completed Task: stdout carries the ATLAS_RESULT= line iff `result`."""
	task = MagicMock()
	task.stdout = f"ATLAS_RESULT={json.dumps(result)}" if result is not None else "done\n"
	return task


def _make_plain_user() -> str:
	"""A user stripped of every role — not a System Manager."""
	if frappe.db.exists("User", PLAIN_USER_EMAIL):
		user = frappe.get_doc("User", PLAIN_USER_EMAIL)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": PLAIN_USER_EMAIL,
				"first_name": "Plain",
				"last_name": "User",
				"send_welcome_email": 0,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	for role_row in list(user.get("roles") or []):
		user.remove(role_row)
	user.save(ignore_permissions=True)
	return user.name


class IntegrationTestCentralLink(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(frappe.set_user, "Administrator")

	# ----- provision_tunnel ------------------------------------------------

	@patch.object(central_link, "run_local_task")
	def test_provision_tunnel_happy_path(self, run_local_task) -> None:
		run_local_task.side_effect = [_task(TUNNEL_UP_RESULT), _task()]

		out = central_link.provision_tunnel(**PAYLOAD)

		# Returns the spoke identity the hub needs to peer.
		self.assertEqual(
			out,
			{"wg_public_key": "SPOKEPUBKEY=", "listen_port": 51820, "tunnel_ip": "10.88.0.2"},
		)
		# tunnel-up THEN firewall-apply (the lockdown only after wg0 is up).
		scripts = [call.kwargs["script"] for call in run_local_task.call_args_list]
		self.assertEqual(scripts, ["tunnel-up", "mgmt-firewall-apply"])
		# firewall-apply runs with the auto-revert armed (no flags = defaults).
		self.assertEqual(run_local_task.call_args_list[1].kwargs["variables"], {})

		settings = frappe.get_single("Central Settings")
		self.assertEqual(settings.url, "https://central.example")
		self.assertEqual(settings.api_key, "svc_key")
		self.assertEqual(settings.tunnel_ip, "10.88.0.2")
		self.assertEqual(settings.tunnel_cidr, "10.88.0.0/16")
		self.assertEqual(settings.hub_public_key, "HUBPUBKEY=")
		self.assertEqual(settings.hub_endpoint, "203.0.113.1:51820")
		self.assertEqual(settings.wg_public_key, "SPOKEPUBKEY=")
		self.assertEqual(settings.wg_listen_port, 51820)
		self.assertEqual(settings.tunnel_status, "Provisioning")
		# The pushed service-user secret is stored encrypted, readable back.
		self.assertEqual(get_secret("Central Settings", "Central Settings", "api_secret"), "svc_secret")

	@patch.object(central_link, "run_local_task")
	def test_provision_tunnel_passes_keypath_and_tunnel_params(self, run_local_task) -> None:
		run_local_task.side_effect = [_task(TUNNEL_UP_RESULT), _task()]

		central_link.provision_tunnel(**PAYLOAD)

		tunnel_vars = run_local_task.call_args_list[0].kwargs["variables"]
		self.assertEqual(tunnel_vars["PRIVATE_KEY_PATH"], central_link.SPOKE_PRIVATE_KEY_PATH)
		self.assertEqual(tunnel_vars["TUNNEL_IP"], "10.88.0.2")
		self.assertEqual(tunnel_vars["TUNNEL_CIDR"], "10.88.0.0/16")
		self.assertEqual(tunnel_vars["HUB_PUBLIC_KEY"], "HUBPUBKEY=")
		self.assertEqual(tunnel_vars["HUB_ENDPOINT"], "203.0.113.1:51820")

	@patch.object(central_link, "run_local_task")
	def test_provision_tunnel_rejects_incomplete_payload(self, run_local_task) -> None:
		incomplete = {key: value for key, value in PAYLOAD.items() if key != "service_api_secret"}
		with self.assertRaises(frappe.ValidationError):
			central_link.provision_tunnel(**incomplete)
		# No host work attempted on a bad payload.
		run_local_task.assert_not_called()

	@patch.object(central_link, "run_local_task")
	def test_provision_tunnel_skip_tunnel_stores_creds_without_host_work(self, run_local_task) -> None:
		"""Local dev (skip_tunnel): store the creds and enable reporting, but run
		no host scripts, touch no tunnel fields, and leave the data path on base_url."""
		frappe.db.set_single_value("Central Settings", "enabled", 0)
		# Clear any tunnel residue a sibling provision test left in the shared single, so
		# we assert skip_tunnel never writes these — not that they happen to be empty.
		frappe.db.set_single_value("Central Settings", "wg_public_key", None)
		frappe.db.set_single_value("Central Settings", "tunnel_ip", None)

		out = central_link.provision_tunnel(
			central_url="https://central.example",
			service_api_key="svc_key",
			service_api_secret="svc_secret",
			skip_tunnel=1,
		)

		self.assertEqual(out, {"skip_tunnel": True, "tunnel_status": "Inactive"})
		run_local_task.assert_not_called()

		settings = frappe.get_single("Central Settings")
		self.assertEqual(settings.url, "https://central.example")
		self.assertEqual(settings.api_key, "svc_key")
		self.assertTrue(settings.enabled)
		self.assertEqual(settings.tunnel_status, "Inactive")
		self.assertFalse(settings.wg_public_key)
		self.assertFalse(settings.tunnel_ip)
		self.assertEqual(get_secret("Central Settings", "Central Settings", "api_secret"), "svc_secret")

	# ----- confirm_tunnel --------------------------------------------------

	@patch.object(central_link, "run_local_task")
	def test_confirm_tunnel_persists_and_activates(self, run_local_task) -> None:
		run_local_task.return_value = _task()
		frappe.db.set_single_value("Central Settings", "tunnel_status", "Provisioning")

		out = central_link.confirm_tunnel()

		self.assertEqual(out, {"tunnel_status": "Active"})
		self.assertEqual(run_local_task.call_args.kwargs["script"], "mgmt-firewall-confirm")
		self.assertEqual(frappe.db.get_single_value("Central Settings", "tunnel_status"), "Active")

	# ----- deprovision_tunnel ----------------------------------------------

	@patch.object(central_link, "run_local_task")
	def test_deprovision_tunnel_tears_down_and_clears(self, run_local_task) -> None:
		run_local_task.side_effect = [_task(TUNNEL_UP_RESULT), _task()]
		central_link.provision_tunnel(**PAYLOAD)  # seed an Active-ish provisioned state

		run_local_task.reset_mock()
		run_local_task.side_effect = None  # drop the exhausted provision sequence
		run_local_task.return_value = _task()
		out = central_link.deprovision_tunnel()

		self.assertEqual(out, {"tunnel_status": "Inactive"})
		# firewall reverted BEFORE the tunnel drops, so a remote caller stays reachable.
		scripts = [call.kwargs["script"] for call in run_local_task.call_args_list]
		self.assertEqual(scripts, ["mgmt-firewall-revert", "tunnel-down"])

		settings = frappe.get_single("Central Settings")
		self.assertEqual(settings.tunnel_status, "Inactive")
		self.assertFalse(settings.tunnel_ip)
		self.assertFalse(settings.wg_public_key)
		self.assertFalse(settings.hub_public_key)

	@patch.object(central_link, "run_local_task")
	def test_deprovision_requires_system_manager(self, run_local_task) -> None:
		frappe.set_user(_make_plain_user())
		with self.assertRaises(frappe.PermissionError):
			central_link.deprovision_tunnel()
		run_local_task.assert_not_called()

	# ----- tunnel_status ---------------------------------------------------

	@patch.object(central_link, "run_local_task")
	def test_tunnel_status_reads_back(self, run_local_task) -> None:
		run_local_task.side_effect = [_task(TUNNEL_UP_RESULT), _task()]
		central_link.provision_tunnel(**PAYLOAD)

		status = central_link.tunnel_status()
		self.assertEqual(status["tunnel_status"], "Provisioning")
		self.assertEqual(status["tunnel_ip"], "10.88.0.2")
		self.assertEqual(status["wg_public_key"], "SPOKEPUBKEY=")
		self.assertEqual(status["wg_listen_port"], 51820)

	# ----- the System-Manager guard ---------------------------------------

	@patch.object(central_link, "run_local_task")
	def test_methods_require_system_manager(self, run_local_task) -> None:
		frappe.set_user(_make_plain_user())
		with self.assertRaises(frappe.PermissionError):
			central_link.provision_tunnel(**PAYLOAD)
		with self.assertRaises(frappe.PermissionError):
			central_link.confirm_tunnel()
		with self.assertRaises(frappe.PermissionError):
			central_link.tunnel_status()
		# The guard fires before any host work.
		run_local_task.assert_not_called()
