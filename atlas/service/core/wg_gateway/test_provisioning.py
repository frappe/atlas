from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.core.wg_gateway.provisioning as provisioning
from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner

PACKAGE_ENVIRONMENT = {
	"PACKAGE_NAME": "wg-gateway",
	"PACKAGE_SETUP_SCRIPT": "setup.sh",
	"PACKAGE_DOWNLOAD_URL": "https://atlas.example.com/files/wg-gateway.tar",
	"PACKAGE_SHA256": "sha-1",
}


def gateway(**values) -> SimpleNamespace:
	defaults = {
		"name": "wg-gateway-001",
		"status": "Provisioning",
		"virtual_machine": "vm-00001",
		"listen_port": 51820,
		"wireguard_mesh_ipv6": "fdaa:1::99",
		"gateway_public_key": None,
		"installation_task": None,
		"api_token": "daemon-token",
		"save": Mock(),
	}
	return SimpleNamespace(**(defaults | values))


def settings(**values) -> SimpleNamespace:
	defaults = {
		"wildcard_domain": "par-1.example.com",
		"region_id": 1,
	}
	return SimpleNamespace(
		**(defaults | values),
		get_password=lambda *args, **kwargs: "proxy-password",
	)


def virtual_machine(**values) -> Mock:
	machine = Mock(**({"name": "vm-00001", "server": "metal-1"} | values))
	machine.get_metal_vm_info.return_value = metal_information("151.115.112.94")
	return machine


def metal_information(public_ipv4: str) -> SimpleNamespace:
	return SimpleNamespace(desired=SimpleNamespace(network=SimpleNamespace(public_ipv4=public_ipv4)))


class TestGatewayInstallation(UnitTestCase):
	def test_the_installer_receives_the_daemon_credential(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		install_task = SimpleNamespace(name="task-1", result=SimpleNamespace(is_success=True))
		with (
			patch.object(
				provisioning.WG_GATEWAY_PACKAGE.__class__,
				"get_install_environment",
				return_value=PACKAGE_ENVIRONMENT,
			),
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(
				provisioning.SSHTask, "create_for_script_file", return_value=install_task
			) as create_task,
		):
			provisioner.install_gateway()

		arguments = create_task.call_args.kwargs
		self.assertEqual(
			arguments["environment"],
			PACKAGE_ENVIRONMENT
			| {
				"REGION_ID": 1,
				"GATEWAY_MESH": "fdaa:1::99",
				"LISTEN_PORT": 51820,
				"DAEMON_TOKEN": "daemon-token",
			},
		)


class TestGatewayApi(UnitTestCase):
	def test_the_proxy_route_maps_the_gateway_hostname(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "patch") as patch_site,
		):
			patch_site.return_value.ok = True
			provisioner.update_proxy_routes()

		self.assertEqual(
			patch_site.call_args.args,
			("https://proxy.par-1.example.com/v1/sites/wg-gateway-001",),
		)
		self.assertEqual(patch_site.call_args.kwargs["headers"], {"Authorization": "Bearer proxy-password"})
		self.assertEqual(patch_site.call_args.kwargs["json"], {"address": "fdaa:1::99"})

	def test_a_refused_route_fails_the_phase(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "patch") as patch_site,
		):
			patch_site.return_value.ok = False
			patch_site.return_value.status_code = 503
			with self.assertRaisesRegex(frappe.ValidationError, "wg-gateway-001"):
				provisioner.update_proxy_routes()

	def test_the_daemon_answers_through_the_proxy(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		response = SimpleNamespace(ok=True, json=lambda: {"status": "ok"})
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "get", return_value=response) as get,
		):
			provisioner.wait_for_daemon()

		self.assertEqual(get.call_args.args, ("https://wg-gateway-001.par-1.example.com/healthz",))
		self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer daemon-token"})

	def test_a_silent_daemon_fails_the_phase(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		clock = SimpleNamespace(monotonic=Mock(side_effect=[0.0, 301.0]), sleep=Mock())
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "get", side_effect=provisioning.requests.Timeout),
			patch.object(provisioning, "time", clock),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "did not answer"):
				provisioner.wait_for_daemon()

	def test_the_public_key_comes_from_the_daemon(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		response = SimpleNamespace(ok=True, json=lambda: {"public_key": "daemon-pubkey\n"})
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "get", return_value=response),
		):
			provisioner.read_public_key()

		self.assertEqual(provisioner.gateway.gateway_public_key, "daemon-pubkey")

	def test_a_missing_key_fails_the_phase(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		response = SimpleNamespace(ok=False, json=lambda: {})
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "get", return_value=response),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "no WireGuard public key"):
				provisioner.read_public_key()

	def test_route_removal_runs_before_termination(self) -> None:
		provisioner = WireGuardGatewayProvisioner(gateway())
		with (
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(provisioning.requests, "delete") as delete_site,
		):
			delete_site.return_value.ok = True
			provisioner.remove_proxy_routes()

		self.assertEqual(
			delete_site.call_args.args, ("https://proxy.par-1.example.com/v1/sites/wg-gateway-001",)
		)
