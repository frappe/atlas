"""Unit tests for the per-site deploy control plane (spec/14-self-serve.md).

Two seams, both pure-once-mocked:

- `wait_for_http` — the readiness gate (Contract B). Its timeout/poll loop and
  the 200-only predicate are asserted by mocking the single-probe `_http_ok`; no
  real socket, milliseconds.
- `deploy_site` — the guest-SSH driver. The upload + run + Task-record + fail-loud
  path is asserted by mocking the SSH transport (`run_ssh`/`run_scp`) and the VM
  lookup; no real guest.

The host fact — a real rename + `bench setup nginx` actually serving the FQDN on
:80 — is proven in the e2e (spec/14-self-serve.md), not here."""

from __future__ import annotations

import importlib.util
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import deploy_site as deploy_module


def _load_guest_script():
	"""Import the in-guest `bench/deploy-site.py` by path (its hyphen + location
	outside the package make a normal import impossible). The script is stdlib-only,
	so importing it here is safe and lets us unit-test its typed I/O without a
	guest. Path mirrors deploy_module._deploy_script_path.

	Registered in sys.modules before exec: `@dataclass` under `from __future__
	import annotations` resolves field annotations via `sys.modules[cls.__module__]`,
	which is None for an unregistered module (Python 3.14 dataclasses crash)."""
	import sys

	module_name = "atlas_deploy_site_guest"
	path = deploy_module._deploy_script_path()
	spec = importlib.util.spec_from_file_location(module_name, str(path))
	module = importlib.util.module_from_spec(spec)
	sys.modules[module_name] = module
	spec.loader.exec_module(module)
	return module


class TestWaitForHttp(IntegrationTestCase):
	"""Contract B: HTTP 200 is the only thing that returns; anything else keeps
	polling until the deadline, then raises."""

	def test_returns_on_first_200(self) -> None:
		with patch.object(deploy_module, "_http_ok", return_value=True) as probe:
			deploy_module.wait_for_http(
				"2001:db8::1", "acme.blr1.frappe.dev", timeout_seconds=5, poll_seconds=0
			)
		probe.assert_called_once_with("2001:db8::1", "acme.blr1.frappe.dev", 80, deploy_module.READINESS_PATH)

	def test_polls_until_200(self) -> None:
		# Not-ready twice, then ready: the loop must keep going, not give up early.
		with patch.object(deploy_module, "_http_ok", side_effect=[False, False, True]) as probe:
			deploy_module.wait_for_http(
				"2001:db8::1", "acme.blr1.frappe.dev", timeout_seconds=5, poll_seconds=0
			)
		self.assertEqual(probe.call_count, 3)

	def test_raises_on_timeout(self) -> None:
		# Always not-ready: a zero timeout means one probe then raise (the deadline
		# is already passed when the loop checks it).
		with patch.object(deploy_module, "_http_ok", return_value=False):
			with self.assertRaises(frappe.ValidationError) as raised:
				deploy_module.wait_for_http(
					"2001:db8::1", "acme.blr1.frappe.dev", timeout_seconds=0, poll_seconds=0
				)
		message = str(raised.exception)
		self.assertIn("acme.blr1.frappe.dev", message)
		self.assertIn("not seen", message)

	def test_probe_targets_v6_host_and_fqdn_header(self) -> None:
		"""The probe must dial the bracketed-free v6 literal and send the FQDN as
		the Host header (Contract A) so multitenant nginx routes to THIS site."""
		captured = {}

		class _Resp:
			status = 200

		class _Conn:
			def __init__(self, host, port, timeout):
				captured["host"] = host
				captured["port"] = port

			def request(self, method, path, headers):
				captured["path"] = path
				captured["headers"] = headers

			def getresponse(self):
				return _Resp()

			def close(self):
				pass

		with patch.object(deploy_module.http.client, "HTTPConnection", _Conn):
			ok = deploy_module._http_ok("2001:db8::1", "acme.blr1.frappe.dev", 80, "/api/method/ping")
		self.assertTrue(ok)
		self.assertEqual(captured["host"], "2001:db8::1")
		self.assertEqual(captured["port"], 80)
		self.assertEqual(captured["headers"]["Host"], "acme.blr1.frappe.dev")
		self.assertEqual(captured["path"], "/api/method/ping")

	def test_probe_swallows_connection_error(self) -> None:
		"""A pre-serving guest (connection refused) is 'not ready', not an error —
		_http_ok returns False so the poll loop continues."""

		def _boom(*a, **k):
			raise OSError("connection refused")

		with patch.object(deploy_module.http.client, "HTTPConnection", _boom):
			self.assertFalse(deploy_module._http_ok("2001:db8::1", "acme.blr1.frappe.dev", 80, "/"))


class TestDeploySite(IntegrationTestCase):
	"""The guest-SSH driver: upload the script, run it, record a Task, return the
	generated admin password — or fail loud on a non-zero exit."""

	def _make_backing_vm(self) -> str:
		from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine

		provider = make_provider("deploy-test-provider")
		server = make_server(
			provider,
			"deploy-test-server",
			ipv6_address="2001:db8:7::1",
			ipv6_prefix="2001:db8:7::/64",
			ipv6_virtual_machine_range="2001:db8:7::/124",
		)
		image = make_image("deploy-test-image")
		vm = make_virtual_machine(server, image, title="deploy-backing")
		vm.db_set("ipv6_address", "2001:db8:7::abcd")
		return vm.name

	def test_uploads_and_runs_with_fqdn_no_password(self) -> None:
		vm_name = self._make_backing_vm()
		# run_ssh: (stdout, stderr, exit_code). The script's own ATLAS_RESULT line is
		# on stdout; deploy_site doesn't parse it (the rename model returns nothing),
		# but a realistic stdout is recorded on the Task.
		with (
			patch.object(deploy_module, "run_ssh", return_value=("ATLAS_RESULT={}", "", 0)) as m_ssh,
			patch.object(deploy_module, "run_scp") as m_scp,
			patch.object(deploy_module, "wait_for_ssh") as m_wait,
		):
			result = deploy_module.deploy_site(vm_name, "acme.blr1.frappe.dev")
		# The deploy gates on sshd answering before the first scp (clone boot-storm guard).
		m_wait.assert_called_once()
		# The rename model returns nothing — the owner gets the baked password, stored
		# by the Site controller, not a value the guest hands back.
		self.assertIsNone(result)
		# The script was scp'd to the guest, then run.
		m_scp.assert_called_once()
		self.assertIn(deploy_module.DEPLOY_SCRIPT_NAME, m_scp.call_args.args[3])
		# The run command carried the FQDN as a flag — and NO admin password (the
		# per-VM reset is gone).
		run_command = m_ssh.call_args_list[-1].args[2]
		self.assertIn("--site-name", run_command)
		self.assertIn("acme.blr1.frappe.dev", run_command)
		self.assertNotIn("--admin-password", run_command)
		# A deploy-site Task row was recorded for the audit trail.
		self.assertTrue(
			frappe.db.exists(
				"Task", {"virtual_machine": vm_name, "script": "deploy-site", "status": "Success"}
			)
		)

	def test_fails_loud_on_nonzero_exit(self) -> None:
		vm_name = self._make_backing_vm()
		with (
			patch.object(deploy_module, "run_ssh", return_value=("", "bench new-site exploded", 1)),
			patch.object(deploy_module, "run_scp"),
			patch.object(deploy_module, "wait_for_ssh"),
		):
			with self.assertRaises(frappe.ValidationError) as raised:
				deploy_module.deploy_site(vm_name, "acme.blr1.frappe.dev")
		self.assertIn("failed", str(raised.exception))
		# The failure is still recorded as a Task (Failure) for the operator.
		self.assertTrue(
			frappe.db.exists(
				"Task", {"virtual_machine": vm_name, "script": "deploy-site", "status": "Failure"}
			)
		)


class TestGuestScriptTypedIO(IntegrationTestCase):
	"""The in-guest deploy-site.py's typed I/O + the RENAME deploy flow: kebab-flag
	parsing in, one ATLAS_RESULT line out, the rename of the baked site to the FQDN,
	the warm/cold branch, and the nginx v6-listener edit. Stdlib-only, so it imports
	and runs in-process — no guest."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.guest = _load_guest_script()

	def test_from_args_parses_site_name(self) -> None:
		inputs = self.guest.DeploySiteInputs.from_args(["--site-name", "acme.blr1.frappe.dev"])
		self.assertEqual(inputs.site_name, "acme.blr1.frappe.dev")
		self.assertEqual(inputs.warm_vm_uuid, "")  # default, optional

	def test_from_args_requires_site_name(self) -> None:
		# argparse exits(2) on the missing required flag — the CLI form of a required
		# input. SystemExit, not a clean error, is the contract. There is no longer an
		# --admin-password flag, so --site-name alone must succeed (covered above) and
		# its absence must fail.
		with self.assertRaises(SystemExit):
			self.guest.DeploySiteInputs.from_args([])

	def test_result_emits_single_marker_line(self) -> None:
		import io
		from contextlib import redirect_stdout

		buffer = io.StringIO()
		with redirect_stdout(buffer):
			self.guest.DeploySiteResult(site="acme.blr1.frappe.dev", serving=True).emit()
		lines = [line for line in buffer.getvalue().splitlines() if line]
		self.assertEqual(len(lines), 1)
		self.assertTrue(lines[0].startswith(self.guest.RESULT_MARKER))
		import json

		payload = json.loads(lines[0][len(self.guest.RESULT_MARKER) :])
		self.assertEqual(payload, {"site": "acme.blr1.frappe.dev", "serving": True})

	def test_baked_site_constant_matches_build_sh(self) -> None:
		"""The baked-site name the deploy renames must stay in lockstep with the name
		build.sh bakes (BAKED_SITE). A drift would make `_rename_site_to_fqdn`'s
		'baked site missing' guard fail on a correctly-baked image."""
		self.assertEqual(self.guest.BAKED_SITE, "site.local")
		build_sh = deploy_module._deploy_script_path().parent / "build.sh"
		self.assertIn('BAKED_SITE="site.local"', build_sh.read_text())

	def test_rename_moves_baked_site_to_fqdn(self) -> None:
		"""The per-VM on-disk identity: sites/site.local -> sites/<fqdn>. Returns True
		(it renamed). Point SITES_DIR at a temp tree carrying the baked dir."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			sites = os.path.join(tmp, "sites")
			os.makedirs(os.path.join(sites, self.guest.BAKED_SITE))
			with patch.object(self.guest, "SITES_DIR", sites):
				renamed = self.guest._rename_site_to_fqdn("acme.blr1.frappe.dev")
			self.assertTrue(renamed)
			self.assertFalse(os.path.isdir(os.path.join(sites, self.guest.BAKED_SITE)))
			self.assertTrue(os.path.isdir(os.path.join(sites, "acme.blr1.frappe.dev")))

	def test_rename_is_idempotent_when_already_renamed(self) -> None:
		"""A re-run finds sites/<fqdn> already present (baked dir gone) — returns False
		and does not raise (spec taste #14: retry = re-run)."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			sites = os.path.join(tmp, "sites")
			os.makedirs(os.path.join(sites, "acme.blr1.frappe.dev"))  # already renamed
			with patch.object(self.guest, "SITES_DIR", sites):
				renamed = self.guest._rename_site_to_fqdn("acme.blr1.frappe.dev")
			self.assertFalse(renamed)
			self.assertTrue(os.path.isdir(os.path.join(sites, "acme.blr1.frappe.dev")))

	def test_rename_fails_loud_when_site_absent(self) -> None:
		"""Cloned from a site-less (old) golden snapshot → neither sites/site.local nor
		sites/<fqdn> exists → the clone can never serve, so the rename must exit loud."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			sites = os.path.join(tmp, "sites")
			os.mkdir(sites)  # exists, but no site dir under it
			with patch.object(self.guest, "SITES_DIR", sites):
				with self.assertRaises(SystemExit) as raised:
					self.guest._rename_site_to_fqdn("acme.blr1.frappe.dev")
		self.assertIn("site-less snapshot", str(raised.exception))

	def test_warm_main_renames_and_skips_setup_production(self) -> None:
		"""The warm fast-path contract: a warm clone wakes already serving, so `main`
		gates on the freshen, renames the site, regenerates the vhost — NO setup
		production, NO restart. That absence is the whole latency win."""
		guest = self.guest
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_freshen") as m_freshen,
			patch.object(guest, "_bench") as m_bench,
			patch.object(guest, "_rename_site_to_fqdn", return_value=True) as m_rename,
			patch.object(guest, "_setup_nginx_for_fqdn") as m_nginx,
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(site_name="acme.blr1.frappe.dev", warm_vm_uuid="vm-123"),
			),
		):
			guest.main()
		m_freshen.assert_called_once()
		m_rename.assert_called_once_with("acme.blr1.frappe.dev")
		m_nginx.assert_called_once_with("acme.blr1.frappe.dev")
		# Warm wakes already serving — no `setup production` (the only _bench call the
		# warm path could make; `setup nginx` is mocked out via _setup_nginx_for_fqdn).
		for call in m_bench.call_args_list:
			self.assertNotEqual(call.args[:2], ("setup", "production"))

	def test_cold_main_runs_setup_production_then_renames(self) -> None:
		"""The cold path (a freshly image-provisioned VM whose bench was never brought
		up) runs the full `setup production` first, then the same rename + vhost
		regenerate, and does NOT gate on the warm-only freshen."""
		guest = self.guest
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_freshen") as m_freshen,
			patch.object(guest, "_bench") as m_bench,
			patch.object(guest, "_rename_site_to_fqdn", return_value=True) as m_rename,
			patch.object(guest, "_setup_nginx_for_fqdn") as m_nginx,
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(site_name="acme.blr1.frappe.dev", warm_vm_uuid=""),
			),
		):
			guest.main()
		m_bench.assert_called_once_with("setup", "production")
		m_rename.assert_called_once_with("acme.blr1.frappe.dev")
		m_nginx.assert_called_once_with("acme.blr1.frappe.dev")
		m_freshen.assert_not_called()

	def test_setup_nginx_removes_stale_conf_and_regenerates(self) -> None:
		"""`bench setup nginx` only writes current sites' confs, never deletes stale
		ones — so the baked `site.local.conf` must be removed before regenerating, or
		its `server_name site.local` block lingers beside the new `<fqdn>` one. The
		regenerate is driven through `bench setup nginx`, then the v6 listener."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			nginx_sites = os.path.join(tmp, "sites")
			os.mkdir(nginx_sites)
			stale = os.path.join(nginx_sites, f"{self.guest.BAKED_SITE}.conf")
			open(stale, "w").close()
			with (
				patch.object(self.guest, "NGINX_SITES_DIR", nginx_sites),
				patch.object(self.guest, "_bench") as m_bench,
				patch.object(self.guest, "_enable_ipv6_listeners") as m_v6,
			):
				self.guest._setup_nginx_for_fqdn("acme.blr1.frappe.dev")
			self.assertFalse(os.path.exists(stale))  # stale baked conf removed
			m_bench.assert_called_once_with("setup", "nginx")
			m_v6.assert_called_once()

	def test_add_ipv6_listen_adds_v6_without_default_server(self) -> None:
		"""The nginx vhost edit: a `listen [::]:80;` is added beside each `listen 80;`
		— v6 (the only inbound path). NO `default_server` is added: the rename gives
		the vhost a real `server_name <fqdn>` that matches the proxy's Host. Idempotent:
		a conf already carrying the v6 listener is untouched."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			conf = os.path.join(tmp, "acme.blr1.frappe.dev.conf")
			with open(conf, "w") as f:
				f.write("server {\n    listen 80;\n    server_name acme.blr1.frappe.dev;\n}\n")
			self.guest._add_ipv6_listen(conf)
			text = open(conf).read()
			self.assertIn("listen [::]:80;", text)
			self.assertIn("listen 80;", text)  # the original v4 listen is kept as-is
			self.assertNotIn("default_server", text)  # no catch-all needed
			# Idempotent: a second pass (v6 listener present) is a no-op.
			before = text
			self.guest._add_ipv6_listen(conf)
			self.assertEqual(open(conf).read(), before)
