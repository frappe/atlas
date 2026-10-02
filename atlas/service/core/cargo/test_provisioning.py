from __future__ import annotations

import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import frappe
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from frappe.tests import UnitTestCase

import atlas
import atlas.service.core.cargo.provisioning as provisioning
from atlas.atlas.core.ssh import SSHResult
from atlas.service.core.cargo.provisioning import CargoServerProvisioner, generate_installer_password


def script_enrolment_variables() -> list[str]:
	"""Return the ENROLMENT_VARS that install-cargo.sh refuses to run without."""
	script = (Path(atlas.__file__).parent / "scripts" / "install-cargo.sh").read_text()
	declaration = re.search(r'ENROLMENT_VARS="(.*?)"', script, re.DOTALL)
	return declaration.group(1).replace("\\\n", " ").split()


STORAGE_CLUSTER_JSON = (
	'{"storage_node_count":3,"replication_factor":3,'
	'"gateway":{"cpu_millicores":2000,"ram_gb":4,"disk_gb":20},'
	'"storage":{"cpu_millicores":4000,"ram_gb":8,"disk_gb":500}}'
)


TELEMETRY_JSON = (
	'{"repository":"https://github.com/frappe/Datum.git","version":"main",'
	'"telemetry":{"cpu_millicores":2000,"ram_gb":4,"disk_gb":20}}'
)


def stored_storage_cluster():
	"""Stand in for the cluster the provision request stored."""
	return patch.object(provisioning, "storage_cluster_config_json", return_value=STORAGE_CLUSTER_JSON)


def stored_telemetry():
	"""Stand in for the Datum host the provision request stored."""
	return patch.object(provisioning, "telemetry_config_json", return_value=TELEMETRY_JSON)


def cargo_server(**values) -> SimpleNamespace:
	defaults = {
		"name": "Cargo Server",
		"status": "Pending",
		"failure_message": None,
		"installation_task": None,
		"virtual_machine": "vm-00001",
		"domain": "cargo.example.com",
		"pilot_domain": "cargo-pilot.example.com",
		"save": Mock(),
	}
	return SimpleNamespace(**(defaults | values))


def atlas_settings(**values) -> SimpleNamespace:
	defaults = {
		"admin_audience_id": "atlas-admin:3",
		"proxy_audience_id": "atlas-proxy:3",
		"issuer": "atlas:3",
		"jwks_url": "https://atlas.example.com/api/atlas/jwks.json",
		"region_id": 3,
		"region_name": "blr",
		"wildcard_domain": "example.com",
		"get_password": Mock(return_value="cluster-password"),
	}
	return SimpleNamespace(**(defaults | values))


class TestCargoInstallation(UnitTestCase):
	def test_a_failed_service_never_creates_a_replacement(self) -> None:
		server = cargo_server(status="Failed")
		provisioner = CargoServerProvisioner(server)
		step = Mock()
		with (
			patch.object(provisioning, "cargo_lifecycle_lock", return_value=nullcontext()),
			patch.object(provisioning.frappe, "get_single", return_value=server),
			patch.object(
				provisioner.__class__, "steps", new=property(lambda self: (("installation", step),))
			),
		):
			provisioner.run()

		step.assert_not_called()
		self.assertEqual(server.status, "Failed")

	def test_a_failed_step_records_its_phase(self) -> None:
		server = cargo_server()
		provisioner = CargoServerProvisioner(server)
		step = Mock(side_effect=RuntimeError("proxy unavailable"))
		with (
			patch.object(provisioning, "cargo_lifecycle_lock", return_value=nullcontext()),
			patch.object(provisioning.frappe, "get_single", return_value=server),
			patch.object(provisioner.__class__, "is_virtual_machine_ready", new=property(lambda self: True)),
			patch.object(
				provisioner.__class__, "steps", new=property(lambda self: (("proxy-routes", step),))
			),
			patch.object(provisioning.frappe.db, "commit"),
			patch.object(provisioning.frappe, "log_error"),
			self.assertRaises(RuntimeError),
		):
			provisioner.run()

		self.assertEqual(server.status, "Failed")
		self.assertEqual(server.failure_message, "proxy-routes: proxy unavailable")

	def test_ssh_wait_goes_through_the_guest_host(self) -> None:
		virtual_machine = SimpleNamespace(ssh_host="vm-00001", get_ssh_proxy_command=lambda: "ssh host nc")
		provisioner = CargoServerProvisioner(cargo_server())
		with (
			patch.object(
				provisioner.__class__, "virtual_machine", new=property(lambda self: virtual_machine)
			),
			patch.object(provisioning, "wait_for_server") as wait_for_server,
		):
			provisioner.wait_for_ssh()

		wait_for_server.assert_called_once_with(
			host="vm-00001",
			users=("root",),
			timeout_seconds=600,
			poll_interval_seconds=5,
			proxy_command="ssh host nc",
		)

	def test_installation_uses_a_visible_ssh_task(self) -> None:
		server = cargo_server()
		provisioner = CargoServerProvisioner(server)
		with (
			patch.object(provisioner, "install_environment", return_value={"ATLAS_TOKEN": "secret"}),
			patch.object(provisioning.SSHTask, "create_for_script_file") as create_task,
		):
			create_task.return_value = SimpleNamespace(name="SSH-00001", result=SSHResult("", 0))
			provisioner.install_cargo()

		create_task.assert_called_once_with(
			target_type="Virtual Machine",
			target="vm-00001",
			script_path="install-cargo.sh",
			environment={"ATLAS_TOKEN": "secret"},
			timeout_seconds=3_600,
			run_in_background=False,
		)
		self.assertEqual(server.installation_task, "SSH-00001")

	def test_install_environment_issues_the_two_required_tokens(self) -> None:
		settings = atlas_settings()
		provisioner = CargoServerProvisioner(cargo_server())
		with (
			patch.object(provisioner.__class__, "settings", new=property(lambda self: settings)),
			patch.object(provisioning.frappe.utils, "get_url", return_value="https://atlas.example.com"),
			patch.object(provisioning, "issue_token", side_effect=["atlas-token", "proxy-token"]) as issue,
			stored_storage_cluster(),
			stored_telemetry(),
		):
			environment = provisioner.install_environment()

		self.assertEqual(
			issue.call_args_list,
			[
				call(
					settings,
					audience="atlas-admin:3",
					subject="cargo",
					scope="*",
					tenant="0",
					lifetime=timedelta(days=365),
				),
				call(
					settings,
					audience="atlas-proxy:3",
					subject="cargo",
					scope="site:*",
					constraints={"site": {"suffix": "-svc"}},
					lifetime=timedelta(days=365),
				),
			],
		)
		self.assertEqual(environment["ATLAS_TENANT_ID"], 0)
		self.assertEqual(environment["ATLAS_TOKEN"], "atlas-token")
		self.assertEqual(environment["PROXY_TOKEN"], "proxy-token")
		self.assertEqual(environment["JWKS_URL"], "https://atlas.example.com/api/atlas/jwks.json")
		self.assertEqual(environment["SITE"], "cargo.example.com")
		self.assertEqual(environment["ADMIN_DOMAIN"], "cargo-pilot.example.com")
		self.assertEqual(environment["DEFAULT_STORAGE_CLUSTER_CONFIG"], STORAGE_CLUSTER_JSON)
		self.assertEqual(environment["DEFAULT_TELEMETRY_CONFIG"], TELEMETRY_JSON)

	def test_every_enrolment_variable_the_script_requires_is_supplied(self) -> None:
		settings = atlas_settings()
		provisioner = CargoServerProvisioner(cargo_server())
		with (
			patch.object(provisioner.__class__, "settings", new=property(lambda self: settings)),
			patch.object(provisioning.frappe.utils, "get_url", return_value="https://atlas.example.com"),
			patch.object(provisioning, "issue_token", side_effect=["atlas-token", "proxy-token"]),
			stored_storage_cluster(),
			stored_telemetry(),
		):
			environment = provisioner.install_environment()

		for name in script_enrolment_variables():
			self.assertIn(name, environment)
			self.assertNotEqual(environment[name], "", f"{name} is empty")

	def test_generated_passwords_meet_the_installer_rule(self) -> None:
		password = generate_installer_password()

		self.assertEqual(len(password), 32)
		self.assertRegex(password, r"[a-z]")
		self.assertRegex(password, r"[A-Z]")
		self.assertRegex(password, r"[0-9]")
		self.assertRegex(password, r"[^A-Za-z0-9]")

	def test_issued_tokens_have_the_required_claims_and_lifetime(self) -> None:
		private_key = Ed25519PrivateKey.generate()
		private_key_pem = private_key.private_bytes(
			Encoding.PEM,
			PrivateFormat.PKCS8,
			NoEncryption(),
		).decode()
		settings = atlas_settings(
			jwt_signing_key_id="atlas:3:key-1",
			get_password=Mock(return_value=private_key_pem),
		)
		provisioner = CargoServerProvisioner(cargo_server())
		with (
			patch.object(provisioner.__class__, "settings", new=property(lambda self: settings)),
			patch.object(provisioning.frappe.utils, "get_url", return_value="https://atlas.example.com"),
			stored_storage_cluster(),
			stored_telemetry(),
		):
			environment = provisioner.install_environment()

		atlas_claims = jwt.decode(
			environment["ATLAS_TOKEN"],
			private_key.public_key(),
			algorithms=["EdDSA"],
			audience="atlas-admin:3",
		)
		proxy_claims = jwt.decode(
			environment["PROXY_TOKEN"],
			private_key.public_key(),
			algorithms=["EdDSA"],
			audience="atlas-proxy:3",
		)

		self.assertEqual(atlas_claims["iss"], "atlas:3")
		self.assertEqual(atlas_claims["sub"], "cargo")
		self.assertEqual(atlas_claims["scope"], "*")
		self.assertEqual(atlas_claims["tenant"], "0")
		self.assertEqual(proxy_claims["scope"], "site:*")
		self.assertEqual(proxy_claims["constraints"], {"site": {"suffix": "-svc"}})
		self.assertNotIn("tenant", proxy_claims)
		self.assertAlmostEqual(
			datetime.fromtimestamp(atlas_claims["exp"], UTC)
			- datetime.fromtimestamp(atlas_claims["iat"], UTC),
			timedelta(days=365),
			delta=timedelta(seconds=1),
		)


class TestCargoProxyRoute(UnitTestCase):
	def setUp(self) -> None:
		self.settings = atlas_settings()
		self.virtual_machine = SimpleNamespace(
			wireguard_mesh_ipv6="fdaa:3::9",
			public_ipv4="203.0.113.9",
		)
		self.provisioner = CargoServerProvisioner(cargo_server())
		self.patches = (
			patch.object(
				self.provisioner.__class__, "settings", new=property(lambda provisioner: self.settings)
			),
			patch.object(
				self.provisioner.__class__,
				"virtual_machine",
				new=property(lambda provisioner: self.virtual_machine),
			),
		)
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)

	def test_routes_use_mesh_ipv6_and_never_public_ipv4(self) -> None:
		with patch.object(provisioning.requests, "patch") as request:
			request.return_value.ok = True
			self.provisioner.update_proxy_routes()

		self.assertEqual(
			request.call_args_list,
			[
				call(
					"https://proxy.example.com/v1/sites/cargo",
					headers={"Authorization": "Bearer cluster-password"},
					json={"address": "fdaa:3::9"},
					timeout=5,
				),
				call(
					"https://proxy.example.com/v1/sites/cargo-pilot",
					headers={"Authorization": "Bearer cluster-password"},
					json={"address": "fdaa:3::9"},
					timeout=5,
				),
			],
		)
		self.assertNotIn("203.0.113.9", str(request.call_args_list))

	def test_archive_route_removal_is_idempotent_at_the_proxy_api(self) -> None:
		with patch.object(provisioning.requests, "delete") as request:
			request.return_value.ok = True
			self.provisioner.remove_proxy_routes()

		self.assertEqual(
			request.call_args_list,
			[
				call(
					"https://proxy.example.com/v1/sites/cargo",
					headers={"Authorization": "Bearer cluster-password"},
					timeout=5,
				),
				call(
					"https://proxy.example.com/v1/sites/cargo-pilot",
					headers={"Authorization": "Bearer cluster-password"},
					timeout=5,
				),
			],
		)

	def test_a_missing_mesh_address_stops_route_publication(self) -> None:
		self.virtual_machine.wireguard_mesh_ipv6 = None
		with (
			patch.object(provisioning.requests, "patch") as request,
			self.assertRaises(frappe.ValidationError),
		):
			self.provisioner.update_proxy_routes()

		request.assert_not_called()
