import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import bench_image, image_builder
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.image_recipes import get_recipe

_BENCH = get_recipe("bench")


def _purge() -> None:
	# Tasks are append-only audit rows (not purged); every assertion filters by
	# the per-test VM name (a fresh UUID), so stale Tasks never match. Same
	# discipline as test_proxy._purge.
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


@contextlib.contextmanager
def _mock_build_ssh(build_result):
	"""Patch the guest-SSH plumbing the shared run_build seam uses. Yields
	(run_ssh, run_scp, run_detached, forget_host).

	build_bench is now a thin wrapper over image_builder.run_build, so the plumbing
	to patch lives in `image_builder` (its setsid+nohup + marker-poll mechanics are
	unit-tested in test_ssh_transport). `run_detached` returns `build_result`
	directly so this suite covers the seam's own logic (upload mapping, Task record,
	fail-loud) without re-simulating the poll loop. `run_ssh` handles the short
	mkdir; `run_scp` the uploads."""
	run_ssh = MagicMock(return_value=("", "", 0))
	run_scp = MagicMock(return_value=None)
	run_detached = MagicMock(return_value=build_result)
	forget_host = MagicMock(return_value=None)
	key_cm = MagicMock()
	key_cm.__enter__ = MagicMock(return_value="/tmp/fake.key")
	key_cm.__exit__ = MagicMock(return_value=False)
	with (
		patch.object(image_builder, "run_ssh", run_ssh),
		patch.object(image_builder, "run_scp", run_scp),
		patch.object(image_builder, "run_detached", run_detached),
		patch.object(image_builder, "forget_host", forget_host),
		patch.object(image_builder, "ssh_key_file", return_value=key_cm),
		patch.object(
			image_builder,
			"connection_for_guest",
			return_value=MagicMock(ssh_private_key="KEY", host="2400::dead"),
		),
	):
		yield run_ssh, run_scp, run_detached, forget_host


class TestBenchTreeUploads(IntegrationTestCase):
	"""The file enumeration is pure (reads the repo's committed bench/ tree), so
	it's unit-coverable in milliseconds with no host."""

	def test_includes_build_script_and_bench_toml(self) -> None:
		uploads = image_builder.tree_uploads(_BENCH)
		remotes = [remote for _, remote in uploads]
		self.assertTrue(any(r.endswith("/build.sh") for r in remotes), remotes)
		self.assertTrue(any(r.endswith("/bench.toml") for r in remotes), remotes)
		# No caches leak into the upload set.
		self.assertFalse(any("__pycache__" in r for r in remotes), remotes)

	def test_remotes_are_under_one_staging_dir_with_build_at_root(self) -> None:
		uploads = image_builder.tree_uploads(_BENCH)
		for _, remote in uploads:
			self.assertTrue(remote.startswith(_BENCH.remote_directory + "/"), remote)
		# build.sh sits at the staging root so it finds its sibling bench.toml.
		build = next(r for _, r in uploads if r.endswith("/build.sh"))
		self.assertEqual(build, _BENCH.remote_entrypoint)


class TestBuildBench(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_uploads_tree_then_runs_build(self) -> None:
		vm = _new_vm()
		with _mock_build_ssh(("baked", "", 0)) as (run_ssh, run_scp, run_detached, _forget_host):
			bench_image.build_bench(vm.name)
		# Every committed bench/ file was scp'd up.
		self.assertEqual(run_scp.call_count, len(image_builder.tree_uploads(_BENCH)))
		self.assertIn("mkdir -p", run_ssh.call_args_list[0].args[2])
		# The build runs through run_detached (survives a dropped SSH) — not a plain
		# foreground build.sh whose life is tied to the connection. The command it
		# hands off runs build.sh, with the recipe's own log/done marker paths.
		run_detached.assert_called_once()
		self.assertIn("build.sh", run_detached.call_args.args[2])
		self.assertEqual(run_detached.call_args.kwargs["log_path"], _BENCH.build_log_path)
		self.assertEqual(run_detached.call_args.kwargs["done_path"], _BENCH.build_done_path)

	def test_forgets_recycled_host_key_before_uploading(self) -> None:
		# build_bench reaches a fresh VM via run_scp directly (no wait_for_ssh in this
		# path), so it must drop any stale pinned key for the address first or the
		# first scp hard-fails on a recycled IP (real-provision-traps #1).
		vm = _new_vm()
		with _mock_build_ssh(("baked", "", 0)) as (_ssh, _scp, _det, forget_host):
			bench_image.build_bench(vm.name)
		forget_host.assert_called_once_with("2400::dead")

	def test_records_a_task_row(self) -> None:
		vm = _new_vm()
		with _mock_build_ssh(("baked", "", 0)):
			bench_image.build_bench(vm.name)
		status = frappe.get_all(
			"Task", filters={"virtual_machine": vm.name, "script": "bench-build"}, pluck="status"
		)
		self.assertEqual(status, ["Success"])

	def test_build_failure_raises_and_records_failure(self) -> None:
		vm = _new_vm()
		# run_detached reports a non-zero exit → build_bench throws.
		with _mock_build_ssh(("bench init: error", "", 1)):
			with self.assertRaises(frappe.ValidationError):
				bench_image.build_bench(vm.name)
		status = frappe.get_all(
			"Task", filters={"virtual_machine": vm.name, "script": "bench-build"}, pluck="status"
		)
		self.assertEqual(status, ["Failure"])


# A clean site-mode guest run: serves pong, a `bench browse`-minted session
# authenticates as Administrator (200 + resolves to Administrator, not Guest),
# and a garbage session resolves to Guest. The exact labelled shape
# sanity_check parses back out.
_SANE_SITE_STDOUT = (
	"=== SERVE ===\n"
	"http_code=200\n"
	'body={"message":"pong"}\n'
	"=== LOGIN ===\n"
	"http_code=200\n"
	'body="Administrator"\n'
	"user=1\n"
	"=== NEGCTL ===\n"
	"http_code=200\n"
	"user=0\n"
)

# A clean ADMIN-mode run: /api/status serves 200, and GET / renders the Pilot admin
# console page (marker present). No login fields (admin bakes no Frappe site).
_SANE_ADMIN_STDOUT = (
	"=== SERVE ===\n"
	"http_code=200\n"
	'body={"authenticated":false,"enabled":true,"name":"atlas"}\n'
	"=== ADMINUI ===\n"
	"http_code=200\n"
	"marker=1\n"
)


class TestSanityFailureLogic(IntegrationTestCase):
	"""The serve/login/negative-control verdict is pure string logic — unit-coverable
	with no host. These are the cases the gate exists to catch."""

	def test_clean_site_run_has_no_failures(self) -> None:
		parsed = bench_image._parse_sanity(_SANE_SITE_STDOUT, "site")
		self.assertEqual(bench_image._sanity_failures(parsed, "site"), [])

	def test_serves_but_browse_session_rejected(self) -> None:
		# The exact gap the unauthenticated ping gate misses: site serves, the
		# bench browse session does not authenticate (resolves to Guest, not
		# Administrator).
		out = _SANE_SITE_STDOUT.replace(
			'http_code=200\nbody="Administrator"\nuser=1',
			'http_code=200\nbody="Guest"\nuser=0',
		)
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "site"), "site")
		self.assertEqual(len(failures), 1)
		self.assertIn("did not authenticate", failures[0])

	def test_open_door_login_is_untrustworthy(self) -> None:
		# Login 200 but a GARBAGE session is ALSO accepted → the login pass is meaningless.
		out = _SANE_SITE_STDOUT.replace(
			"=== NEGCTL ===\nhttp_code=200\nuser=0", "=== NEGCTL ===\nhttp_code=200\nuser=1"
		)
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "site"), "site")
		self.assertEqual(len(failures), 1)
		self.assertIn("NOT rejected", failures[0])

	def test_does_not_serve(self) -> None:
		out = "=== SERVE ===\nhttp_code=502\nbody=\n"
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "site"), "site")
		self.assertTrue(any("does not serve" in f for f in failures), failures)

	def test_serves_200_without_pong_fails(self) -> None:
		# A 200 from a wrong/default vhost that doesn't carry pong is not "serving this site".
		out = "=== SERVE ===\nhttp_code=200\nbody=<html>default</html>\n"
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "site"), "site")
		self.assertTrue(any("no pong" in f for f in failures), failures)

	def test_clean_admin_run_has_no_failures(self) -> None:
		# Admin bakes no Frappe site → serve (/api/status) + console-render check, no
		# login fields parsed.
		parsed = bench_image._parse_sanity(_SANE_ADMIN_STDOUT, "admin")
		self.assertEqual(bench_image._sanity_failures(parsed, "admin"), [])
		self.assertNotIn("login_http", parsed)

	def test_admin_console_not_rendering_fails(self) -> None:
		# /api/status is 200 but GET / 500s (broken console) — the gap a serve-only
		# admin check would miss.
		out = _SANE_ADMIN_STDOUT.replace("=== ADMINUI ===\nhttp_code=200", "=== ADMINUI ===\nhttp_code=500")
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "admin"), "admin")
		self.assertEqual(len(failures), 1)
		self.assertIn("does not render", failures[0])

	def test_admin_console_200_without_marker_fails(self) -> None:
		# GET / 200s but the page isn't the Pilot admin console (blank/wrong shell).
		out = _SANE_ADMIN_STDOUT.replace(
			"=== ADMINUI ===\nhttp_code=200\nmarker=1", "=== ADMINUI ===\nhttp_code=200\nmarker=0"
		)
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "admin"), "admin")
		self.assertEqual(len(failures), 1)
		self.assertIn("not the Pilot admin console", failures[0])

	def test_admin_not_serving_fails(self) -> None:
		# /api/status itself down — serve failure short-circuits before the UI check.
		out = "=== SERVE ===\nhttp_code=502\nbody=\n=== ADMINUI ===\nhttp_code=200\nmarker=1\n"
		failures = bench_image._sanity_failures(bench_image._parse_sanity(out, "admin"), "admin")
		self.assertTrue(any("does not serve" in f for f in failures), failures)


@contextlib.contextmanager
def _mock_sanity_ssh(stdout: str, exit_code: int = 0):
	"""Patch the guest-SSH plumbing sanity_check uses so no host is touched."""
	key_cm = MagicMock()
	key_cm.__enter__ = MagicMock(return_value="/tmp/fake.key")
	key_cm.__exit__ = MagicMock(return_value=False)
	run_ssh = MagicMock(return_value=(stdout, "", exit_code))
	with (
		patch.object(bench_image, "run_ssh", run_ssh),
		patch.object(bench_image, "ssh_key_file", return_value=key_cm),
		patch.object(
			bench_image,
			"connection_for_guest",
			return_value=MagicMock(ssh_private_key="KEY", host="2400::dead"),
		),
	):
		yield run_ssh


class TestSanityCheck(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_passing_site_build_returns_parsed_result(self) -> None:
		vm = _new_vm(build_mode="site")
		with _mock_sanity_ssh(_SANE_SITE_STDOUT) as run_ssh:
			result = bench_image.sanity_check(vm.name)
		self.assertEqual(result["serve_http"], "200")
		self.assertEqual(result["login_http"], "200")
		# Site mode mints a session via `bench browse` — never a known password.
		# There is no `--sid` flag on stock Frappe's `browse`; the sid is parsed out
		# of the printed `Login URL: <url>?sid=<sid>` instead (grep -oP 'sid=\K\S+').
		remote = run_ssh.call_args.args[2]
		self.assertIn("browse", remote)
		self.assertNotIn("--sid", remote)
		self.assertIn(r"grep -oP 'sid=\K\S+'", remote)
		self.assertIn("/api/method/frappe.auth.get_logged_user", remote)
		self.assertIn("/api/method/ping", remote)

	def test_failing_login_raises_and_names_the_problem(self) -> None:
		vm = _new_vm(build_mode="site")
		out = _SANE_SITE_STDOUT.replace(
			'http_code=200\nbody="Administrator"\nuser=1', "http_code=401\nbody=denied\nuser=0"
		)
		with _mock_sanity_ssh(out):
			with self.assertRaisesRegex(frappe.ValidationError, "did not authenticate"):
				bench_image.sanity_check(vm.name)

	def test_unreachable_guest_raises(self) -> None:
		vm = _new_vm(build_mode="site")
		with _mock_sanity_ssh("", exit_code=255):
			with self.assertRaisesRegex(frappe.ValidationError, "could not reach"):
				bench_image.sanity_check(vm.name)

	def test_admin_mode_probes_status_and_console_not_login(self) -> None:
		vm = _new_vm(build_mode="admin")
		with _mock_sanity_ssh(_SANE_ADMIN_STDOUT) as run_ssh:
			result = bench_image.sanity_check(vm.name)
		self.assertEqual(result["mode"], "admin")
		self.assertEqual(result["adminui_http"], "200")
		remote = run_ssh.call_args.args[2]
		# Admin probes /api/status + renders the console at /, never the login endpoint.
		self.assertIn("/api/status", remote)
		self.assertIn("<title>Pilot</title>", remote)
		self.assertNotIn("/api/method/login", remote)

	def test_admin_console_render_failure_raises(self) -> None:
		# The whole point: an admin build whose console doesn't render fails the gate
		# (so image_build.run marks it Failed and never snapshots).
		vm = _new_vm(build_mode="admin")
		out = _SANE_ADMIN_STDOUT.replace("=== ADMINUI ===\nhttp_code=200", "=== ADMINUI ===\nhttp_code=500")
		with _mock_sanity_ssh(out):
			with self.assertRaisesRegex(frappe.ValidationError, "does not render"):
				bench_image.sanity_check(vm.name)
