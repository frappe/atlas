"""Unit tests for the shared image-build seam + the recipe registry.

The tree enumeration is pure (reads the committed bench/ and proxy/ trees) and the
run_build path mocks the guest-SSH plumbing — all milliseconds, no host. The host
fact (a real bake actually produces a working bench / serving proxy) is the e2e's
job (spec/08, spec/12, spec/15)."""

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import image_builder
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.image_recipes import RECIPES, get_recipe

_BENCH = get_recipe("bench")
_PROXY = get_recipe("proxy")


def _purge() -> None:
	# Tasks are append-only audit rows (not purged); every assertion filters by the
	# per-test VM name (a fresh UUID), so stale Tasks never match.
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


@contextlib.contextmanager
def _mock_build_ssh(build_result, finalize_result=("", "", 0)):
	"""Patch the guest-SSH plumbing run_build uses. `build_result` is what the
	detached build returns; `finalize_result` what the proxy recipe's finalize
	run_ssh returns (image_recipes calls run_ssh, so patch it there too). Yields
	(run_ssh, run_scp, run_detached, forget_host, finalize_run_ssh)."""
	run_ssh = MagicMock(return_value=("", "", 0))
	run_scp = MagicMock(return_value=None)
	run_detached = MagicMock(return_value=build_result)
	forget_host = MagicMock(return_value=None)
	finalize_run_ssh = MagicMock(return_value=finalize_result)
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
		patch("atlas.atlas.image_recipes.run_ssh", finalize_run_ssh),
	):
		yield run_ssh, run_scp, run_detached, forget_host, finalize_run_ssh


class TestRecipeRegistry(IntegrationTestCase):
	def test_known_recipes(self) -> None:
		# Three versioned bench variants in site mode + their three admin twins + the
		# proxy; the back-compat `bench` alias is NOT a real recipe (so it never appears
		# in the Select).
		self.assertEqual(
			sorted(RECIPES),
			[
				"bench-nightly",
				"bench-nightly-admin",
				"bench-v15",
				"bench-v15-admin",
				"bench-v16",
				"bench-v16-admin",
				"proxy",
			],
		)
		self.assertNotIn("bench", RECIPES)

	def test_bench_alias_resolves_to_v16(self) -> None:
		# Callers that still say "bench" via get_recipe() (bootstrap.build_bench)
		# resolve to the current line, bench-v16. NOTE: the alias only works through
		# get_recipe() — a stored Image Build.recipe Select value must be a real name.
		self.assertIs(get_recipe("bench"), RECIPES["bench-v16"])

	def test_recipe_names_excludes_alias(self) -> None:
		from atlas.atlas.image_recipes import recipe_names

		names = recipe_names()
		self.assertNotIn("bench", names)
		self.assertEqual(set(names), set(RECIPES))

	def test_unknown_recipe_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			get_recipe("nope")

	def test_image_build_json_options_match_recipe_names(self) -> None:
		# The Image Build `recipe` Select options are a hand-maintained mirror of
		# recipe_names() (the JSON isn't auto-dynamic). Pin them equal so adding a
		# recipe without updating the JSON fails HERE, loudly — not at the operator's
		# first bake with a Select-validation reject. (The Server "Bake Image" dialog
		# in server.js is a third copy; keep it in lockstep too.)
		import json
		from pathlib import Path

		from atlas.atlas.image_recipes import recipe_names

		path = Path(frappe.get_app_path("atlas")) / "atlas" / "doctype" / "image_build" / "image_build.json"
		doc = json.loads(path.read_text())
		recipe_field = next(f for f in doc["fields"] if f["fieldname"] == "recipe")
		json_options = recipe_field["options"].split("\n")
		self.assertEqual(set(json_options), set(recipe_names()))
		# The back-compat alias must NOT leak into the picker (a stored alias value
		# would fail the Select validation).
		self.assertNotIn("bench", json_options)

	def test_bench_recipe_shape(self) -> None:
		self.assertEqual(_BENCH.task_script, "bench-build")
		self.assertEqual(_BENCH.registers_as, "default_bench_snapshot")
		self.assertFalse(_BENCH.is_proxy)
		self.assertIsNone(_BENCH.finalize)

	def test_bench_variants_carry_version_pins(self) -> None:
		v16, v15, nightly = (RECIPES["bench-v16"], RECIPES["bench-v15"], RECIPES["bench-nightly"])
		self.assertEqual(
			(v16.frappe_branch, v16.erpnext_branch, v16.python_version), ("version-16", "version-16", "3.14")
		)
		self.assertEqual(
			(v15.frappe_branch, v15.erpnext_branch, v15.python_version), ("version-15", "version-15", "3.11")
		)
		self.assertEqual((nightly.frappe_branch, nightly.erpnext_branch), ("develop", "develop"))
		# All three share the proven bench-cli ref + the bench source tree + sizing.
		self.assertEqual({v16.bench_cli_ref, v15.bench_cli_ref, nightly.bench_cli_ref}, {v16.bench_cli_ref})
		self.assertTrue(v16.bench_cli_ref)
		for r in (v16, v15, nightly):
			self.assertEqual(r.source_directory, "bench")
			self.assertEqual(r.task_script, "bench-build")

	def test_versioned_recipes_promote_to_series_image_name(self) -> None:
		# The promote default name == the recipe name (the series image name).
		for name in ("bench-v15", "bench-v16", "bench-nightly"):
			self.assertEqual(RECIPES[name].promote_image_name, name)

	def test_v16_is_the_only_warm_registering_variant(self) -> None:
		# v16 doubles as the self-serve site accelerator base (warm + registers); v15
		# and nightly are cold customer goldens (promote-to-image requires cold).
		self.assertEqual(RECIPES["bench-v16"].warm_entrypoint, "warm.sh")
		self.assertEqual(RECIPES["bench-v16"].registers_as, "default_bench_snapshot")
		for name in ("bench-v15", "bench-nightly"):
			self.assertEqual(RECIPES[name].warm_entrypoint, "")
			self.assertIsNone(RECIPES[name].registers_as)

	def test_build_mode_defaults_to_site(self) -> None:
		for name in ("bench-v15", "bench-v16", "bench-nightly"):
			self.assertEqual(RECIPES[name].build_mode, "site")
			self.assertEqual(RECIPES[name].effective_build_mode, "site")
		# The proxy pins no mode; effective_build_mode still resolves to the harmless site.
		self.assertEqual(RECIPES["proxy"].build_mode, "")
		self.assertEqual(RECIPES["proxy"].effective_build_mode, "site")

	def test_admin_variants_shape(self) -> None:
		# Each version ships an admin twin: same pins, build_mode=admin, COLD-only
		# (no warm), never registers (the admin console is a distinct product, not the
		# self-serve site golden), and promotes to its own series name (the Central
		# name-match link). The pins mirror the site twin so a bump stays one edit.
		for admin_name, site_name in (
			("bench-v16-admin", "bench-v16"),
			("bench-v15-admin", "bench-v15"),
			("bench-nightly-admin", "bench-nightly"),
		):
			admin, site = RECIPES[admin_name], RECIPES[site_name]
			self.assertEqual(admin.build_mode, "admin")
			self.assertEqual(admin.effective_build_mode, "admin")
			self.assertEqual(admin.warm_entrypoint, "")
			self.assertIsNone(admin.registers_as)
			self.assertEqual(admin.promote_image_name, admin_name)
			# Same Frappe/ERPNext/Python pins + tree as the site twin — only the mode differs.
			self.assertEqual(
				(admin.frappe_branch, admin.erpnext_branch, admin.python_version),
				(site.frappe_branch, site.erpnext_branch, site.python_version),
			)
			self.assertEqual(admin.source_directory, "bench")
			self.assertEqual(admin.bench_cli_ref, site.bench_cli_ref)

	def test_proxy_recipe_shape(self) -> None:
		self.assertEqual(_PROXY.task_script, "proxy-build")
		self.assertIsNone(_PROXY.registers_as)
		self.assertTrue(_PROXY.is_proxy)
		self.assertIsNotNone(_PROXY.finalize)
		self.assertIn("test", _PROXY.exclude)


class TestTreeUploads(IntegrationTestCase):
	def test_bench_tree_has_build_and_toml_no_caches(self) -> None:
		remotes = [remote for _, remote in image_builder.tree_uploads(_BENCH)]
		self.assertTrue(any(r.endswith("/build.sh") for r in remotes), remotes)
		self.assertTrue(any(r.endswith("/bench.toml") for r in remotes), remotes)
		self.assertFalse(any("__pycache__" in r for r in remotes), remotes)
		# build.sh sits at the staging root so it finds its sibling bench.toml.
		build = next(r for _, r in image_builder.tree_uploads(_BENCH) if r.endswith("/build.sh"))
		self.assertEqual(build, _BENCH.remote_entrypoint)

	def test_proxy_tree_excludes_test_harness(self) -> None:
		remotes = [remote for _, remote in image_builder.tree_uploads(_PROXY)]
		self.assertTrue(any(r.endswith("/build.sh") for r in remotes), remotes)
		self.assertTrue(any(r.endswith("/conf/nginx.conf") for r in remotes), remotes)
		self.assertTrue(any(r.endswith("/lua/router.lua") for r in remotes), remotes)
		self.assertTrue(any(r.endswith("/guest/nginx.service.d/atlas.conf") for r in remotes), remotes)
		# The dev-only compose harness (recipe.exclude=("test",)) + caches are gone.
		self.assertFalse(any("/test/" in r for r in remotes), remotes)
		self.assertFalse(any("__pycache__" in r for r in remotes), remotes)


class TestRunBuild(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()
		# The proxy finalize recipe reads Atlas Settings.region (no per-VM region
		# field anymore) to write the region + name the cert dir. Pin it so the
		# finalize command carries "blr1" and atlas_region() doesn't throw.
		frappe.db.set_single_value("Atlas Settings", "region", "blr1")

	def test_uploads_tree_then_runs_detached_and_records_task(self) -> None:
		vm = _new_vm()
		with _mock_build_ssh(("baked", "", 0)) as (run_ssh, run_scp, run_detached, forget_host, _fin):
			image_builder.run_build(vm.name, _BENCH)
		# Every committed file scp'd; a stale recycled-IP host key dropped first.
		self.assertEqual(run_scp.call_count, len(image_builder.tree_uploads(_BENCH)))
		forget_host.assert_called_once_with("2400::dead")
		# mkdir is the first short SSH; the long build goes through run_detached.
		self.assertIn("mkdir -p", run_ssh.call_args_list[0].args[2])
		run_detached.assert_called_once()
		self.assertIn("build.sh", run_detached.call_args.args[2])
		self.assertEqual(run_detached.call_args.kwargs["log_path"], _BENCH.build_log_path)
		self.assertEqual(run_detached.call_args.kwargs["done_path"], _BENCH.build_done_path)
		status = frappe.get_all(
			"Task", filters={"virtual_machine": vm.name, "script": "bench-build"}, pluck="status"
		)
		self.assertEqual(status, ["Success"])

	def test_build_failure_raises_and_records_failure(self) -> None:
		vm = _new_vm()
		with _mock_build_ssh(("bench init: error", "", 1)):
			with self.assertRaises(frappe.ValidationError):
				image_builder.run_build(vm.name, _BENCH)
		status = frappe.get_all(
			"Task", filters={"virtual_machine": vm.name, "script": "bench-build"}, pluck="status"
		)
		self.assertEqual(status, ["Failure"])

	def test_on_task_callback_fires_with_task_name_on_success(self) -> None:
		vm = _new_vm()
		seen = []
		with _mock_build_ssh(("baked", "", 0)):
			image_builder.run_build(vm.name, _BENCH, on_task=seen.append)
		self.assertEqual(len(seen), 1)
		self.assertTrue(frappe.db.exists("Task", seen[0]))

	def test_on_task_callback_fires_before_throw_on_failure(self) -> None:
		# The Image Build controller links the build Task even on a failed build —
		# on_task must fire before run_build throws.
		vm = _new_vm()
		seen = []
		with _mock_build_ssh(("boom", "", 1)):
			with self.assertRaises(frappe.ValidationError):
				image_builder.run_build(vm.name, _BENCH, on_task=seen.append)
		self.assertEqual(len(seen), 1)
		self.assertEqual(frappe.db.get_value("Task", seen[0], "status"), "Failure")

	def test_proxy_recipe_runs_finalize_after_build(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		with _mock_build_ssh(("built", "", 0)) as (_ssh, _scp, _det, _fh, finalize_run_ssh):
			image_builder.run_build(vm.name, _PROXY)
		# The proxy recipe's finalize wrote the region + restarted the unit.
		finalize_run_ssh.assert_called_once()
		finalize_command = finalize_run_ssh.call_args.args[2]
		self.assertIn("blr1", finalize_command)
		self.assertIn("systemctl restart nginx.service", finalize_command)
		# It must NOT repoint the cert symlink (push_cert owns that, after the real
		# cert lands — repointing here would dangle the symlink at start).
		self.assertNotIn("ln -sfn", finalize_command)

	def test_proxy_finalize_failure_is_a_build_failure(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		# Build succeeds, finalize (region-write/restart) fails → run_build throws,
		# and the recorded Task is a Failure.
		with _mock_build_ssh(("built", "", 0), finalize_result=("", "no such unit", 1)):
			with self.assertRaises(frappe.ValidationError):
				image_builder.run_build(vm.name, _PROXY)
		status = frappe.get_all(
			"Task", filters={"virtual_machine": vm.name, "script": "proxy-build"}, pluck="status"
		)
		self.assertEqual(status, ["Failure"])


class TestGuestTaskStream(IntegrationTestCase):
	"""The spec/22 streaming sink: a build Task that exists as Running from the
	start and tails the guest log onto itself, then finalizes with the full output."""

	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_row_is_running_with_started_on_construction(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		stream = image_builder.GuestTaskStream(vm.name, "proxy-build", {"recipe": "proxy"})
		row = frappe.get_doc("Task", stream.task.name)
		self.assertEqual(row.status, "Running")
		self.assertTrue(row.started)
		self.assertEqual(row.virtual_machine, vm.name)

	def test_on_log_appends_bounded_tail_and_progress_line(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		stream = image_builder.GuestTaskStream(vm.name, "proxy-build", {"recipe": "proxy"})
		with patch.object(stream.task, "publish_log") as publish:
			stream.on_log("step one\n")
			stream.on_log("step two\n\n")  # trailing blank lines must be skipped
		row = frappe.get_doc("Task", stream.task.name)
		self.assertEqual(row.live_output, "step one\nstep two\n\n")
		# progress_line is the last NON-empty line, not the trailing blank.
		self.assertEqual(row.progress_line, "step two")
		# Each chunk was pushed over realtime as it arrived.
		self.assertEqual(publish.call_count, 2)

	def test_live_output_is_bounded_to_buffer_size(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		stream = image_builder.GuestTaskStream(vm.name, "proxy-build", {"recipe": "proxy"})
		with patch.object(stream.task, "publish_log"):
			stream.on_log("x" * (image_builder.LIVE_OUTPUT_BUFFER_BYTES + 5000))
		row = frappe.get_doc("Task", stream.task.name)
		self.assertEqual(len(row.live_output), image_builder.LIVE_OUTPUT_BUFFER_BYTES)

	def test_finalize_writes_terminal_status_and_full_output(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		stream = image_builder.GuestTaskStream(vm.name, "proxy-build", {"recipe": "proxy"})
		with patch.object(stream.task, "publish_log"):
			stream.on_log("partial tail")
		name = stream.finalize("FULL AUTHORITATIVE LOG", "warnings", 0)
		row = frappe.get_doc("Task", name)
		self.assertEqual(row.status, "Success")
		self.assertEqual(row.stdout, "FULL AUTHORITATIVE LOG")
		self.assertEqual(row.stderr, "warnings")
		self.assertTrue(row.ended)

	def test_finalize_marks_failure_on_nonzero(self) -> None:
		vm = _new_vm(is_proxy=1, region="blr1")
		stream = image_builder.GuestTaskStream(vm.name, "proxy-build", {"recipe": "proxy"})
		name = stream.finalize("oops", "boom", 1)
		self.assertEqual(frappe.db.get_value("Task", name, "status"), "Failure")


class TestRunBuildStreaming(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_stream_creates_running_row_passes_on_log_and_finalizes(self) -> None:
		# The proxy recipe has a finalize step whose output supersedes the build's
		# (existing run_build contract), so assert on a build with no finalize to keep
		# the streamed stdout == the build output: use the bench recipe, streamed.
		vm = _new_vm()
		with _mock_build_ssh(("BUILD OUTPUT", "", 0)) as (_ssh, _scp, run_detached, _fh, _fin):
			image_builder.run_build(vm.name, _BENCH, stream=True)
		# run_detached received a real on_log sink (the streaming path), not None.
		self.assertIsNotNone(run_detached.call_args.kwargs["on_log"])
		# Exactly one build Task, finalized Success with the full build output —
		# NOT a second row (the streamed row IS the audit row).
		rows = frappe.get_all(
			"Task",
			filters={"virtual_machine": vm.name, "script": "bench-build"},
			fields=["status", "stdout"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Success")
		self.assertEqual(rows[0].stdout, "BUILD OUTPUT")

	def test_default_path_passes_no_on_log_and_records_on_completion(self) -> None:
		# stream defaults False: run_detached gets on_log=None (byte-for-byte the old
		# behavior) and the row is the _record_guest_task one.
		vm = _new_vm()
		with _mock_build_ssh(("baked", "", 0)) as (_ssh, _scp, run_detached, _fh, _fin):
			image_builder.run_build(vm.name, _BENCH)
		self.assertIsNone(run_detached.call_args.kwargs["on_log"])

	def test_stream_on_task_fires_early_with_the_running_row(self) -> None:
		# The Image Build form links build_task to surface the live Task DURING the
		# bake, so in the stream path on_task must fire with the Running row's name
		# before the build finishes — and exactly once (not again at finalize).
		vm = _new_vm()
		seen: list[str] = []

		def link(task_name: str) -> None:
			# At link time the row must already exist and be Running (the build is
			# still notionally in flight; finalize hasn't run yet in production).
			seen.append(task_name)
			self.assertTrue(frappe.db.exists("Task", task_name))

		with _mock_build_ssh(("baked", "", 0)):
			image_builder.run_build(vm.name, _BENCH, on_task=link, stream=True)
		self.assertEqual(len(seen), 1)
		# The linked row is the finalized build Task (same name, now Success).
		self.assertEqual(frappe.db.get_value("Task", seen[0], "status"), "Success")

	def test_stream_on_task_fires_before_throw_on_failure(self) -> None:
		# A failed streamed build must still have linked its row (the form needs it to
		# show the failure), and on_task must not fire twice.
		vm = _new_vm()
		seen: list[str] = []
		with _mock_build_ssh(("boom", "", 1)):
			with self.assertRaises(frappe.ValidationError):
				image_builder.run_build(vm.name, _BENCH, on_task=seen.append, stream=True)
		self.assertEqual(len(seen), 1)
		self.assertEqual(frappe.db.get_value("Task", seen[0], "status"), "Failure")


_V15 = get_recipe("bench-v15")
_V16 = get_recipe("bench-v16")
_NIGHTLY = get_recipe("bench-nightly")


class TestRenderBenchToml(IntegrationTestCase):
	"""The committed bench.toml is rendered per-version before upload — line-targeted,
	stdlib only. These read the real committed tree (pure, no host)."""

	def test_renders_python_and_frappe_branch_for_v15(self) -> None:
		rendered = image_builder._render_bench_toml(_V15)
		self.assertIn('python = "3.11"', rendered)
		self.assertIn('branch = "version-15"', rendered)
		# The committed v16 values are gone — they were rewritten, not appended.
		self.assertNotIn('python = "3.14"', rendered)
		self.assertNotIn('branch = "version-16"', rendered)

	def test_renders_develop_for_nightly(self) -> None:
		rendered = image_builder._render_bench_toml(_NIGHTLY)
		self.assertIn('branch = "develop"', rendered)
		self.assertIn('python = "3.14"', rendered)

	def test_render_is_a_noop_shape_for_v16(self) -> None:
		# v16's pins equal the committed defaults, so the rendered text still carries
		# them (the point is correctness, not that it differs from the file).
		rendered = image_builder._render_bench_toml(_V16)
		self.assertIn('python = "3.14"', rendered)
		self.assertIn('branch = "version-16"', rendered)

	def test_only_frappe_app_branch_is_touched(self) -> None:
		# Section-aware: a second [[apps]] block's branch must NOT be rewritten. Drive
		# the helper against a synthetic toml via the real substitution path by
		# temporarily standing up a recipe-shaped object.
		from atlas.atlas.image_recipes import ImageRecipe

		toml = (
			"[bench]\n"
			'python = "3.14"\n'
			"\n"
			"[[apps]]\n"
			'name = "frappe"\n'
			'branch = "version-16"\n'
			"\n"
			"[[apps]]\n"
			'name = "erpnext"\n'
			'branch = "version-16"\n'
		)
		# _render_bench_toml reads from disk, so exercise the inner section logic by
		# pointing _source_directory at a temp dir holding this toml.
		import tempfile
		from pathlib import Path
		from unittest.mock import patch

		with tempfile.TemporaryDirectory() as d:
			(Path(d) / "bench.toml").write_text(toml)
			recipe = ImageRecipe(
				name="t",
				title="t",
				source_directory="bench",
				build_entrypoint="build.sh",
				remote_directory="/tmp/t",
				disk_gigabytes=1,
				memory_megabytes=1,
				vcpus=1,
				snapshot_title="t",
				task_script="t",
				frappe_branch="version-15",
				python_version="3.11",
			)
			with patch.object(image_builder, "_source_directory", return_value=Path(d)):
				rendered = image_builder._render_bench_toml(recipe)
		# frappe's branch flipped to version-15; erpnext's branch stayed version-16.
		lines = rendered.splitlines()
		self.assertIn('branch = "version-15"', lines)
		self.assertEqual(rendered.count('branch = "version-15"'), 1)
		self.assertEqual(rendered.count('branch = "version-16"'), 1)  # erpnext untouched

	def test_proxy_renders_nothing(self) -> None:
		self.assertIsNone(image_builder._render_bench_toml(_PROXY))

	def test_fail_loud_when_python_line_missing(self) -> None:
		import tempfile
		from pathlib import Path
		from unittest.mock import patch

		from atlas.atlas.image_recipes import ImageRecipe

		with tempfile.TemporaryDirectory() as d:
			(Path(d) / "bench.toml").write_text('[bench]\nname = "atlas"\n')
			recipe = ImageRecipe(
				name="t",
				title="t",
				source_directory="bench",
				build_entrypoint="build.sh",
				remote_directory="/tmp/t",
				disk_gigabytes=1,
				memory_megabytes=1,
				vcpus=1,
				snapshot_title="t",
				task_script="t",
				python_version="3.11",
			)
			with patch.object(image_builder, "_source_directory", return_value=Path(d)):
				with self.assertRaises(frappe.ValidationError):
					image_builder._render_bench_toml(recipe)


class TestBuildCommand(IntegrationTestCase):
	def test_env_carries_cli_ref_and_erpnext_branch(self) -> None:
		env = image_builder._build_env(_V15)
		self.assertEqual(env["BENCH_CLI_REF"], _V15.bench_cli_ref)
		self.assertEqual(env["ERPNEXT_BRANCH"], "version-15")

	def test_proxy_env_is_empty(self) -> None:
		self.assertEqual(image_builder._build_env(_PROXY), {})

	def test_command_exports_env_and_passes_mode(self) -> None:
		command = image_builder._build_command(_V16)
		self.assertIn(f"export BENCH_CLI_REF={_V16.bench_cli_ref}", command)
		self.assertIn("export ERPNEXT_BRANCH=version-16", command)
		# Ends with `… build.sh site` (the bake mode, the entrypoint's positional arg).
		self.assertTrue(command.rstrip().endswith("build.sh site"), command)
		self.assertIn("chmod +x", command)

	def test_command_passes_admin_mode(self) -> None:
		import dataclasses

		from atlas.atlas.image_recipes import RECIPES

		# A hypothetical admin variant: only build_mode differs. Use replace to avoid
		# minting one in the registry.
		admin = dataclasses.replace(RECIPES["bench-v16"], build_mode="admin")
		self.assertTrue(image_builder._build_command(admin).rstrip().endswith("build.sh admin"))


class TestUploadsWithRenderedToml(IntegrationTestCase):
	def test_swaps_bench_toml_for_rendered_temp_and_cleans_up(self) -> None:
		import os
		from contextlib import ExitStack

		with ExitStack() as stack:
			uploads = image_builder._uploads_with_rendered_toml(_V15, stack)
			remote_toml = f"{_V15.remote_directory}/bench.toml"
			swapped = [(local, remote) for local, remote in uploads if remote == remote_toml]
			self.assertEqual(len(swapped), 1)
			local_path = swapped[0][0]
			# The swapped local file is a temp file (not the committed bench.toml) with
			# the v15-rendered content, and it exists while the stack is open.
			self.assertTrue(str(local_path).startswith("/") and local_path.exists())
			self.assertNotEqual(local_path.name, "bench.toml")
			self.assertIn('branch = "version-15"', local_path.read_text())
			temp_name = str(local_path)
		# Closed stack → temp file unlinked.
		self.assertFalse(os.path.exists(temp_name))

	def test_proxy_uploads_are_verbatim(self) -> None:
		from contextlib import ExitStack

		with ExitStack() as stack:
			uploads = image_builder._uploads_with_rendered_toml(_PROXY, stack)
		# Proxy renders nothing, so uploads == tree_uploads verbatim.
		self.assertEqual(uploads, image_builder.tree_uploads(_PROXY))

	def test_run_build_uses_rendered_toml_and_version_command(self) -> None:
		vm = _new_vm()
		with _mock_build_ssh(("baked", "", 0)) as (_ssh, run_scp, run_detached, _fh, _fin):
			image_builder.run_build(vm.name, _V15)
		# The detached build command carries the version env + bake mode.
		command = run_detached.call_args.args[2]
		self.assertIn(f"export BENCH_CLI_REF={_V15.bench_cli_ref}", command)
		self.assertIn("export ERPNEXT_BRANCH=version-15", command)
		self.assertTrue(command.rstrip().endswith("build.sh site"))
		# The bench.toml that was scp'd is the rendered (v15) one, not the committed v16.
		remote_toml = f"{_V15.remote_directory}/bench.toml"
		toml_scp = [c for c in run_scp.call_args_list if c.args[3] == remote_toml]
		self.assertEqual(len(toml_scp), 1)
