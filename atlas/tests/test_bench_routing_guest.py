"""Guest-side tests for self-service subdomain routing (spec/18, the one-way push
model): the identity injection that carries the routing config into the VM, and the
in-guest `atlas-route` client that reads it.

All stdlib-only, no host:

- The cold path's `Identity` carries `routing_base_url` and writes ONLY
  /etc/atlas-routing.env (no /etc/atlas-vm-uuid — caller resolution is by source
  address); `_mmds_metadata` (the warm path) puts `routing_base_url` in the payload.
- `atlas-warm-freshen.py` writes /etc/atlas-routing.env from that payload.
- `bench/atlas-route-client.py` (`atlas-route`): the TYPED surface the bench-cli wiring
  imports — register → Registered|Declined, deregister → Deregistered, check_label →
  Available|Declined, list_routes → Listing; NotConfigured (no config) / TransportError
  (unreachable / no v6 / unknown wire status). The CLI wrapper's exit codes: register
  non-zero on Declined (abort the create), deregister always 0, etc.
- The IPv6-only transport: the client connects over AF_INET6 only and raises
  TransportError (never a v4 fallback) when the host has no AAAA / no v6 route.

The client is exercised against a real in-process HTTP stub bound to **::1** so the
AF_INET6-only connector + the POST shape + the typed contract are proven, not mocked.
"""

import importlib.util
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_BENCH_DIR = _REPO_ROOT / "bench"
_ROUTE_CLIENT = _BENCH_DIR / "atlas-route-client.py"


def _load_by_path(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


# A driver run in a clean interpreter (provision-vm.py's sys.path shim pulls in
# scripts/lib): build a COLD and a WARM ProvisionInputs WITH a routing base URL and
# emit the cold Identity's field + the warm MMDS payload, to prove both paths carry
# the routing config.
_ROUTING_DRIVER = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("provision_vm", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from atlas.rootfs import Identity

base = "https://atlas.blr1.frappe.dev"
uuid = "12345678-1234-1234-1234-123456789abc"
common = dict(
    virtual_machine_name=uuid, image_name="img", kernel_filename="vmlinux",
    rootfs_filename="rootfs.squashfs", vcpus=2, memory_mb=2048, disk_gb=12,
    mac_address="06:00:01:02:03:04", tap_device="atlas-x", virtual_machine_ipv6="2001:db8::2",
    ipv4_host_cidr="100.64.0.1/30", ipv4_guest_cidr="100.64.0.2/30", ipv4_gateway="100.64.0.1",
    ssh_public_key="ssh-ed25519 AAAA", atlas_fc_uid=12345, atlas_netns="ns",
    host_veth="h", namespace_veth="n", cgroup_arg=[], resource_arg=[],
)
warm = module.ProvisionInputs(**common, warm_snapshot_directory="/var/lib/atlas/snapshots/s", routing_base_url=base)
identity = Identity(uuid=uuid, ipv6_address="2001:db8::2", ssh_public_key="k",
                    ipv4_guest_cidr="100.64.0.2/30", ipv4_gateway="100.64.0.1", routing_base_url=base)
print(json.dumps({
    "base": base,
    "identity_field": identity.routing_base_url,
    "warm_metadata": json.loads(module._mmds_metadata(warm)),
}))
"""


class TestRoutingIdentityInjection(unittest.TestCase):
	"""The controller-side injection carries routing_base_url on BOTH paths."""

	@classmethod
	def setUpClass(cls) -> None:
		import subprocess
		import sys

		result = subprocess.run(
			[sys.executable, "-c", _ROUTING_DRIVER, str(_SCRIPTS_DIR / "provision-vm.py")],
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr
		cls.data = json.loads(result.stdout)

	def test_cold_identity_carries_routing_base_url(self) -> None:
		self.assertEqual(self.data["identity_field"], self.data["base"])

	def test_warm_mmds_payload_carries_routing_base_url(self) -> None:
		identity = self.data["warm_metadata"]["identity"]
		self.assertEqual(identity["routing_base_url"], self.data["base"])
		# The uuid the freshen unit keys on (its adopted-identity marker) is still present
		# beside it — unrelated to routing, untouched by the push-only model.
		self.assertEqual(identity["uuid"], "12345678-1234-1234-1234-123456789abc")

	def test_cold_path_writes_only_routing_env_not_vm_uuid_for_routing(self) -> None:
		# Caller resolution is by source address, so the cold routing writer injects
		# ONLY /etc/atlas-routing.env. /etc/atlas-vm-uuid is the warm-freshen marker
		# (warm.sh / the freshen unit), NOT written by _write_routing_identity. Check the
		# CODE lines (the install_file calls), not the docstring — which mentions the
		# uuid path precisely to explain that routing does NOT write it.
		source = (_SCRIPTS_DIR / "lib" / "atlas" / "rootfs.py").read_text()
		writer = source.split("def _write_routing_identity")[1].split("\ndef ")[0]
		# Strip the docstring (which mentions atlas-vm-uuid precisely to explain it is
		# NOT written) so the assertions see only the executable body.
		body = writer.split('"""')[-1]
		self.assertIn("/etc/atlas-routing.env", body)
		self.assertIn("ATLAS_BASE_URL=", body)
		# Exactly one install_file call, and it targets routing.env — never the uuid.
		self.assertEqual(body.count("install_file"), 1, "exactly one install_file call (routing.env)")
		self.assertNotIn("atlas-vm-uuid", body, "the cold routing writer must not write /etc/atlas-vm-uuid")


class TestFreshenWritesRoutingEnv(unittest.TestCase):
	"""The warm path's in-guest freshen unit writes /etc/atlas-routing.env."""

	def test_freshen_has_routing_env_path_and_writer(self) -> None:
		source = (_BENCH_DIR / "atlas-warm-freshen.py").read_text()
		self.assertIn("/etc/atlas-routing.env", source)
		self.assertIn("ATLAS_BASE_URL=", source)
		self.assertIn("routing_base_url", source)

	def test_freshen_module_parses(self) -> None:
		module = _load_by_path("freshen_routing", _BENCH_DIR / "atlas-warm-freshen.py")
		self.assertEqual(module.ROUTING_ENV_PATH, "/etc/atlas-routing.env")


class _IPv6HTTPServer(HTTPServer):
	"""An HTTPServer bound to the IPv6 loopback, so the client's AF_INET6-only connector
	can actually reach it (the client refuses IPv4)."""

	address_family = socket.AF_INET6


class _StubHandler(BaseHTTPRequestHandler):
	"""A minimal Frappe-method stub: returns the configured JSON for the called method,
	wrapped as {"message": …} like Frappe does. Reads config off the OWNING server
	(server.responses / server.calls), so each test's server is isolated."""

	def log_message(self, *_args) -> None:  # silence the test output
		pass

	def do_POST(self) -> None:
		length = int(self.headers.get("Content-Length", 0))
		body = self.rfile.read(length).decode()
		self.server.calls.append((self.path, body))
		# /api/method/atlas.atlas.bench_routing.<method>; key on the last dot-segment.
		method = self.path.rsplit(".", 1)[-1]
		status, payload = self.server.responses.get(method, (404, {"message": {"status": "ok"}}))
		data = json.dumps(payload).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)


class _ClientTestCase(unittest.TestCase):
	"""Base: a real ::1-bound HTTP stub + a loaded client module pointed at a scratch
	routing.env. The client connects over IPv6 only, so the stub MUST be on ::1."""

	def setUp(self) -> None:
		self.server = _IPv6HTTPServer(("::1", 0), _StubHandler)
		self.server.responses = {}
		self.server.calls = []
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.port = self.server.server_address[1]
		self.base = f"http://[::1]:{self.port}"
		self.addCleanup(self.thread.join)
		self.addCleanup(self.server.server_close)
		self.addCleanup(self.server.shutdown)
		self.client = _load_by_path("route_client", _ROUTE_CLIENT)

	def _set_config(self, base_url: str | None) -> None:
		import tempfile

		tmp = Path(tempfile.mkdtemp())
		self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
		if base_url is not None:
			(tmp / "routing.env").write_text(f"ATLAS_BASE_URL={base_url}\n")
			self.client.ROUTING_ENV_PATH = str(tmp / "routing.env")
		else:
			self.client.ROUTING_ENV_PATH = str(tmp / "absent.env")


class TestTypedRegister(_ClientTestCase):
	def test_register_ok_returns_registered_with_fqdn(self) -> None:
		self.server.responses = {
			"register": (200, {"message": {"status": "ok", "suffix": "blr1.frappe.dev"}})
		}
		self._set_config(self.base)
		outcome = self.client.register("acme")
		self.assertIsInstance(outcome, self.client.Registered)
		self.assertEqual(outcome.label, "acme")
		self.assertEqual(outcome.fqdn, "acme.blr1.frappe.dev")
		# The POST went to the register method with the label, over IPv6, no second call.
		path, body = self.server.calls[-1]
		self.assertTrue(path.endswith("/api/method/atlas.atlas.bench_routing.register"))
		self.assertIn("label=acme", body)
		self.assertEqual(len(self.server.calls), 1)

	def test_register_taken_returns_declined(self) -> None:
		self.server.responses = {"register": (200, {"message": {"status": "taken"}})}
		self._set_config(self.base)
		outcome = self.client.register("acme")
		self.assertIsInstance(outcome, self.client.Declined)
		self.assertEqual(outcome.reason, self.client.Reason.TAKEN)

	def test_register_at_limit_returns_declined(self) -> None:
		self.server.responses = {"register": (200, {"message": {"status": "at_limit"}})}
		self._set_config(self.base)
		outcome = self.client.register("acme")
		self.assertIsInstance(outcome, self.client.Declined)
		self.assertEqual(outcome.reason, self.client.Reason.AT_LIMIT)

	def test_register_invalid_carries_controller_message(self) -> None:
		self.server.responses = {
			"register": (200, {"message": {"status": "invalid", "reason": "must be a single label"}})
		}
		self._set_config(self.base)
		outcome = self.client.register("ac.me")
		self.assertIsInstance(outcome, self.client.Declined)
		self.assertEqual(outcome.reason, self.client.Reason.INVALID)
		self.assertEqual(outcome.message, "must be a single label")

	def test_unknown_wire_status_is_transport_error(self) -> None:
		# A status the Reason enum doesn't know must NOT pass silently.
		self.server.responses = {"register": (200, {"message": {"status": "teapot"}})}
		self._set_config(self.base)
		with self.assertRaises(self.client.TransportError):
			self.client.register("acme")

	def test_no_config_raises_not_configured(self) -> None:
		self._set_config(None)
		with self.assertRaises(self.client.NotConfigured):
			self.client.register("acme")


class TestTypedDeregisterCheckList(_ClientTestCase):
	def test_deregister_returns_deregistered(self) -> None:
		self.server.responses = {"deregister": (200, {"message": {"status": "ok"}})}
		self._set_config(self.base)
		outcome = self.client.deregister("acme")
		self.assertIsInstance(outcome, self.client.Deregistered)
		self.assertEqual(outcome.label, "acme")
		path, body = self.server.calls[-1]
		self.assertTrue(path.endswith("bench_routing.deregister"))
		self.assertIn("label=acme", body)

	def test_check_label_available(self) -> None:
		self.server.responses = {
			"check_label": (200, {"message": {"status": "ok", "suffix": "blr1.frappe.dev"}})
		}
		self._set_config(self.base)
		outcome = self.client.check_label("acme")
		self.assertIsInstance(outcome, self.client.Available)
		self.assertEqual(outcome.suffix, "blr1.frappe.dev")

	def test_check_label_declined(self) -> None:
		self.server.responses = {"check_label": (200, {"message": {"status": "reserved"}})}
		self._set_config(self.base)
		outcome = self.client.check_label("admin")
		self.assertIsInstance(outcome, self.client.Declined)
		self.assertEqual(outcome.reason, self.client.Reason.RESERVED)

	def test_list_returns_listing_of_routes(self) -> None:
		self.server.responses = {
			"list": (
				200,
				{
					"message": {
						"domains": [
							{"label": "acme", "fqdn": "acme.blr1.frappe.dev", "active": True},
							{"label": "widgets", "fqdn": "widgets.blr1.frappe.dev", "active": False},
						]
					}
				},
			)
		}
		self._set_config(self.base)
		listing = self.client.list_routes()
		self.assertIsInstance(listing, self.client.Listing)
		self.assertEqual(len(listing.domains), 2)
		self.assertEqual(listing.domains[0].label, "acme")
		self.assertTrue(listing.domains[0].active)
		self.assertFalse(listing.domains[1].active)

	def test_list_empty_is_empty_listing(self) -> None:
		self.server.responses = {"list": (200, {"message": {"domains": []}})}
		self._set_config(self.base)
		self.assertEqual(self.client.list_routes().domains, [])


class TestTransport(_ClientTestCase):
	def test_unreachable_controller_is_transport_error(self) -> None:
		# Point at a closed port on ::1 (v6 route exists, nothing listening).
		self._set_config("http://[::1]:1")
		with self.assertRaises(self.client.TransportError):
			self.client.register("acme")

	def test_non_2xx_is_transport_error(self) -> None:
		self.server.responses = {"register": (500, {"message": "boom"})}
		self._set_config(self.base)
		with self.assertRaises(self.client.TransportError):
			self.client.register("acme")

	def test_ipv4_only_host_raises_transport_error_no_fallback(self) -> None:
		# 127.0.0.1 has NO AAAA / no v6 route — the AF_INET6-only connector must raise
		# TransportError, never silently fall back to IPv4.
		self._set_config(f"http://127.0.0.1:{self.port}")
		with self.assertRaises(self.client.TransportError):
			self.client.register("acme")

	def test_connector_resolves_only_inet6(self) -> None:
		# The connector asks getaddrinfo for AF_INET6 only — assert that directly so a
		# refactor that drops the family is caught.
		source = _ROUTE_CLIENT.read_text()
		self.assertIn("socket.AF_INET6", source)
		self.assertNotIn("socket.AF_INET,", source)  # never the v4 family


class TestCLIWrapper(_ClientTestCase):
	"""The CLI exit-code contract the bench-cli wiring depends on."""

	def test_register_ok_exits_zero(self) -> None:
		self.server.responses = {
			"register": (200, {"message": {"status": "ok", "suffix": "blr1.frappe.dev"}})
		}
		self._set_config(self.base)
		self.assertEqual(self.client.main(["atlas-route", "register", "acme"]), 0)

	def test_register_declined_exits_two_aborting_the_create(self) -> None:
		self.server.responses = {"register": (200, {"message": {"status": "taken"}})}
		self._set_config(self.base)
		# Non-zero so the bench-cli flow ABORTS before `bench new-site`.
		self.assertEqual(self.client.main(["atlas-route", "register", "acme"]), 2)

	def test_register_no_config_is_a_noop_zero(self) -> None:
		self._set_config(None)
		self.assertEqual(self.client.main(["atlas-route", "register", "acme"]), 0)

	def test_register_unreachable_fails_open_zero(self) -> None:
		self._set_config("http://[::1]:1")
		self.assertEqual(self.client.main(["atlas-route", "register", "acme"]), 0)

	def test_deregister_always_exits_zero(self) -> None:
		self.server.responses = {"deregister": (200, {"message": {"status": "ok"}})}
		self._set_config(self.base)
		self.assertEqual(self.client.main(["atlas-route", "deregister", "acme"]), 0)

	def test_deregister_unreachable_still_zero(self) -> None:
		self._set_config("http://[::1]:1")
		self.assertEqual(self.client.main(["atlas-route", "deregister", "acme"]), 0)

	def test_check_label_declined_exits_two(self) -> None:
		self.server.responses = {"check_label": (200, {"message": {"status": "taken"}})}
		self._set_config(self.base)
		self.assertEqual(self.client.main(["atlas-route", "check-label", "acme"]), 2)

	def test_list_deregisters_a_stray(self) -> None:
		# A routed label with NO matching on-disk site is a stray the client clears with a
		# per-stray deregister. Point the on-disk lister at an empty scratch dir so the
		# routed label is a stray.
		import tempfile

		empty = Path(tempfile.mkdtemp())
		self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
		self.client.BENCH_SITES_DIRECTORY = str(empty)
		self.server.responses = {
			"list": (
				200,
				{
					"message": {
						"domains": [{"label": "stray", "fqdn": "stray.blr1.frappe.dev", "active": True}]
					}
				},
			),
			"deregister": (200, {"message": {"status": "ok"}}),
		}
		self._set_config(self.base)
		self.assertEqual(self.client.main(["atlas-route", "list"]), 0)
		# The stray triggered a deregister POST.
		deregisters = [c for c in self.server.calls if c[0].endswith("bench_routing.deregister")]
		self.assertEqual(len(deregisters), 1)
		self.assertIn("label=stray", deregisters[0][1])

	def test_list_keeps_a_matching_route(self) -> None:
		import tempfile

		sites = Path(tempfile.mkdtemp())
		self.addCleanup(lambda: __import__("shutil").rmtree(sites, ignore_errors=True))
		# The on-disk dir name is the FQDN (bench layout: sites/<fqdn>).
		(sites / "keep.blr1.frappe.dev").mkdir()
		self.client.BENCH_SITES_DIRECTORY = str(sites)
		self.server.responses = {
			"list": (
				200,
				{"message": {"domains": [{"label": "keep", "fqdn": "keep.blr1.frappe.dev", "active": True}]}},
			),
			"deregister": (200, {"message": {"status": "ok"}}),
		}
		self._set_config(self.base)
		self.assertEqual(self.client.main(["atlas-route", "list"]), 0)
		deregisters = [c for c in self.server.calls if c[0].endswith("bench_routing.deregister")]
		self.assertEqual(deregisters, [])  # the matching route is kept

	def test_usage_error(self) -> None:
		self.assertEqual(self.client.main(["atlas-route", "bogus"]), 64)


class TestBuildInstallsClient(unittest.TestCase):
	def test_build_sh_installs_the_route_client(self) -> None:
		source = (_BENCH_DIR / "build.sh").read_text()
		self.assertIn("atlas-route-client.py", source)
		self.assertIn("/usr/local/bin/atlas-route", source)

	def test_route_client_is_stdlib_only(self) -> None:
		source = _ROUTE_CLIENT.read_text()
		self.assertNotIn("import frappe", source)
		self.assertNotIn("from atlas", source)


if __name__ == "__main__":
	unittest.main()
