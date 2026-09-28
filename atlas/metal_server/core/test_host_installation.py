from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import CallbackManager

from atlas.auth.datum_token import TOKEN_LIFETIME
from atlas.metal_server.core.host_installation import HostInstallation, request_datum_token_refresh

SERVER_NAME = "atlas-server-1"


def _installation() -> HostInstallation:
	server = SimpleNamespace(
		name=SERVER_NAME,
		ssh_host="192.0.2.7",
		settings=SimpleNamespace(issuer="atlas:1", region_id=1),
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
			patch("atlas.metal_server.core.host_installation.datetime") as clock,
			patch(
				"atlas.metal_server.core.host_installation.convert_utc_to_system_timezone",
				side_effect=lambda value: value,
			),
			patch("atlas.metal_server.core.host_installation.frappe.db.get_value", return_value=7),
			patch("atlas.metal_server.core.host_installation.frappe.db.set_value") as set_value,
		):
			clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)

			def ship(*args, **kwargs):
				clock.now.return_value = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
				return result

			ssh_runner.return_value.run_script.side_effect = ship
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
		set_value.assert_called_once_with(
			"Metal Server",
			{"name": SERVER_NAME, "datum_tokens_revision": 7},
			"datum_tokens_expire_on",
			datetime(2026, 1, 1) + TOKEN_LIFETIME,
			update_modified=False,
		)

	def test_a_failed_shipment_raises(self) -> None:
		result = SimpleNamespace(output="permission denied", is_success=False, exit_code=1)

		with (
			patch(
				"atlas.metal_server.core.host_installation.frappe.conf",
				{"atlas_datum_url": "https://datum.example"},
			),
			patch("atlas.metal_server.core.host_installation.frappe.get_all", return_value=[]),
			patch("atlas.metal_server.core.host_installation.frappe.db.get_value", return_value=0),
			patch("atlas.metal_server.core.host_installation.frappe.db.set_value") as set_value,
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

		set_value.assert_not_called()


class TestDatumConfiguration(UnitTestCase):
	def test_installer_writes_updates_and_disables_datum_without_changing_other_settings(self) -> None:
		script = (Path(__file__).parents[2] / "scripts/install-metald.sh").read_text()
		configuration_script = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "metald.toml"
			path.write_text('[metald]\nlisten = "127.0.0.1:9000"\n')
			for endpoint in ('https://datum.example/a?x="quoted"&y=1', "https://new.example", ""):
				environment = {**os.environ, "DATUM_URL": endpoint}
				subprocess.run(
					[sys.executable, "-c", configuration_script, str(path)], env=environment, check=True
				)
				configuration = tomllib.loads(path.read_text())
				self.assertEqual(configuration["datum"]["url"], endpoint)
				self.assertEqual(configuration["metald"]["listen"], "127.0.0.1:9000")
				if "token_file" in configuration["datum"]:
					self.assertEqual(configuration["datum"]["token_file"], "/custom/tokens.json")
				else:
					path.write_text(
						path.read_text() + 'token_file = "/custom/tokens.json"\n[other]\nvalue = 1\n'
					)


class TestDatumTokenPlacement(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = frappe.new_doc("Metal Server")
		self.server.update({"name": str(uuid7()), "status": "Running", "is_provisioning_completed": 1})
		self.server.db_insert()

	def test_pending_refresh_is_transactional_and_queued_after_commit(self) -> None:
		frappe.db.savepoint("datum_placement")
		callbacks = CallbackManager()
		with (
			patch.dict(frappe.conf, {"atlas_datum_url": "https://datum.example"}),
			patch("frappe.enqueue_doc") as enqueue,
			patch.object(frappe.db, "after_commit", callbacks),
		):
			request_datum_token_refresh(self.server.name)
			request_datum_token_refresh(self.server.name)
			enqueue.assert_not_called()
			callbacks.run()
		self.assertEqual(frappe.db.get_value("Metal Server", self.server.name, "datum_tokens_revision"), 2)
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		frappe.db.rollback(save_point="datum_placement")
		self.assertEqual(frappe.db.get_value("Metal Server", self.server.name, "datum_tokens_revision"), 0)

	def test_a_placement_change_during_shipment_stays_pending_until_the_next_refresh(self) -> None:
		installation = _installation()
		installation.server.name = self.server.name

		def ship(*args, **kwargs):
			request_datum_token_refresh(self.server.name)
			return SimpleNamespace(is_success=True)

		with (
			patch.dict(frappe.conf, {"atlas_datum_url": "https://datum.example"}),
			patch("frappe.enqueue_doc"),
			patch("atlas.metal_server.core.host_installation.issuer.signing_key", return_value=("key", "id")),
			patch("atlas.metal_server.core.host_installation.issue_datum_token", return_value="token"),
			patch("atlas.metal_server.core.host_installation.SSHRunner") as runner,
		):
			runner.return_value.run_script.side_effect = ship
			installation.install_datum_tokens()
			self.assertIsNone(frappe.db.get_value("Metal Server", self.server.name, "datum_tokens_expire_on"))
			runner.return_value.run_script.side_effect = None
			runner.return_value.run_script.return_value = SimpleNamespace(is_success=True)
			installation.install_datum_tokens()
			self.assertIsNotNone(
				frappe.db.get_value("Metal Server", self.server.name, "datum_tokens_expire_on")
			)

	def test_db_set_placement_refreshes_source_and_destination(self) -> None:
		machine = frappe.new_doc("Virtual Machine")
		machine.update({"name": frappe.generate_hash(length=12), "server": self.server.name})
		machine.db_insert()
		machine = frappe.get_doc("Virtual Machine", machine.name)
		destination = uuid7()
		with (
			patch.dict(frappe.conf, {"atlas_datum_url": "https://datum.example"}),
			patch("atlas.vm.doctype.virtual_machine.virtual_machine.request_datum_token_refresh") as refresh,
		):
			machine.db_set("server", str(destination))
		self.assertEqual(
			{call.args[0] for call in refresh.call_args_list}, {self.server.name, str(destination)}
		)

	def test_queue_failure_keeps_the_request_pending_without_failing_placement(self) -> None:
		callbacks = CallbackManager()
		with (
			patch.dict(frappe.conf, {"atlas_datum_url": "https://datum.example"}),
			patch.object(frappe.db, "after_commit", callbacks),
			patch("frappe.enqueue_doc", side_effect=ConnectionError("queue unavailable")),
			patch("frappe.logger") as logger,
		):
			request_datum_token_refresh(self.server.name)
			callbacks.run()
		logger.return_value.exception.assert_called_once()
		self.assertEqual(frappe.db.get_value("Metal Server", self.server.name, "datum_tokens_revision"), 1)
		self.assertIsNone(frappe.db.get_value("Metal Server", self.server.name, "datum_tokens_expire_on"))

	def test_insert_requests_tokens_and_an_unrelated_change_does_not(self) -> None:
		machine = frappe.new_doc("Virtual Machine")
		machine.server = self.server.name
		machine.flags.created_by_virtual_machine_api = True
		with (
			patch.dict(frappe.conf, {"atlas_datum_url": "https://datum.example"}),
			patch("atlas.vm.doctype.virtual_machine.virtual_machine.request_datum_token_refresh") as refresh,
		):
			machine.insert(ignore_mandatory=True)
			refresh.assert_called_once_with(self.server.name)
			refresh.reset_mock()
			machine = frappe.get_doc("Virtual Machine", machine.name)
			machine.db_set("is_draft", 1)
			refresh.assert_not_called()
