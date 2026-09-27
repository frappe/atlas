from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.auth.datum_token import TOKEN_LIFETIME
from atlas.metal_server.core.host_installation import HostInstallation

SERVER_NAME = "atlas-server-1"


def _installation() -> HostInstallation:
	server = SimpleNamespace(
		name=SERVER_NAME,
		ssh_host="192.0.2.7",
		settings=SimpleNamespace(issuer="atlas:1", region_id=1),
		db_set=Mock(),
	)
	return HostInstallation(server)


class TestInstallDatumTokens(UnitTestCase):
	def test_is_a_no_op_without_a_datum_url(self) -> None:
		with (
			patch("atlas.metal_server.core.host_installation.frappe.conf", {}),
			patch("atlas.metal_server.core.host_installation.SSHRunner") as ssh_runner,
		):
			_installation().install_datum_tokens()

		ssh_runner.assert_not_called()

	def test_ships_one_token_per_vm_and_the_host(self) -> None:
		result = SimpleNamespace(is_success=True)
		installation = _installation()

		with (
			patch(
				"atlas.metal_server.core.host_installation.frappe.conf",
				{"atlas_datum_url": "https://datum.example"},
			),
			patch(
				"atlas.metal_server.core.host_installation.frappe.get_all",
				return_value=["vm-00001", "vm-00002"],
			) as get_all,
			patch(
				"atlas.metal_server.core.host_installation.issuer.signing_key",
				return_value=("private-key", "atlas:1:key"),
			) as signing_key,
			patch(
				"atlas.metal_server.core.host_installation.issue_datum_token",
				side_effect=lambda settings, resource_id, **_kwargs: f"token-for-{resource_id}",
			),
			patch("atlas.metal_server.core.host_installation.SSHRunner") as ssh_runner,
			patch(
				"atlas.metal_server.core.host_installation.now_datetime",
				return_value=datetime(2026, 1, 1, 0, 0, 0),
			),
		):
			ssh_runner.return_value.run_script.return_value = result
			installation.install_datum_tokens()

		signing_key.assert_called_once()
		self.assertEqual(get_all.call_args.kwargs["filters"], {"server": SERVER_NAME})
		run_script = ssh_runner.return_value.run_script
		run_script.assert_called_once_with(
			"install-datum-tokens.sh",
			data={"DATUM_TOKEN_BUNDLE": run_script.call_args.kwargs["data"]["DATUM_TOKEN_BUNDLE"]},
			timeout_seconds=120,
		)
		bundle = json.loads(run_script.call_args.kwargs["data"]["DATUM_TOKEN_BUNDLE"])
		self.assertEqual(bundle["host"], f"token-for-{SERVER_NAME}")
		self.assertEqual(bundle["vms"], {"vm-00001": "token-for-vm-00001", "vm-00002": "token-for-vm-00002"})
		installation.server.db_set.assert_called_once_with(
			"datum_tokens_expire_on", datetime(2026, 1, 1, 0, 0, 0) + TOKEN_LIFETIME
		)

	def test_a_failed_shipment_raises(self) -> None:
		result = SimpleNamespace(output="permission denied", is_success=False, exit_code=1)

		with (
			patch(
				"atlas.metal_server.core.host_installation.frappe.conf",
				{"atlas_datum_url": "https://datum.example"},
			),
			patch("atlas.metal_server.core.host_installation.frappe.get_all", return_value=[]),
			patch(
				"atlas.metal_server.core.host_installation.issuer.signing_key",
				return_value=("private-key", "atlas:1:key"),
			),
			patch("atlas.metal_server.core.host_installation.issue_datum_token", return_value="token"),
			patch("atlas.metal_server.core.host_installation.SSHRunner") as ssh_runner,
			patch("atlas.metal_server.core.host_installation.frappe.throw", side_effect=ValueError),
		):
			ssh_runner.return_value.run_script.return_value = result
			with self.assertRaises(ValueError):
				_installation().install_datum_tokens()
