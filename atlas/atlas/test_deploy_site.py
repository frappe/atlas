"""Unit tests for the per-site deploy control plane (plans/self-serve/03).

Two seams, both pure-once-mocked:

- `wait_for_http` — the readiness gate (Contract B). Its timeout/poll loop and
  the 200-only predicate are asserted by mocking the single-probe `_http_ok`; no
  real socket, milliseconds.
- `deploy_site` — the guest-SSH driver. The upload + run + Task-record + fail-loud
  path is asserted by mocking the SSH transport (`run_ssh`/`run_scp`) and the VM
  lookup; no real guest.

The host fact — a real `bench new-site` + `setup production` actually serving on
:80 — is proven in the e2e (plan 05), not here."""

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

	def test_uploads_runs_and_returns_admin_password(self) -> None:
		vm_name = self._make_backing_vm()
		# run_ssh: (stdout, stderr, exit_code). The script's own ATLAS_RESULT line
		# is on stdout; deploy_site doesn't parse it (it returns the password it
		# generated), but a realistic stdout is recorded on the Task.
		with (
			patch.object(deploy_module, "run_ssh", return_value=("ATLAS_RESULT={}", "", 0)) as m_ssh,
			patch.object(deploy_module, "run_scp") as m_scp,
		):
			password = deploy_module.deploy_site(vm_name, "acme.blr1.frappe.dev")
		self.assertTrue(password)
		# A real generated secret, not the literal flag value or empty.
		self.assertGreaterEqual(len(password), 16)
		# The script was scp'd to the guest, then run.
		m_scp.assert_called_once()
		self.assertIn(deploy_module.DEPLOY_SCRIPT_NAME, m_scp.call_args.args[3])
		# The run command carried the FQDN and the generated password as flags.
		run_command = m_ssh.call_args_list[-1].args[2]
		self.assertIn("--site-name", run_command)
		self.assertIn("acme.blr1.frappe.dev", run_command)
		self.assertIn("--admin-password", run_command)
		self.assertIn(password, run_command)
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
	"""The in-guest deploy-site.py's typed I/O: kebab-flag parsing in, one
	ATLAS_RESULT line out, and the on-disk idempotency predicate. Stdlib-only, so
	it imports and runs in-process — no guest."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.guest = _load_guest_script()

	def test_from_args_parses_kebab_flags(self) -> None:
		inputs = self.guest.DeploySiteInputs.from_args(
			["--site-name", "acme.blr1.frappe.dev", "--admin-password", "s3cret"]
		)
		self.assertEqual(inputs.site_name, "acme.blr1.frappe.dev")
		self.assertEqual(inputs.admin_password, "s3cret")

	def test_from_args_requires_both_flags(self) -> None:
		# argparse exits(2) on a missing required flag — the CLI form of a required
		# input. SystemExit, not a clean error, is the contract.
		with self.assertRaises(SystemExit):
			self.guest.DeploySiteInputs.from_args(["--site-name", "acme.blr1.frappe.dev"])

	def test_result_emits_single_marker_line(self) -> None:
		import io
		from contextlib import redirect_stdout

		buffer = io.StringIO()
		with redirect_stdout(buffer):
			self.guest.DeploySiteResult(site="acme.blr1.frappe.dev", created=True, serving=True).emit()
		lines = [line for line in buffer.getvalue().splitlines() if line]
		self.assertEqual(len(lines), 1)
		self.assertTrue(lines[0].startswith(self.guest.RESULT_MARKER))
		import json

		payload = json.loads(lines[0][len(self.guest.RESULT_MARKER) :])
		self.assertEqual(payload, {"site": "acme.blr1.frappe.dev", "created": True, "serving": True})

	def test_site_exists_is_the_on_disk_predicate(self) -> None:
		# _site_exists checks <bench>/sites/<fqdn>; a non-existent bench dir means
		# the site doesn't exist (the fresh-deploy branch), never a crash.
		self.assertFalse(self.guest._site_exists("does-not-exist.blr1.frappe.dev"))

	def test_baked_site_constant_matches_build_sh(self) -> None:
		"""The baked-site name deploy renames FROM must stay in lockstep with the
		name build.sh bakes (BAKED_SITE). A drift would make every deploy fail
		_rename_site's 'baked site missing' guard on a correctly-baked image."""
		self.assertEqual(self.guest.BAKED_SITE, "site.local")
		build_sh = deploy_module._deploy_script_path().parent / "build.sh"
		self.assertIn('BAKED_SITE="site.local"', build_sh.read_text())

	def test_rename_site_fails_loud_when_baked_site_absent(self) -> None:
		"""Cloned from a site-less (old) golden snapshot → no sites/site.local →
		_rename_site must exit loud, not silently no-op or rename a missing dir.
		Point BAKED_SITE/SITES_DIR at a temp tree with no baked site."""
		import tempfile

		inputs = self.guest.DeploySiteInputs(site_name="acme.blr1.frappe.dev", admin_password="s3cret")
		with tempfile.TemporaryDirectory() as tmp:
			with patch.object(self.guest, "SITES_DIR", tmp):
				with self.assertRaises(SystemExit) as raised:
					self.guest._rename_site(inputs)
		self.assertIn("baked site", str(raised.exception))

	def test_rename_site_moves_baked_dir_and_resets_password(self) -> None:
		"""The happy path: sites/site.local exists → it is renamed to sites/<fqdn>
		(a directory move, not a new-site) and the per-VM admin password is reset.
		bench-cli is mocked (_bench) so no real frappe runs."""
		import os
		import tempfile

		inputs = self.guest.DeploySiteInputs(site_name="acme.blr1.frappe.dev", admin_password="s3cret")
		with tempfile.TemporaryDirectory() as tmp:
			os.mkdir(os.path.join(tmp, self.guest.BAKED_SITE))
			with (
				patch.object(self.guest, "SITES_DIR", tmp),
				patch.object(self.guest, "_bench") as m_bench,
			):
				self.guest._rename_site(inputs)
			# The baked dir is gone; the FQDN dir is in its place (the move).
			self.assertFalse(os.path.isdir(os.path.join(tmp, self.guest.BAKED_SITE)))
			self.assertTrue(os.path.isdir(os.path.join(tmp, inputs.site_name)))
		# The admin password was reset against the renamed site with the per-VM secret.
		args = m_bench.call_args.args
		self.assertEqual(args[0], "frappe")
		self.assertIn("set-admin-password", args)
		self.assertIn(inputs.site_name, args)
		self.assertIn(inputs.admin_password, args)

	def test_set_default_site_repoints_to_fqdn(self) -> None:
		"""The host-only bug fix: bench-cli's `frappe serve` resolves every request to
		`default_site` (it does NOT honor the Host header on a snapshot-booted clone),
		so the baked `default_site = site.local` must be repointed to the renamed FQDN
		or the site 404s "site.local does not exist". `_set_default_site` rewrites the
		key in common_site_config.json (preserving the rest), and is idempotent."""
		import json
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			config_path = os.path.join(tmp, "common_site_config.json")
			with open(config_path, "w") as handle:
				json.dump({"default_site": "site.local", "dns_multitenant": 1}, handle)
			with patch.object(self.guest, "SITES_DIR", tmp):
				self.guest._set_default_site("acme.blr1.frappe.dev")
				# Idempotent — a second call is a no-op write of the same value.
				self.guest._set_default_site("acme.blr1.frappe.dev")
			with open(config_path) as handle:
				config = json.load(handle)
		self.assertEqual(config["default_site"], "acme.blr1.frappe.dev")
		# Other keys are preserved (a targeted rewrite, not a clobber).
		self.assertEqual(config["dns_multitenant"], 1)
