"""Guest-side tests for self-service subdomain routing (spec/18, the one-way push
model): the identity injection that carries the routing config into the VM, and the
in-guest `bench-domain-provider` binary that reads it.

All stdlib-only, no host:

- The cold path's `Identity` carries `routing_base_url` and writes ONLY
  /etc/atlas-routing.env (no /etc/atlas-vm-uuid — caller resolution is by source
  address); `_mmds_metadata` (the warm path) puts `routing_base_url` in the payload.
- `atlas-warm-freshen.py` writes /etc/atlas-routing.env from that payload.
- `bench/bench-domain-provider.py` (`bench-domain-provider`): the process-I/O contract
  pilot drives — `generate-dns-records <site> <domain>` (read-only `{}` for a wildcard
  subdomain), `register <domain>` (peel the wildcard suffix → label, POST register;
  exit 0 ok / 2 declined / 1 transport, **fail-closed**), `deregister <domain>` (always
  exit 0), `wildcard-domains` / `proxy-servers` (JSON list, fail-soft). NotConfigured (no
  config) no-ops; TransportError fails-closed on register, fail-soft elsewhere.
- The IPv6-only transport: the binary connects over AF_INET6 only and raises
  TransportError (never a v4 fallback) when the host has no AAAA / no v6 route.

The binary is exercised against a real in-process HTTP stub bound to **::1** so the
AF_INET6-only connector + the POST shape + the exit-code contract are proven, not mocked.
The stub always answers `wildcard_domains` with `["*.blr1.frappe.dev"]` so the binary's
suffix-peel resolves; each test sets the per-verb responses it needs on top.
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
_PROVIDER = _BENCH_DIR / "bench-domain-provider.py"

# The region wildcard the stub advertises; the binary peels this suffix off a full FQDN.
_REGION_DOMAIN = "blr1.frappe.dev"
_WILDCARD = f"*.{_REGION_DOMAIN}"


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
	"""An HTTPServer bound to the IPv6 loopback, so the binary's AF_INET6-only connector
	can actually reach it (the binary refuses IPv4)."""

	address_family = socket.AF_INET6


class _StubHandler(BaseHTTPRequestHandler):
	"""A minimal Frappe-method stub: returns the configured JSON for the called method,
	wrapped as {"message": …} like Frappe does. Reads config off the OWNING server
	(server.responses / server.calls), so each test's server is isolated. Defaults
	`wildcard_domains` to the region wildcard so the binary's suffix-peel always resolves
	unless a test overrides it."""

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


class _ProviderTestCase(unittest.TestCase):
	"""Base: a real ::1-bound HTTP stub + a loaded provider module pointed at a scratch
	routing.env. The binary connects over IPv6 only, so the stub MUST be on ::1. The stub
	answers `wildcard_domains` with the region wildcard by default."""

	def setUp(self) -> None:
		self.server = _IPv6HTTPServer(("::1", 0), _StubHandler)
		self.server.responses = {"wildcard_domains": (200, {"message": {"domains": [_WILDCARD]}})}
		self.server.calls = []
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.port = self.server.server_address[1]
		self.base = f"http://[::1]:{self.port}"
		self.addCleanup(self.thread.join)
		self.addCleanup(self.server.server_close)
		self.addCleanup(self.server.shutdown)
		self.provider = _load_by_path("domain_provider", _PROVIDER)

	def _set_config(self, base_url: str | None) -> None:
		import tempfile

		tmp = Path(tempfile.mkdtemp())
		self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
		if base_url is not None:
			(tmp / "routing.env").write_text(f"ATLAS_BASE_URL={base_url}\n")
			self.provider.ROUTING_ENV_PATH = str(tmp / "routing.env")
		else:
			self.provider.ROUTING_ENV_PATH = str(tmp / "absent.env")

	def _set_responses(self, **per_verb) -> None:
		"""Layer per-verb responses on top of the default wildcard_domains answer."""
		self.server.responses.update(per_verb)

	def _register_calls(self) -> list:
		return [c for c in self.server.calls if c[0].endswith("bench_routing.register")]

	def _deregister_calls(self) -> list:
		return [c for c in self.server.calls if c[0].endswith("bench_routing.deregister")]


class TestRegister(_ProviderTestCase):
	def test_register_ok_peels_label_and_exits_zero(self) -> None:
		self._set_responses(register=(200, {"message": {"status": "ok", "suffix": _REGION_DOMAIN}}))
		self._set_config(self.base)
		# pilot hands the FULL FQDN; the binary peels the wildcard suffix → bare label.
		rc = self.provider.main(["bench-domain-provider", "register", f"app.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 0)
		calls = self._register_calls()
		self.assertEqual(len(calls), 1)
		self.assertIn("label=app", calls[0][1])  # peeled to the bare label

	def test_register_taken_exits_two_aborting_the_create(self) -> None:
		self._set_responses(register=(200, {"message": {"status": "taken"}}))
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "register", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 2)

	def test_register_at_limit_exits_two(self) -> None:
		self._set_responses(register=(200, {"message": {"status": "at_limit"}}))
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "register", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 2)

	def test_register_invalid_exits_two(self) -> None:
		self._set_responses(register=(200, {"message": {"status": "invalid", "reason": "bad label"}}))
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "register", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 2)

	def test_non_wildcard_domain_declines_without_a_register_post(self) -> None:
		# A custom external domain is NOT a Phase-1 wildcard subdomain → decline (exit 2),
		# and we never POST register for it.
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "register", "shop.acme.com"])
		self.assertEqual(rc, 2)
		self.assertEqual(self._register_calls(), [])

	def test_multi_label_under_wildcard_declines_as_invalid(self) -> None:
		# `a.b.<region>` peels to `a.b`, which the controller rejects as invalid → exit 2.
		self._set_responses(register=(200, {"message": {"status": "invalid", "reason": "no dots"}}))
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "register", f"a.b.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 2)
		# It DID peel and POST `a.b` (the controller is the arbiter of the dot ban).
		self.assertIn("label=a.b", self._register_calls()[0][1])

	def test_register_transport_failure_fails_closed_exit_one(self) -> None:
		# FAIL-CLOSED (the deliberate change from atlas-route): an unreachable controller
		# → exit 1 so pilot ABORTS the create (no orphan site with no route).
		self._set_config("http://[::1]:1")
		rc = self.provider.main(["bench-domain-provider", "register", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 1)

	def test_register_no_config_is_a_noop_zero(self) -> None:
		self._set_config(None)
		rc = self.provider.main(["bench-domain-provider", "register", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 0)

	def test_unknown_wire_status_is_a_decline(self) -> None:
		# A status the binary doesn't recognise is treated as a decline (exit 2), the
		# conservative read — register never silently passes a non-`ok` answer.
		self._set_responses(register=(200, {"message": {"status": "teapot"}}))
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "register", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 2)


class TestDeregister(_ProviderTestCase):
	def test_deregister_peels_and_exits_zero(self) -> None:
		self._set_responses(deregister=(200, {"message": {"status": "ok"}}))
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "deregister", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 0)
		self.assertIn("label=acme", self._deregister_calls()[0][1])

	def test_deregister_unreachable_still_exits_zero(self) -> None:
		# best-effort: a non-zero would throw on an otherwise-successful drop.
		self._set_config("http://[::1]:1")
		rc = self.provider.main(["bench-domain-provider", "deregister", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 0)

	def test_deregister_no_config_exits_zero(self) -> None:
		self._set_config(None)
		rc = self.provider.main(["bench-domain-provider", "deregister", f"acme.{_REGION_DOMAIN}"])
		self.assertEqual(rc, 0)

	def test_deregister_non_wildcard_domain_exits_zero_without_post(self) -> None:
		self._set_config(self.base)
		rc = self.provider.main(["bench-domain-provider", "deregister", "shop.acme.com"])
		self.assertEqual(rc, 0)
		self.assertEqual(self._deregister_calls(), [])


class TestGenerateDnsRecords(_ProviderTestCase):
	def test_wildcard_subdomain_needs_no_records(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_config(self.base)
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(
				[
					"bench-domain-provider",
					"generate-dns-records",
					f"app.{_REGION_DOMAIN}",
					f"app.{_REGION_DOMAIN}",
				]
			)
		self.assertEqual(rc, 0)
		# Blank `{}` — a wildcard subdomain we already route needs no user DNS records.
		self.assertEqual(json.loads(out.getvalue().strip()), {})

	def test_non_wildcard_domain_declines(self) -> None:
		# Custom-domain record recipes are Phase 2; a non-wildcard name declines for now.
		self._set_config(self.base)
		rc = self.provider.main(
			["bench-domain-provider", "generate-dns-records", "shop.acme.com", "shop.acme.com"]
		)
		self.assertEqual(rc, 2)

	def test_no_config_fails_open_with_empty_records(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_config(None)
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(
				["bench-domain-provider", "generate-dns-records", "mysite", f"app.{_REGION_DOMAIN}"]
			)
		self.assertEqual(rc, 0)
		self.assertEqual(json.loads(out.getvalue().strip()), {})


class TestWildcardDomains(_ProviderTestCase):
	def test_prints_the_region_wildcard_list(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_config(self.base)
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(["bench-domain-provider", "wildcard-domains"])
		self.assertEqual(rc, 0)
		self.assertEqual(json.loads(out.getvalue().strip()), [_WILDCARD])

	def test_fail_soft_blank_on_outage(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_config("http://[::1]:1")
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(["bench-domain-provider", "wildcard-domains"])
		# Fail-soft: BLANK stdout + exit 0 (pilot reads blank as []; a non-zero would
		# break pilot's Add-Domain UI).
		self.assertEqual(rc, 0)
		self.assertEqual(out.getvalue().strip(), "")

	def test_no_config_fail_soft_blank(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_config(None)
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(["bench-domain-provider", "wildcard-domains"])
		self.assertEqual(rc, 0)
		self.assertEqual(out.getvalue().strip(), "")


class TestProxyServers(_ProviderTestCase):
	def test_prints_the_proxy_ip_list(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_responses(proxy_servers=(200, {"message": {"ips": ["203.0.113.10", "2001:db8::9"]}}))
		self._set_config(self.base)
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(["bench-domain-provider", "proxy-servers"])
		self.assertEqual(rc, 0)
		self.assertEqual(json.loads(out.getvalue().strip()), ["203.0.113.10", "2001:db8::9"])

	def test_fail_soft_blank_on_outage(self) -> None:
		import io
		from contextlib import redirect_stdout

		self._set_config("http://[::1]:1")
		out = io.StringIO()
		with redirect_stdout(out):
			rc = self.provider.main(["bench-domain-provider", "proxy-servers"])
		# Fail-soft: BLANK stdout + exit 0 (pilot reads blank as []).
		self.assertEqual(rc, 0)
		self.assertEqual(out.getvalue().strip(), "")


class TestTransport(_ProviderTestCase):
	def test_unreachable_controller_fails_closed_on_register(self) -> None:
		# Point at a closed port on ::1 (v6 route exists, nothing listening).
		self._set_config("http://[::1]:1")
		self.assertEqual(self.provider.main(["bench-domain-provider", "register", f"a.{_REGION_DOMAIN}"]), 1)

	def test_non_2xx_fails_closed_on_register(self) -> None:
		self._set_responses(register=(500, {"message": "boom"}))
		self._set_config(self.base)
		self.assertEqual(self.provider.main(["bench-domain-provider", "register", f"a.{_REGION_DOMAIN}"]), 1)

	def test_ipv4_only_host_fails_closed_no_fallback(self) -> None:
		# 127.0.0.1 has NO AAAA / no v6 route — the AF_INET6-only connector must raise
		# TransportError (→ register fail-closed exit 1), never silently fall back to IPv4.
		self._set_config(f"http://127.0.0.1:{self.port}")
		self.assertEqual(self.provider.main(["bench-domain-provider", "register", f"a.{_REGION_DOMAIN}"]), 1)

	def test_connector_resolves_only_inet6(self) -> None:
		# The connector asks getaddrinfo for AF_INET6 only — assert that directly so a
		# refactor that drops the family is caught.
		source = _PROVIDER.read_text()
		self.assertIn("socket.AF_INET6", source)
		self.assertNotIn("socket.AF_INET,", source)  # never the v4 family


class TestUsage(_ProviderTestCase):
	def test_usage_error(self) -> None:
		self.assertEqual(self.provider.main(["bench-domain-provider", "bogus"]), 64)

	def test_register_wrong_arg_count_is_usage_error(self) -> None:
		self.assertEqual(self.provider.main(["bench-domain-provider", "register"]), 64)


class TestBuildInstallsProvider(unittest.TestCase):
	def test_build_sh_installs_the_provider_binary(self) -> None:
		source = (_BENCH_DIR / "build.sh").read_text()
		self.assertIn("bench-domain-provider.py", source)
		self.assertIn("/usr/local/bin/bench-domain-provider", source)

	def test_provider_is_stdlib_only(self) -> None:
		source = _PROVIDER.read_text()
		self.assertNotIn("import frappe", source)
		self.assertNotIn("from atlas", source)


if __name__ == "__main__":
	unittest.main()
