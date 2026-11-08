from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.dns_providers.base import DnsProvider
from atlas.atlas.core.ssh import SSHResult
from atlas.service.core.runway_cache import provisioning
from atlas.service.core.runway_cache.provisioning import RunwayCacheServerProvisioner
from atlas.service.core.runway_cache.proxy import proxy_route
from atlas.service.doctype.runway_cache_server.runway_cache_server import (
	RunwayCacheServer,
	get_cache_firewall,
)


def cache_record(**values):
	return frappe.get_doc(
		{
			"doctype": "Runway Cache Server",
			"name": "Runway Cache Server",
			"status": "Pending",
			"release_url": "https://releases.example.com/cache.tar",
			"release_sha256": "a" * 64,
			"central_url": "https://central.example.com",
			"central_issuer": "central",
			"central_jwks_url": "https://central.example.com/api/method/central.api.jwks.get_jwks",
			"central_region": "blr",
			"guest_port": 8443,
			"operator_port": 8444,
			**values,
		}
	)


class TestCacheRecord(UnitTestCase):
	def test_invalid_pins_and_listener_configuration_are_rejected(self):
		for values in (
			{"release_url": "http://example.com/release"},
			{"release_url": "https://secret@example.com/release"},
			{"release_sha256": "no-hash"},
			{"operator_port": 8443},
			{"guest_port": 22},
			{"central_url": "https://central.example.com/path"},
			{"central_jwks_url": "https://other.example.com/jwks"},
		):
			with self.subTest(values=values), self.assertRaises(frappe.ValidationError):
				cache_record(**values).validate()
		cache_record().validate()

	def test_listener_changes_require_recreation(self):
		cache = cache_record(virtual_machine="vm-00001", guest_port=9443)
		with patch.object(
			cache, "get_doc_before_save", return_value=cache_record(virtual_machine="vm-00001")
		):
			with self.assertRaises(frappe.ValidationError):
				cache.validate()

	def test_existing_vm_prevents_duplicate_provisioning(self):
		cache = cache_record(virtual_machine="vm-00001")
		with (
			patch("atlas.service.doctype.runway_cache_server.runway_cache_server._validate_system_manager"),
			patch.object(frappe, "get_single", return_value=cache),
			patch("atlas.vm.core.vm_service.VirtualMachineService.create") as create,
			self.assertRaises(frappe.ValidationError),
		):
			cache.provision({})
		create.assert_not_called()

	def test_vm_creation_forces_service_identity_and_protection(self):
		cache = cache_record()
		cache.save = Mock()
		cache.enqueue_provisioning = Mock()
		settings = SimpleNamespace(region_id=3, public_ssh_key="")
		image = SimpleNamespace(image_type="system", validate_compatibility=Mock())
		with (
			patch("atlas.service.doctype.runway_cache_server.runway_cache_server._validate_system_manager"),
			patch.object(
				frappe,
				"get_single",
				side_effect=lambda name: cache if name == "Runway Cache Server" else settings,
			),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "commit"),
			patch("atlas.vm.core.vm_service.VirtualMachineService.get_image", return_value=image),
			patch(
				"atlas.vm.core.vm_service.VirtualMachineService.create",
				return_value={"name": "vm-00001", "is_draft": True},
			) as create,
		):
			cache.provision(
				{
					"virtual_machine_image": "image",
					"cpu_millicores": 2000,
					"memory_mib": 4096,
					"disk_mib": 32768,
					"tenant_id": 99,
					"is_privileged": False,
				}
			)
		request = create.call_args.args[0]
		self.assertEqual(request["tenant_id"], 0)
		self.assertTrue(request["is_privileged"])
		self.assertTrue(request["is_termination_protected"])
		self.assertTrue(request["firewall"]["enabled"])
		self.assertEqual(cache.virtual_machine, "vm-00001")
		cache.enqueue_provisioning.assert_called_once()

	def test_interrupted_archive_retries_route_cleanup_before_termination(self):
		cache = cache_record(status="Archived", virtual_machine="vm-00001")
		cache.save = Mock()
		with (
			patch("atlas.service.doctype.runway_cache_server.runway_cache_server._validate_system_manager"),
			patch.object(frappe, "get_single", return_value=cache),
			patch.object(frappe.db, "commit"),
			patch.object(frappe, "get_doc") as get_vm,
			patch.object(
				RunwayCacheServerProvisioner,
				"push_proxy_routes",
				side_effect=RuntimeError("proxy unavailable"),
			) as routes,
			self.assertRaises(RuntimeError),
		):
			cache.archive()
		routes.assert_called_once()
		get_vm.assert_not_called()
		self.assertEqual(cache.virtual_machine, "vm-00001")
		self.assertEqual(cache.status, "Failed")
		self.assertIn("archive:", cache.failure_message)

	def test_firewall_keeps_operator_traffic_in_tenant_zero(self):
		rules = get_cache_firewall(3, 8443, 8444)["inbound"]
		guest = ipaddress.ip_address("fdaa:3:0:42::1")
		proxy = ipaddress.ip_address("fdaa:3::1")
		self.assertIn(guest, ipaddress.ip_network(rules[0]["cidrs"][0]))
		self.assertNotIn(guest, ipaddress.ip_network(rules[1]["cidrs"][0]))
		self.assertIn(proxy, ipaddress.ip_network(rules[1]["cidrs"][0]))
		self.assertNotIn(ipaddress.ip_address("fdaa:4::1"), ipaddress.ip_network(rules[0]["cidrs"][0]))

	def test_internal_dns_label_accepts_only_mesh_addresses(self):
		provider = Mock(spec=DnsProvider)
		provider.domain_name = "example.com"
		provider._is_internal_service_record = DnsProvider._is_internal_service_record.__get__(provider)
		DnsProvider.upsert_service_record(
			provider, "internal-runway-cache-svc.example.com", ipaddress.ip_address("fdaa:3::1"), 300
		)
		provider.upsert_record.assert_called_once_with(
			"AAAA", "internal-runway-cache-svc.example.com", ["fdaa:3::1"], 300, "A"
		)
		with self.assertRaises(Exception):
			DnsProvider.upsert_service_record(
				provider, "internal-runway-cache-svc.example.com", ipaddress.ip_address("8.8.8.8"), 300
			)


class TestCacheProvisioning(UnitTestCase):
	def test_configuration_contains_no_central_credential_or_signing_key(self):
		cache = cache_record(virtual_machine="vm-00001")
		with (
			patch.object(frappe, "get_single", return_value=SimpleNamespace(region_id=3)),
			patch.object(
				RunwayCacheServerProvisioner, "mesh_address", new=property(lambda self: "fdaa:3::1")
			),
		):
			config = RunwayCacheServerProvisioner(cache).configuration()
		self.assertEqual(config["listen"], "[fdaa:3::1]:8443")
		self.assertEqual(config["operator_listen"], "[fdaa:3::1]:8444")
		self.assertEqual(config["central_region_id"], 3)
		self.assertEqual(config["audience"], "https://127.0.0.1:18445")
		self.assertEqual(config["central_token_file"], "/etc/runway/central-token")
		self.assertNotIn("credential", config)
		self.assertNotIn("signing_key", config)

	def test_install_failure_records_phase_and_stops_publication(self):
		cache = cache_record(virtual_machine="vm-00001")
		cache.save = Mock()
		provisioner = RunwayCacheServerProvisioner(cache)
		with (
			patch.object(frappe, "get_single", return_value=cache),
			patch.object(
				RunwayCacheServer, "domain", new=property(lambda self: "runway-cache-svc.example.com")
			),
			patch.object(frappe.db, "commit"),
			patch.object(
				RunwayCacheServerProvisioner,
				"virtual_machine",
				new=property(lambda self: SimpleNamespace(is_draft=False)),
			),
			patch.object(provisioner, "wait_for_ssh"),
			patch.object(provisioner, "write_configuration"),
			patch.object(
				provisioning.SSHTask,
				"create_for_script_file",
				return_value=SimpleNamespace(name="task", result=SSHResult("hash mismatch", 1)),
			),
			patch.object(provisioner, "publish_dns") as dns,
			patch.object(provisioner, "push_proxy_routes") as routes,
			self.assertRaises(frappe.ValidationError),
		):
			provisioner.run()
		self.assertEqual(cache.status, "Failed")
		self.assertIn("installation:", cache.failure_message)
		dns.assert_not_called()
		routes.assert_not_called()

	def test_draft_vm_defers_setup(self):
		cache = cache_record(virtual_machine="vm-00001")
		with (
			patch.object(frappe, "get_single", return_value=cache),
			patch.object(
				RunwayCacheServerProvisioner,
				"virtual_machine",
				new=property(lambda self: SimpleNamespace(is_draft=True)),
			),
			patch.object(provisioning.SSHTask, "create_for_script_file") as install,
		):
			RunwayCacheServerProvisioner(cache).run()
		install.assert_not_called()
		self.assertEqual(cache.status, "Pending")

	def test_operator_route_verifies_tls_and_exposes_only_configure(self):
		cache = cache_record(status="Active", virtual_machine="vm-00001", operator_ca="public-ca")
		with (
			patch.object(frappe, "get_single", return_value=cache),
			patch.object(
				RunwayCacheServer, "domain", new=property(lambda self: "runway-cache-svc.example.com")
			),
			patch(
				"atlas.service.core.runway_cache.proxy.get_virtual_machine_mesh_address",
				return_value="fdaa:3::1",
			),
		):
			route, ca = proxy_route()
			self.assertIn("location = /configure", route)
			self.assertIn("proxy_pass https://[fdaa:3::1]:8444", route)
			self.assertIn("proxy_ssl_verify on", route)
			self.assertIn("location / { return 404; }", route)
			self.assertEqual(ca, "public-ca")
			cache.status = "Archived"
			self.assertEqual(proxy_route(), ("", ""))


class TestPinnedInstaller(UnitTestCase):
	def test_verified_install_skip_and_hash_mismatch(self):
		"""Execute the shared installer in a temporary tree with a local download fixture."""
		source = (Path(provisioning.__file__).parents[3] / "scripts/install-service-package.sh").read_text()
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			script = root / "install.sh"
			script.write_text(source.replace("/opt/atlas", str(root / "atlas")))
			archive = root / "release.tar"
			with tarfile.open(archive, "w") as tar:
				content = b'#!/bin/sh\necho installed >> "$INSTALL_COUNT"\n'
				info = tarfile.TarInfo("runway-cache/setup.sh")
				info.size = len(content)
				tar.addfile(info, io.BytesIO(content))
			bin_directory = root / "bin"
			bin_directory.mkdir()
			(bin_directory / "id").write_text("#!/bin/sh\necho 0\n")
			(bin_directory / "curl").write_text('#!/bin/sh\ncp "$TEST_ARCHIVE" "$4"\n')
			for path in bin_directory.iterdir():
				path.chmod(0o755)
			environment = dict(
				os.environ,
				PATH=f"{bin_directory}:{os.environ['PATH']}",
				PACKAGE_NAME="runway-cache",
				PACKAGE_SETUP_SCRIPT="setup.sh",
				PACKAGE_DOWNLOAD_URL="https://example.com/release.tar",
				PACKAGE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
				TEST_ARCHIVE=str(archive),
				INSTALL_COUNT=str(root / "count"),
			)

			def run():
				return subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True)

			self.assertEqual(run().returncode, 0)
			self.assertEqual(run().returncode, 0)
			self.assertEqual((root / "count").read_text(), "installed\n")
			environment["PACKAGE_SHA256"] = "0" * 64
			self.assertNotEqual(run().returncode, 0)
			self.assertEqual((root / "count").read_text(), "installed\n")
			self.assertNotEqual((root / "atlas/runway-cache-package-hash").read_text().strip(), "0" * 64)
