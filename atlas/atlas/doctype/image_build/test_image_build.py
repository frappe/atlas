"""Unit tests for the Image Build controller — the bake lifecycle state machine.

All milliseconds, no host: the host steps (provision a build VM, run build.sh in
the guest, snapshot it) are mocked at the module seams; only the pure orchestration
(status transitions, artifact linking, auto-register, terminate, immutability,
fail-loud, rebake) is asserted here. The real bake is the e2e's job (spec/15)."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.image_build import image_build as image_build_module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
)


def _purge() -> None:
	for name in frappe.get_all("Image Build", pluck="name"):
		frappe.delete_doc("Image Build", name, force=1, ignore_permissions=True)


def _new_build(recipe: str = "bench-v16", **overrides):
	"""Insert an Image Build WITHOUT firing the background job (after_insert
	enqueues run() — we drive run() by hand in the tests that want it).

	Passes an explicit `base_image` so insert never depends on `default_image()`
	resolving cleanly (the shared test DB carries several active images, which
	default_image() refuses to pick between — that's the operator's job, not this
	test's concern)."""
	doc = {
		"doctype": "Image Build",
		"recipe": recipe,
		"server": _ensure_test_server(),
		"base_image": _ensure_test_image(),
	}
	doc.update(overrides)
	with patch.object(image_build_module.frappe, "enqueue"):
		return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestImageBuildInsert(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_before_insert_fills_title_and_status(self) -> None:
		build = _new_build("bench-v16")
		self.assertEqual(build.title, "Bench v16")
		self.assertEqual(build.status, "Draft")
		# Base image defaulted from Atlas Settings / the active image.
		self.assertTrue(build.base_image)

	def test_proxy_recipe_inserts(self) -> None:
		build = _new_build("proxy")
		self.assertEqual(build.title, "Reverse proxy image")

	def test_after_insert_enqueues_run(self) -> None:
		with patch.object(image_build_module.frappe, "enqueue") as enqueue:
			frappe.get_doc(
				{
					"doctype": "Image Build",
					"recipe": "bench-v16",
					"server": _ensure_test_server(),
					"base_image": _ensure_test_image(),
				}
			).insert(ignore_permissions=True)
		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args.args[0],
			"atlas.atlas.doctype.image_build.image_build.run",
		)
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")

	def test_recipe_is_immutable_after_insert(self) -> None:
		build = _new_build("bench-v16")
		build.recipe = "proxy"
		with self.assertRaises(frappe.ValidationError):
			build.save(ignore_permissions=True)


class TestImageBuildRun(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def _run_with_mocks(self, build, **extra):
		"""Drive run() with every host seam mocked. Returns the mocks for asserting."""
		defaults = dict(
			# The preflight capacity guard SSHes the host; mock it so the host-free run()
			# flow doesn't try to reach the fake build server.
			_capacity=patch.object(image_build_module, "_assert_host_has_capacity"),
			_provision_build_vm=patch.object(
				image_build_module, "_provision_build_vm", return_value="build-vm-1"
			),
			_wait=patch.object(image_build_module, "_wait_for_vm_running"),
			run_build=patch.object(image_build_module, "run_build"),
			# The post-build serve+login gate SSHes a real guest; mock it here so the
			# host-free run() flow doesn't try to reach the fake build VM.
			sanity=patch.object(image_build_module.bench_image, "sanity_check"),
			# The pre-snapshot shrink stops/resizes/(re)boots a real build VM; mock it
			# so the host-free flow doesn't reach the fake VM.
			_resize=patch.object(image_build_module, "_resize_to_restore_memory"),
			_snap=patch.object(image_build_module, "_stop_and_snapshot", return_value="snap-1"),
			_register=patch.object(image_build_module, "_register"),
			_terminate=patch.object(image_build_module, "_terminate_build_vm"),
			commit=patch.object(image_build_module.frappe.db, "commit"),
		)
		with (
			defaults["_capacity"],
			defaults["_provision_build_vm"] as m_prov,
			defaults["_wait"] as m_wait,
			defaults["run_build"] as m_build,
			defaults["sanity"] as m_sanity,
			defaults["_resize"],
			defaults["_snap"] as m_snap,
			defaults["_register"] as m_register,
			defaults["_terminate"] as m_terminate,
			defaults["commit"],
		):
			image_build_module.run(build.name)
		return m_prov, m_wait, m_build, m_sanity, m_snap, m_register, m_terminate

	def test_happy_path_reaches_available_and_links_artifacts(self) -> None:
		build = _new_build("bench-v16")
		_, _, m_build, _, _, _, _ = self._run_with_mocks(build)
		build.reload()
		self.assertEqual(build.status, "Available")
		self.assertEqual(build.build_virtual_machine, "build-vm-1")
		self.assertEqual(build.snapshot, "snap-1")
		# The bake drives run_build with stream=True (spec/22) so the build Task is
		# created Running up front and tails the in-guest log live on this form,
		# and with an on_task callback that links build_task to that live row.
		self.assertTrue(m_build.call_args.kwargs["stream"])
		self.assertIsNotNone(m_build.call_args.kwargs["on_task"])

	def test_bench_build_auto_registers_when_checked(self) -> None:
		build = _new_build("bench-v16", auto_register=1)
		_, _, _, _, _, m_register, _ = self._run_with_mocks(build)
		m_register.assert_called_once()

	def test_bench_build_skips_register_when_unchecked(self) -> None:
		build = _new_build("bench-v16", auto_register=0)
		_, _, _, _, _, m_register, _ = self._run_with_mocks(build)
		m_register.assert_not_called()

	def test_proxy_build_never_registers(self) -> None:
		# The proxy recipe has no registers_as, so register is skipped even if the
		# (harmless, defaulted-on) auto_register check is set.
		build = _new_build("proxy", auto_register=1)
		_, _, _, _, _, m_register, _ = self._run_with_mocks(build)
		m_register.assert_not_called()

	def test_bench_build_runs_sanity_gate_before_snapshot(self) -> None:
		# A bench build must clear the serve+login gate on the build VM before it is
		# allowed to snapshot.
		build = _new_build("bench-v16")
		_, _, _, m_sanity, _, _, _ = self._run_with_mocks(build)
		m_sanity.assert_called_once_with("build-vm-1")

	def test_proxy_build_skips_sanity_gate(self) -> None:
		# The proxy bakes no Frappe site, so the Frappe serve+login gate doesn't apply.
		build = _new_build("proxy")
		_, _, _, m_sanity, _, _, _ = self._run_with_mocks(build)
		m_sanity.assert_not_called()

	def test_failed_sanity_gate_marks_build_failed_no_snapshot(self) -> None:
		# The gate raising (a build that serves wrong / won't log in) must fail the
		# build loud and never reach the snapshot step.
		build = _new_build("bench-v16")
		with (
			patch.object(image_build_module, "_assert_host_has_capacity"),
			patch.object(image_build_module, "_provision_build_vm", return_value="vm-x"),
			patch.object(image_build_module, "_wait_for_vm_running"),
			patch.object(image_build_module, "run_build"),
			patch.object(
				image_build_module.bench_image,
				"sanity_check",
				side_effect=frappe.ValidationError("did not log in"),
			),
			patch.object(image_build_module, "_stop_and_snapshot") as m_snap,
			patch.object(image_build_module.frappe.db, "commit"),
		):
			with self.assertRaises(frappe.ValidationError):
				image_build_module.run(build.name)
		m_snap.assert_not_called()
		build.reload()
		self.assertEqual(build.status, "Failed")
		self.assertIn("did not log in", build.error)

	def test_terminate_build_vm_when_checked(self) -> None:
		build = _new_build("bench-v16", terminate_build_vm=1)
		_, _, _, _, _, _, m_terminate = self._run_with_mocks(build)
		m_terminate.assert_called_once_with("build-vm-1")

	def test_keeps_build_vm_by_default(self) -> None:
		build = _new_build("bench-v16")
		_, _, _, _, _, _, m_terminate = self._run_with_mocks(build)
		m_terminate.assert_not_called()

	def test_failure_marks_failed_and_records_error_and_reraises(self) -> None:
		build = _new_build("bench-v16")
		with (
			patch.object(image_build_module, "_assert_host_has_capacity"),
			patch.object(image_build_module, "_provision_build_vm", return_value="vm-x"),
			patch.object(image_build_module, "_wait_for_vm_running"),
			patch.object(image_build_module, "run_build", side_effect=RuntimeError("build broke")),
			patch.object(image_build_module.frappe.db, "commit"),
		):
			with self.assertRaises(RuntimeError):
				image_build_module.run(build.name)
		build.reload()
		self.assertEqual(build.status, "Failed")
		self.assertIn("build broke", build.error)

	def test_run_is_noop_when_not_draft(self) -> None:
		build = _new_build("bench-v16")
		build.db_set("status", "Available")
		with patch.object(image_build_module, "_provision_build_vm") as m_prov:
			image_build_module.run(build.name)
		m_prov.assert_not_called()

	def test_provision_build_vm_stamps_bench_mode(self) -> None:
		# The real _provision_build_vm inserts a VM (its after_insert enqueues a boot
		# job — patch enqueue so it doesn't run). A bench recipe stamps build_mode=site;
		# the proxy recipe leaves it empty.
		from atlas.atlas.image_recipes import get_recipe

		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAA test")
		build = _new_build("bench-v16")
		with patch.object(image_build_module.frappe, "enqueue"):
			vm_name = image_build_module._provision_build_vm(build, get_recipe("bench-v16"))
		self.assertEqual(frappe.db.get_value("Virtual Machine", vm_name, "build_mode"), "site")

	def test_provision_build_vm_proxy_has_no_mode(self) -> None:
		from atlas.atlas.image_recipes import get_recipe

		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAA test")
		build = _new_build("proxy")
		with patch.object(image_build_module.frappe, "enqueue"):
			vm_name = image_build_module._provision_build_vm(build, get_recipe("proxy"))
		self.assertFalse(frappe.db.get_value("Virtual Machine", vm_name, "build_mode"))

	def test_provision_boots_build_vm_at_fat_build_memory(self) -> None:
		# A bench recipe fattens the build VM (build_memory_megabytes) so the Node-asset
		# build has headroom; the VM must boot at THAT, not the smaller restore size.
		from atlas.atlas.image_recipes import get_recipe

		recipe = get_recipe("bench-v16")
		self.assertGreater(recipe.build_memory_megabytes, recipe.memory_megabytes)
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAA test")
		build = _new_build("bench-v16")
		with patch.object(image_build_module.frappe, "enqueue"):
			vm_name = image_build_module._provision_build_vm(build, recipe)
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", vm_name, "memory_megabytes"),
			recipe.effective_build_memory_megabytes,
		)

	def test_provision_boots_proxy_vm_at_its_single_memory(self) -> None:
		# The proxy leaves build_memory_megabytes unset (0), so effective_* falls back
		# to memory_megabytes — build and restore at one size, no resize step.
		from atlas.atlas.image_recipes import get_recipe

		recipe = get_recipe("proxy")
		self.assertEqual(recipe.build_memory_megabytes, 0)
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAA test")
		build = _new_build("proxy")
		with patch.object(image_build_module.frappe, "enqueue"):
			vm_name = image_build_module._provision_build_vm(build, recipe)
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", vm_name, "memory_megabytes"),
			recipe.memory_megabytes,
		)

	def _run_capacity_guard(self, mem_available_kb: int, needed_megabytes: int = 6144) -> None:
		"""Drive _assert_host_has_capacity with the host memory probe stubbed."""
		server = _ensure_test_server()
		connection = SimpleNamespace(host="host-1", ssh_private_key="pk")

		@contextmanager
		def fake_key_file(_key):
			yield "/tmp/key"

		with (
			patch("atlas.atlas.ssh.connection_for_server", return_value=connection),
			patch("atlas.atlas._ssh.transport.ssh_key_file", fake_key_file),
			patch("atlas.atlas._ssh.transport.run_ssh", return_value=(str(mem_available_kb), "", 0)),
		):
			image_build_module._assert_host_has_capacity(server, needed_megabytes)

	def test_capacity_guard_throws_when_host_too_small(self) -> None:
		# A 4 GB host (~3.9 GB available) can't hold a 6 GB fat build VM — the exact
		# mismatch that OOM-kills firecracker mid-build. Fail loud BEFORE booting it.
		with self.assertRaises(frappe.ValidationError):
			self._run_capacity_guard(3_900_000)

	def test_capacity_guard_passes_when_host_large_enough(self) -> None:
		# An 8 GB host (~7.4 GB available) fits the 6 GB build VM plus the margin.
		self._run_capacity_guard(7_400_000)

	def test_capacity_guard_fails_open_on_unreadable_probe(self) -> None:
		# An empty MemAvailable read (odd host / parse miss) skips the guard rather than
		# blocking an otherwise-fine bake.
		self._run_capacity_guard(0)

	def test_resize_shrinks_stopped_vm_and_leaves_it_stopped(self) -> None:
		# The shrink: stop (if needed) → resize to the restore size → NO reboot. Both
		# snapshot paths pick up from here — the cold path snapshots Stopped, the warm
		# path boots it back up itself (_warm_snapshot). Assert resize with the restore
		# memory and the VM is never started back up by resize.
		from atlas.atlas.image_recipes import get_recipe

		recipe = get_recipe("bench-v16")
		vm = self._fake_vm(status="Running")
		with (
			patch.object(image_build_module.frappe, "get_doc", return_value=vm),
			patch.object(image_build_module, "_sync_guest_before_stop"),
		):
			image_build_module._resize_to_restore_memory(recipe, vm.name)
		vm.stop.assert_called_once()
		vm.resize.assert_called_once_with(memory_megabytes=recipe.memory_megabytes)
		vm.start.assert_not_called()

	def test_resize_is_noop_when_build_memory_equals_restore(self) -> None:
		# A recipe that didn't fatten (proxy: build == restore) never touches the VM.
		from atlas.atlas.image_recipes import get_recipe

		with patch.object(image_build_module.frappe, "get_doc") as m_get:
			image_build_module._resize_to_restore_memory(get_recipe("proxy"), "vm-x")
		m_get.assert_not_called()

	def test_cold_snapshot_syncs_guest_before_stopping(self) -> None:
		# The bug this guards: build.sh's last write (bench-domain-provider) was still
		# dirty in the guest page cache when the plain `systemctl stop` KILLED the guest,
		# so ext4 journalled the inode but not the data → the snapshot captured a 0-byte
		# binary that fails at deploy with `Exec format error`. The cold snapshot must
		# flush the guest BEFORE it stops it, so the capture is durable.
		from atlas.atlas.image_recipes import get_recipe

		recipe = get_recipe("bench-v16")
		vm = self._fake_vm(status="Running")
		with (
			patch.object(image_build_module.frappe, "get_doc", return_value=vm),
			patch.object(image_build_module, "_sync_guest_before_stop") as m_sync,
		):
			image_build_module._stop_and_snapshot(None, recipe, vm.name)
		m_sync.assert_called_once_with(vm.name)
		vm.stop.assert_called_once()
		vm.snapshot.assert_called_once_with(title=recipe.snapshot_title)

	def test_sync_guest_before_stop_noops_when_not_running(self) -> None:
		# A guest that is already Stopped has nothing live to flush (and no VM to SSH),
		# so the sync is a clean no-op — it never reaches for a connection.
		vm = self._fake_vm(status="Stopped")
		with patch.object(image_build_module.frappe, "get_doc", return_value=vm):
			image_build_module._sync_guest_before_stop(vm.name)
		vm.stop.assert_not_called()

	def test_warm_snapshot_boots_stopped_vm_before_capture(self) -> None:
		# The warm path owns getting its own guest warm: the resize step leaves the VM
		# Stopped, so _warm_snapshot must boot it back up (at the small size) BEFORE it
		# arms + freezes. start() is synchronous (returns Running), so the boot is a
		# start() + commit — NOT a rollback-polling wait, which would wipe start()'s
		# uncommitted save in this job's txn. Stop the test right after the boot (raise
		# from _run_warm_entrypoint) — the run_task/capture tail needs a real host.
		from atlas.atlas.image_recipes import get_recipe

		recipe = get_recipe("bench-v16")
		vm = self._fake_vm(status="Stopped")
		# start() flips it Running, mirroring the synchronous controller start().
		vm.start.side_effect = lambda: setattr(vm, "status", "Running")
		with (
			patch.object(image_build_module.frappe, "get_doc", return_value=vm),
			patch.object(image_build_module.frappe.db, "commit") as m_commit,
			patch.object(image_build_module, "_wait_for_guest_serving") as m_wait,
			patch.object(image_build_module, "_run_warm_entrypoint", side_effect=RuntimeError("stop here")),
		):
			with self.assertRaises(RuntimeError):
				image_build_module._warm_snapshot(_new_build("bench-v16"), recipe, vm.name)
		vm.start.assert_called_once()
		m_commit.assert_called_once()
		# The guest must be proven SERVING (not just Running) before the warm arm.
		m_wait.assert_called_once_with(vm.name)

	def test_warm_snapshot_skips_boot_when_already_running(self) -> None:
		# A warm bake whose recipe DID fatten left the VM Running after the resize
		# reboot (or a no-fatten recipe never stopped it) — don't double-boot.
		from atlas.atlas.image_recipes import get_recipe

		recipe = get_recipe("bench-v16")
		vm = self._fake_vm(status="Running")
		with (
			patch.object(image_build_module.frappe, "get_doc", return_value=vm),
			patch.object(image_build_module.frappe.db, "commit") as m_commit,
			patch.object(image_build_module, "_wait_for_guest_serving") as m_wait,
			patch.object(image_build_module, "_run_warm_entrypoint", side_effect=RuntimeError("stop here")),
		):
			with self.assertRaises(RuntimeError):
				image_build_module._warm_snapshot(_new_build("bench-v16"), recipe, vm.name)
		vm.start.assert_not_called()
		m_commit.assert_not_called()
		m_wait.assert_not_called()

	def test_wait_for_guest_serving_retries_until_sanity_passes(self) -> None:
		# The readiness probe: retry sanity_check (SSH refused / not-yet-serving raises)
		# until it passes. First two probes fail, third succeeds → returns, no throw.
		with (
			patch.object(
				image_build_module.bench_image,
				"sanity_check",
				side_effect=[RuntimeError("ssh refused"), RuntimeError("no pong"), {"ok": True}],
			) as m_sanity,
			patch.object(image_build_module.time, "sleep"),
		):
			image_build_module._wait_for_guest_serving("vm-1", timeout_seconds=300, poll_seconds=0)
		self.assertEqual(m_sanity.call_count, 3)

	def test_wait_for_guest_serving_throws_on_timeout(self) -> None:
		# Never serves before the deadline → throw carrying the last probe error.
		with (
			patch.object(
				image_build_module.bench_image,
				"sanity_check",
				side_effect=RuntimeError("still booting"),
			),
			patch.object(image_build_module.time, "sleep"),
			patch.object(image_build_module.time, "monotonic", side_effect=[0, 1, 400]),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				image_build_module._wait_for_guest_serving("vm-1", timeout_seconds=300, poll_seconds=0)
		self.assertIn("still booting", str(ctx.exception))

	def _fake_vm(self, status: str):
		from unittest.mock import MagicMock

		vm = MagicMock()
		vm.name = "build-vm-1"
		vm.status = status
		# reload() re-reads status; keep it Stopped after a stop so the code path is stable.
		vm.reload.side_effect = lambda: setattr(vm, "status", "Stopped")
		return vm

	def test_records_build_inputs_from_task_stdout(self) -> None:
		# _record_build_inputs harvests the ATLAS_BUILD_*= lines build.sh stamped into
		# the build Task's stdout into build_inputs JSON. Insert a REAL Task row (the
		# harvest reads stdout back from the DB by name).
		build = _new_build("bench-nightly")
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"script": "bench-build",
				"triggered_by": "Administrator",
				"status": "Success",
				"variables": "{}",
				"stdout": (
					"some build noise\n"
					"ATLAS_BUILD_BENCH_CLI_REF=deadbeef\n"
					"ATLAS_BUILD_FRAPPE_SHA=cafe1234\n"
					"ATLAS_BUILD_ERPNEXT_SHA=feed5678\n"
				),
			}
		).insert(ignore_permissions=True)
		build.db_set("build_task", task.name)
		image_build_module._record_build_inputs(build)
		build.reload()
		inputs = frappe.parse_json(build.build_inputs)
		self.assertEqual(inputs["bench_cli_ref"], "deadbeef")
		self.assertEqual(inputs["frappe_sha"], "cafe1234")
		self.assertEqual(inputs["erpnext_sha"], "feed5678")

	def test_record_build_inputs_noop_without_task(self) -> None:
		build = _new_build("bench-nightly")
		image_build_module._record_build_inputs(build)  # no build_task
		build.reload()
		self.assertFalse(build.build_inputs)


class TestImageBuildRebake(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_rebake_resets_to_draft_and_reenqueues(self) -> None:
		build = _new_build("bench-v16")
		build.db_set("status", "Failed")
		build.db_set("error", "old failure")
		with patch.object(image_build_module.frappe, "enqueue") as enqueue:
			with patch.object(image_build_module.frappe.db, "commit"):
				build.rebake()
		build.reload()
		self.assertEqual(build.status, "Draft")
		self.assertFalse(build.error)
		enqueue.assert_called_once()

	def test_rebake_rejected_while_in_flight(self) -> None:
		build = _new_build("bench-v16")
		build.db_set("status", "Building")
		with self.assertRaises(frappe.ValidationError):
			build.rebake()


class TestImageBuildPromote(IntegrationTestCase):
	"""Promote delegates to the snapshot's promote_to_image — the warm-reject and
	every guard live there. Here we assert the delegation + the guards Image Build
	owns (Available + has a snapshot) and the default image-name slug."""

	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_promote_delegates_with_default_name(self) -> None:
		# Delegation + the default image name. For a versioned bench build the default
		# is the series image name (bench-v16), the Central-Image name-match link — the
		# fallback <recipe>-<build> slug is covered separately for the proxy recipe.
		from unittest.mock import MagicMock

		build = _new_build("bench-v16")
		build.db_set("status", "Available")
		build.db_set("snapshot", "snap-xyz")
		build.reload()
		snapshot = MagicMock()
		snapshot.promote_to_image.return_value = "bench-v16"
		with patch.object(image_build_module.frappe, "get_doc", return_value=snapshot):
			result = build.promote()
		snapshot.promote_to_image.assert_called_once()
		kwargs = snapshot.promote_to_image.call_args.kwargs
		self.assertEqual(kwargs["image_name"], "bench-v16")
		self.assertEqual(result, "bench-v16")

	def test_promote_passes_explicit_name(self) -> None:
		from unittest.mock import MagicMock

		build = _new_build("bench-v16")
		build.db_set("status", "Available")
		build.db_set("snapshot", "snap-xyz")
		build.reload()
		snapshot = MagicMock()
		snapshot.promote_to_image.return_value = "my-image"
		with patch.object(image_build_module.frappe, "get_doc", return_value=snapshot):
			build.promote(image_name="my-image", title="My Image")
		kwargs = snapshot.promote_to_image.call_args.kwargs
		self.assertEqual(kwargs["image_name"], "my-image")
		self.assertEqual(kwargs["title"], "My Image")

	def test_promote_rejects_non_available_build(self) -> None:
		build = _new_build("bench-v16")  # Draft
		with self.assertRaises(frappe.ValidationError) as raised:
			build.promote()
		self.assertIn("Available", str(raised.exception))

	def test_promote_rejects_build_without_snapshot(self) -> None:
		build = _new_build("bench-v16")
		build.db_set("status", "Available")  # but no snapshot linked
		build.reload()
		with self.assertRaises(frappe.ValidationError) as raised:
			build.promote()
		self.assertIn("no snapshot", str(raised.exception))

	def test_versioned_build_default_names_to_series_image(self) -> None:
		# A versioned bench build's promoted image defaults to the SERIES name
		# (bench-v15 / -v16 / -nightly), not the <recipe>-<build name> slug, so
		# customers pick the version through the VM `image` field.
		from unittest.mock import MagicMock

		for recipe in ("bench-v15", "bench-v16", "bench-nightly"):
			with self.subTest(recipe=recipe):
				build = _new_build(recipe)
				build.db_set("status", "Available")
				build.db_set("snapshot", "snap-xyz")
				build.reload()
				snapshot = MagicMock()
				snapshot.promote_to_image.return_value = recipe
				with patch.object(image_build_module.frappe, "get_doc", return_value=snapshot):
					build.promote()
				self.assertEqual(snapshot.promote_to_image.call_args.kwargs["image_name"], recipe)

	def test_explicit_name_overrides_series_default(self) -> None:
		from unittest.mock import MagicMock

		build = _new_build("bench-v15")
		build.db_set("status", "Available")
		build.db_set("snapshot", "snap-xyz")
		build.reload()
		snapshot = MagicMock()
		snapshot.promote_to_image.return_value = "custom"
		with patch.object(image_build_module.frappe, "get_doc", return_value=snapshot):
			build.promote(image_name="custom")
		self.assertEqual(snapshot.promote_to_image.call_args.kwargs["image_name"], "custom")

	def test_proxy_build_falls_back_to_recipe_build_slug(self) -> None:
		# A recipe with no series name (proxy) keeps the old <recipe>-<build> default.
		from unittest.mock import MagicMock

		build = _new_build("proxy")
		build.db_set("status", "Available")
		build.db_set("snapshot", "snap-xyz")
		build.reload()
		snapshot = MagicMock()
		snapshot.promote_to_image.return_value = "x"
		with patch.object(image_build_module.frappe, "get_doc", return_value=snapshot):
			build.promote()
		self.assertEqual(
			snapshot.promote_to_image.call_args.kwargs["image_name"], f"proxy-{build.name}".lower()
		)
