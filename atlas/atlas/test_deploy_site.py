"""Unit tests for the per-site deploy control plane (spec/14-self-serve.md).

Two seams, both pure-once-mocked:

- `wait_for_http` — the readiness gate (Contract B). Its timeout/poll loop and
  the 200-only predicate are asserted by mocking the single-probe `_http_ok`; no
  real socket, milliseconds.
- `deploy_site` — the guest-SSH driver. The upload + run + Task-record + fail-loud
  path is asserted by mocking the SSH transport (`run_ssh`/`run_scp`) and the VM
  lookup; no real guest.

The host fact — a real `bench rename-site` actually serving the FQDN on :80 — is
proven in the e2e (spec/14-self-serve.md), not here."""

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
		# run_ssh: (stdout, stderr, exit_code). The guest's own ATLAS_RESULT line
		# carries the minted login_url; deploy_site parses the LAST such line.
		login_url = "https://acme.blr1.frappe.dev/app?sid=abc123"
		stdout = (
			f'noise\nATLAS_RESULT={{"site": "acme.blr1.frappe.dev", "serving": true, '
			f'"login_url": "{login_url}"}}'
		)
		with (
			patch.object(deploy_module, "run_ssh", return_value=(stdout, "", 0)) as m_ssh,
			patch.object(deploy_module, "run_scp") as m_scp,
			patch.object(deploy_module, "wait_for_ssh") as m_wait,
		):
			result = deploy_module.deploy_site(vm_name, "acme.blr1.frappe.dev")
		# The deploy gates on sshd answering before the first scp (clone boot-storm guard).
		m_wait.assert_called_once()
		# The parsed result carries the tenant's one-click login URL — not a password.
		self.assertEqual(result["login_url"], login_url)
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

	def test_site_mode_vm_passes_no_mode_flag(self) -> None:
		# An ordinary (site / no build_mode) clone deploys exactly as before — no
		# `--mode` flag, so the command is byte-identical to the pre-mode path.
		vm_name = self._make_backing_vm()
		with (
			patch.object(deploy_module, "run_ssh", return_value=("ATLAS_RESULT={}", "", 0)) as m_ssh,
			patch.object(deploy_module, "run_scp"),
			patch.object(deploy_module, "wait_for_ssh"),
		):
			deploy_module.deploy_site(vm_name, "acme.blr1.frappe.dev")
		self.assertNotIn("--mode", m_ssh.call_args_list[-1].args[2])

	def test_admin_mode_vm_passes_mode_admin_flag(self) -> None:
		# A clone carrying build_mode=admin gets `--mode admin` so the in-guest script
		# maps the FQDN to the admin console rather than renaming a (non-existent) site.
		vm_name = self._make_backing_vm()
		frappe.db.set_value("Virtual Machine", vm_name, "build_mode", "admin")
		with (
			patch.object(deploy_module, "run_ssh", return_value=("ATLAS_RESULT={}", "", 0)) as m_ssh,
			patch.object(deploy_module, "run_scp"),
			patch.object(deploy_module, "wait_for_ssh"),
		):
			deploy_module.deploy_site(vm_name, "acme.blr1.frappe.dev")
		self.assertIn("--mode admin", m_ssh.call_args_list[-1].args[2])


class TestReadinessPathForMode(IntegrationTestCase):
	"""The readiness/health PATH is mode-aware: a Frappe site answers
	`/api/method/ping`; the admin console (a Flask app) answers `/api/status`."""

	def test_site_and_empty_default_to_ping(self) -> None:
		self.assertEqual(deploy_module.readiness_path_for_mode("site"), "/api/method/ping")
		self.assertEqual(deploy_module.readiness_path_for_mode(""), "/api/method/ping")
		self.assertEqual(deploy_module.readiness_path_for_mode(None), "/api/method/ping")

	def test_admin_uses_status(self) -> None:
		self.assertEqual(deploy_module.readiness_path_for_mode("admin"), "/api/status")

	def test_unknown_mode_falls_back_to_ping(self) -> None:
		self.assertEqual(deploy_module.readiness_path_for_mode("weird"), "/api/method/ping")


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
		self.assertEqual(inputs.mode, "site")  # default mode

	def test_from_args_parses_admin_mode(self) -> None:
		inputs = self.guest.DeploySiteInputs.from_args(
			["--site-name", "admin.blr1.frappe.dev", "--mode", "admin"]
		)
		self.assertEqual(inputs.mode, "admin")

	def test_from_args_rejects_unknown_mode(self) -> None:
		with self.assertRaises(SystemExit):
			self.guest.DeploySiteInputs.from_args(["--site-name", "x", "--mode", "bogus"])

	def test_serving_probes_ping_in_site_mode(self) -> None:
		"""site mode: the local health probe hits Frappe's `/api/method/ping`."""
		seen = []
		with patch.object(
			self.guest, "_local_ping", side_effect=lambda host, ip, path: seen.append(path) or True
		):
			self.assertTrue(self.guest._serving("acme.blr1.frappe.dev", "site"))
		self.assertEqual(set(seen), {"/api/method/ping"})

	def test_serving_probes_status_in_admin_mode(self) -> None:
		"""admin mode: the admin console is a Flask app with no Frappe ping route, so
		the local health probe hits `/api/status` (200 unauthenticated)."""
		seen = []
		with patch.object(
			self.guest, "_local_ping", side_effect=lambda host, ip, path: seen.append(path) or True
		):
			self.assertTrue(self.guest._serving("admin.blr1.frappe.dev", "admin"))
		self.assertEqual(set(seen), {"/api/status"})

	def test_guest_health_paths_match_controller(self) -> None:
		"""The in-guest health-path map stays in lockstep with the controller's
		readiness paths (a drift would make one probe a route the other doesn't)."""
		self.assertEqual(self.guest._HEALTH_PATH["site"], deploy_module.readiness_path_for_mode("site"))
		self.assertEqual(self.guest._HEALTH_PATH["admin"], deploy_module.readiness_path_for_mode("admin"))

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

	def test_result_emits_login_url_when_present(self) -> None:
		import io
		import json
		from contextlib import redirect_stdout

		buffer = io.StringIO()
		login_url = "https://acme.blr1.frappe.dev/app?sid=abc123"
		with redirect_stdout(buffer):
			self.guest.DeploySiteResult(site="acme.blr1.frappe.dev", serving=True, login_url=login_url).emit()
		line = next(line for line in buffer.getvalue().splitlines() if line)
		payload = json.loads(line[len(self.guest.RESULT_MARKER) :])
		self.assertEqual(payload["login_url"], login_url)

	def test_mint_login_url_uses_browse_sid_no_password(self) -> None:
		"""`_mint_login_url` mints via `bench browse` (never a password) and builds
		`https://<fqdn>/app?sid=<sid>` from the sid parsed out of its printed Login
		URL — there is no `--sid` flag on stock Frappe's `browse`."""
		with patch.object(
			self.guest, "_bench", return_value="Login URL: http://acme.blr1.frappe.dev:8000/app?sid=abc123\n"
		) as m_bench:
			url = self.guest._mint_login_url("acme.blr1.frappe.dev")
		self.assertEqual(url, "https://acme.blr1.frappe.dev/app?sid=abc123")
		call_args = m_bench.call_args
		self.assertTrue(call_args.kwargs.get("capture"))
		positional = call_args.args
		self.assertIn("browse", positional)
		self.assertNotIn("--sid", positional)
		self.assertIn("--session-end", positional)
		self.assertNotIn("--password", positional)

	def test_mint_login_url_fails_loud_without_sid(self) -> None:
		"""If `bench browse` ever prints no `sid=` (a broken guest, a future Frappe
		change), fail loud with the raw output rather than minting a garbage URL."""
		with patch.object(
			self.guest, "_bench", return_value="Login URL: http://acme.blr1.frappe.dev:8000/app\n"
		):
			with self.assertRaises(SystemExit):
				self.guest._mint_login_url("acme.blr1.frappe.dev")

	def test_await_db_ready_returns_when_unit_active(self) -> None:
		"""The gate returns as soon as `systemctl is-active mariadb@<bench>` exits 0
		(the Type=notify unit has opened its socket) — and probes the right unit."""
		import subprocess as sp

		with patch.object(self.guest.subprocess, "run", return_value=sp.CompletedProcess([], 0)) as m_run:
			self.guest._await_db_ready()
		m_run.assert_called_once()
		self.assertEqual(
			m_run.call_args.args[0],
			["systemctl", "is-active", "--quiet", f"mariadb@{self.guest.BENCH_NAME}.service"],
		)

	def test_await_db_ready_polls_until_active(self) -> None:
		"""On a snapshot-booted clone MariaDB can still be starting: the gate keeps
		polling (returncode 3 = inactive) until it flips to active, then returns."""
		import subprocess as sp

		codes = [sp.CompletedProcess([], 3), sp.CompletedProcess([], 3), sp.CompletedProcess([], 0)]
		with (
			patch.object(self.guest.subprocess, "run", side_effect=codes) as m_run,
			patch.object(self.guest.time, "sleep") as m_sleep,
		):
			self.guest._await_db_ready()
		self.assertEqual(m_run.call_count, 3)
		self.assertEqual(m_sleep.call_count, 2)

	def test_await_db_ready_fails_loud_on_timeout(self) -> None:
		"""If MariaDB never comes up, exit loud (not a swallowed browse crash later)."""
		import subprocess as sp

		with (
			patch.object(self.guest.subprocess, "run", return_value=sp.CompletedProcess([], 3)),
			patch.object(self.guest.time, "sleep"),
		):
			with self.assertRaises(SystemExit) as raised:
				self.guest._await_db_ready(timeout_seconds=0.01)
		self.assertIn("did not become active", str(raised.exception))

	def test_baked_site_constant_matches_build_sh(self) -> None:
		"""The baked-site name the deploy renames must stay in lockstep with the name
		build.sh bakes (BAKED_SITE). A drift would make `_rename_site_to_fqdn`'s
		'baked site missing' guard fail on a correctly-baked image."""
		self.assertEqual(self.guest.BAKED_SITE, "site.local")
		build_sh = deploy_module._deploy_script_path().parent / "build.sh"
		self.assertIn('BAKED_SITE="site.local"', build_sh.read_text())

	def test_rename_moves_baked_site_to_fqdn(self) -> None:
		"""The per-VM on-disk identity: `bench rename-site site.local <fqdn>`. Returns
		True (it renamed) and drives the rename through bench-cli, not a raw os.rename.
		Point SITES_DIR at a temp tree carrying the baked dir."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			sites = os.path.join(tmp, "sites")
			os.makedirs(os.path.join(sites, self.guest.BAKED_SITE))
			with (
				patch.object(self.guest, "SITES_DIR", sites),
				patch.object(self.guest, "_bench") as m_bench,
			):
				renamed = self.guest._rename_site_to_fqdn("acme.blr1.frappe.dev")
			self.assertTrue(renamed)
			m_bench.assert_called_once_with("rename-site", self.guest.BAKED_SITE, "acme.blr1.frappe.dev")

	def test_rename_is_idempotent_when_already_renamed(self) -> None:
		"""A re-run finds sites/<fqdn> already present (baked dir gone) — returns False
		and does not raise or re-invoke bench-cli (spec taste #14: retry = re-run)."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			sites = os.path.join(tmp, "sites")
			os.makedirs(os.path.join(sites, "acme.blr1.frappe.dev"))  # already renamed
			with (
				patch.object(self.guest, "SITES_DIR", sites),
				patch.object(self.guest, "_bench") as m_bench,
			):
				renamed = self.guest._rename_site_to_fqdn("acme.blr1.frappe.dev")
			self.assertFalse(renamed)
			m_bench.assert_not_called()

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

	def test_warm_main_renames_and_skips_bench_start(self) -> None:
		"""The warm fast-path contract: a warm clone wakes already serving, so `main`
		gates on the freshen and renames the site (`bench rename-site` does nginx +
		production setup itself) — NO `bench start`, NO restart. That absence is the
		latency win. The rename is mocked here, so the deploy path makes no direct
		`_bench` call of its own (login-URL minting is mocked separately)."""
		guest = self.guest
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_freshen") as m_freshen,
			patch.object(guest, "_await_db_ready"),
			patch.object(guest, "_bench") as m_bench,
			patch.object(guest, "_rename_site_to_fqdn", return_value=True) as m_rename,
			patch.object(guest, "_mint_login_url", return_value="https://acme.blr1.frappe.dev/app?sid=x"),
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
		# Warm wakes already serving — no `bench start`. The rename (which carries the
		# nginx + production-setup work) is mocked, so no direct `_bench` call remains.
		m_bench.assert_not_called()

	def test_cold_main_runs_bench_start_then_renames(self) -> None:
		"""The cold path (a snapshot-booted clone) idempotently re-asserts `bench
		start` first, then the same rename, and does NOT gate on the warm-only
		freshen."""
		guest = self.guest
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_freshen") as m_freshen,
			patch.object(guest, "_await_db_ready") as m_db_ready,
			patch.object(guest, "_bench") as m_bench,
			patch.object(guest, "_rename_site_to_fqdn", return_value=True) as m_rename,
			patch.object(guest, "_mint_login_url", return_value="https://acme.blr1.frappe.dev/app?sid=x"),
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(site_name="acme.blr1.frappe.dev", warm_vm_uuid=""),
			),
		):
			guest.main()
		m_bench.assert_called_once_with("start")
		# The DB-readiness gate runs before rename/mint — the whole point is that a
		# snapshot-booted clone's MariaDB can still be starting when the deploy lands.
		m_db_ready.assert_called_once()
		m_rename.assert_called_once_with("acme.blr1.frappe.dev")
		m_freshen.assert_not_called()

	def test_admin_main_sets_admin_domain_no_rename(self) -> None:
		"""Admin mode: no site rename — instead `[admin].domain` is set to the FQDN +
		`bench setup production`, mapping the FQDN to the admin app's vhost — then
		the admin login URL is minted (Pilot #117 `generate-admin-session`)."""
		guest = self.guest
		admin_login_url = "http://admin.blr1.frappe.dev/?sid=jwt-token"
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_db_ready"),
			patch.object(guest, "_bench"),
			patch.object(guest, "_set_admin_domain") as m_admin,
			patch.object(guest, "_mint_admin_login_url", return_value=admin_login_url) as m_mint,
			patch.object(guest, "_rename_site_to_fqdn") as m_rename,
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(
					site_name="acme.blr1.frappe.dev", warm_vm_uuid="", mode="admin"
				),
			),
		):
			guest.main()
		m_admin.assert_called_once_with("acme.blr1.frappe.dev")
		m_rename.assert_not_called()
		# Minting runs AFTER _set_admin_domain, so the printed URL carries the real
		# FQDN, not the placeholder admin.localhost.
		m_mint.assert_called_once_with()

	def test_mint_admin_login_url_uses_generate_admin_session_full_path(self) -> None:
		"""`_mint_admin_login_url` shells out to Pilot #117's
		`bench generate-admin-session --full-path` and returns its bare stdout —
		never touching the (random, bake-time) [admin].password."""
		guest = self.guest
		with patch.object(guest, "_bench", return_value="http://admin.example/?sid=jwt\n") as m_bench:
			url = guest._mint_admin_login_url()
		self.assertEqual(url, "http://admin.example/?sid=jwt")
		m_bench.assert_called_once_with("generate-admin-session", "--full-path", capture=True)

	def test_set_admin_domain_rewrites_toml_and_regenerates(self) -> None:
		"""Admin mode points the admin vhost at the FQDN by rewriting `domain = ""`
		in the committed bench.toml in place, then `bench setup production`."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			toml = os.path.join(tmp, "bench.toml")
			with open(toml, "w") as f:
				f.write('[admin]\nenabled = true\ndomain = ""\n')
			with (
				patch.object(self.guest, "BENCH_TOML", toml),
				patch.object(self.guest, "_bench") as m_bench,
			):
				self.guest._set_admin_domain("acme.blr1.frappe.dev")
			self.assertIn('domain = "acme.blr1.frappe.dev"', open(toml).read())
			m_bench.assert_called_once_with("setup", "production")

	def test_set_admin_domain_skips_setup_when_run_setup_false(self) -> None:
		"""`run_setup=False` writes the toml line but does NOT run production setup — the
		site-mode caller's later `bench rename-site` regenerates nginx in one pass, so a
		second setup here would be a redundant CPU-throttled no-op."""
		import os
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			toml = os.path.join(tmp, "bench.toml")
			with open(toml, "w") as f:
				f.write('[admin]\nenabled = true\ndomain = ""\n')
			with (
				patch.object(self.guest, "BENCH_TOML", toml),
				patch.object(self.guest, "_bench") as m_bench,
			):
				self.guest._set_admin_domain("acme-pilot.blr1.frappe.dev", run_setup=False)
			self.assertIn('domain = "acme-pilot.blr1.frappe.dev"', open(toml).read())
			m_bench.assert_not_called()

	def test_from_args_parses_admin_domain(self) -> None:
		inputs = self.guest.DeploySiteInputs.from_args(
			["--site-name", "acme.blr1.frappe.dev", "--admin-domain", "acme-pilot.blr1.frappe.dev"]
		)
		self.assertEqual(inputs.admin_domain, "acme-pilot.blr1.frappe.dev")
		# Defaults empty when the flag is absent (an ordinary deploy that knows no console FQDN).
		bare = self.guest.DeploySiteInputs.from_args(["--site-name", "acme.blr1.frappe.dev"])
		self.assertEqual(bare.admin_domain, "")

	def test_site_main_sets_admin_domain_before_rename(self) -> None:
		"""site mode with --admin-domain: `[admin].domain` is written (run_setup=False)
		BEFORE the rename, so the rename-site's production setup emits the admin vhost in
		the same pass — the console is reachable at its real FQDN out of this one deploy,
		not left at the baked `admin.localhost`."""
		guest = self.guest
		calls = []
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_db_ready"),
			patch.object(guest, "_bench"),
			patch.object(
				guest, "_set_admin_domain", side_effect=lambda *a, **k: calls.append(("admin", a, k))
			) as m_admin,
			patch.object(
				guest, "_rename_site_to_fqdn", side_effect=lambda *a: calls.append(("rename", a, {})) or True
			),
			patch.object(guest, "_mint_login_url", return_value="https://acme.blr1.frappe.dev/app?sid=x"),
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(
					site_name="acme.blr1.frappe.dev",
					warm_vm_uuid="",
					admin_domain="acme-pilot.blr1.frappe.dev",
				),
			),
		):
			guest.main()
		# The admin domain is the PILOT FQDN, written with run_setup=False, and the write
		# lands strictly before the rename.
		m_admin.assert_called_once_with(
			"acme-pilot.blr1.frappe.dev", run_setup=False, update_site="site.local"
		)
		self.assertEqual([c[0] for c in calls], ["admin", "rename"])

	def test_site_main_skips_admin_domain_when_not_given(self) -> None:
		"""No --admin-domain (an ordinary site deploy that knows no console): the admin
		domain write is skipped entirely — back-compat for every existing site deploy."""
		guest = self.guest
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_db_ready"),
			patch.object(guest, "_bench"),
			patch.object(guest, "_set_admin_domain") as m_admin,
			patch.object(guest, "_rename_site_to_fqdn", return_value=True),
			patch.object(guest, "_mint_login_url", return_value="https://acme.blr1.frappe.dev/app?sid=x"),
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(site_name="acme.blr1.frappe.dev", warm_vm_uuid=""),
			),
		):
			guest.main()
		m_admin.assert_not_called()

	def test_site_main_enrols_after_admin_domain(self) -> None:
		"""Ordering invariant: `bench enroll` runs after the admin domain is in place, so the
		pilot's Central config is written to a bench.toml that already carries the real
		`[admin].domain` — never the `admin.localhost` placeholder."""
		guest = self.guest
		order = []
		with (
			patch.object(guest, "_preflight"),
			patch.object(guest, "_await_db_ready"),
			patch.object(
				guest, "_set_admin_domain", side_effect=lambda *a, **k: order.append("admin-domain")
			),
			patch.object(guest, "_rename_site_to_fqdn", return_value=True),
			patch.object(guest, "_mint_login_url", return_value="https://acme.blr1.frappe.dev/app?sid=x"),
			patch.object(guest, "_serving", return_value=True),
			patch.object(
				guest,
				"_bench",
				side_effect=lambda *a, **k: order.append(a[0]) if a and a[0] == "enroll" else None,
			),
			patch.object(
				guest.DeploySiteInputs,
				"from_args",
				return_value=guest.DeploySiteInputs(
					site_name="acme.blr1.frappe.dev",
					warm_vm_uuid="",
					admin_domain="acme-pilot.blr1.frappe.dev",
					central_endpoint="https://central.example",
					bootstrap_token="tok",
				),
			),
		):
			guest.main()
		self.assertEqual(order, ["admin-domain", "enroll"])
