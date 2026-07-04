"""Unit coverage for base-image export (spec/08-images.md § two origins; the
standalone form of the migration base ship, spec/24 §5.1): the pure port derivation,
the phase machine, the pre-flight throws, and the immutability/retry contract. Host
facts (the real NBD/dm-clone copy) are the migration base-ship's e2e territory;
everything here runs in milliseconds with no host."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import export as export_module
from atlas.atlas.doctype.virtual_machine_image_export.virtual_machine_image_export import (
	active_export_for,
)
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server


def _servers(target_status: str = "Active") -> tuple[str, str]:
	"""A source + target on ONE provider (cross-provider export is rejected)."""
	provider = make_provider("exp-test-provider")
	source = make_server(
		provider,
		"exp-source",
		ipv4_address="10.0.0.1",
		ipv6_address="2001:db8:9::1",
		ipv6_prefix="2001:db8:9::/64",
		ipv6_virtual_machine_range="2001:db8:9::/124",
		status="Active",
	).name
	target = make_server(
		provider,
		"exp-target",
		ipv4_address="10.0.0.2",
		ipv6_address="2001:db8:a::1",
		ipv6_prefix="2001:db8:a::/64",
		ipv6_virtual_machine_range="2001:db8:a::/124",
		status=target_status,
	).name
	return source, target


def _local_image(name: str = "exp-local-image") -> str:
	"""A local (promoted-from-snapshot) image: no rootfs URL, so export — not sync — is
	the only way to place it on another host."""
	return make_image(
		name,
		kernel_url="",
		kernel_sha256="",
		rootfs_url="",
		rootfs_sha256="",
	).name


def _export_row(image: str, source: str, target: str):
	"""Insert an export row directly (bypassing export_image's guard) so a test can
	drive its phase machine. source_server is passed so before_insert doesn't try the
	Task-history resolution (there is no real promote Task in a unit test)."""
	return frappe.get_doc(
		{
			"doctype": "Virtual Machine Image Export",
			"image": image,
			"source_server": source,
			"target_server": target,
		}
	).insert(ignore_permissions=True)


class TestExportPure(IntegrationTestCase):
	def test_export_port_is_stable_and_in_range(self) -> None:
		port = export_module.export_port("bench-v16")
		self.assertEqual(port, export_module.export_port("bench-v16"))  # stable
		self.assertTrue(20000 <= port < 25000)
		# Even (the tar rides on port+1, so the pair-stride keeps pairs disjoint).
		self.assertEqual(port % 2, 0)

	def test_export_ports_differ_across_images(self) -> None:
		self.assertNotEqual(export_module.export_port("bench-v15"), export_module.export_port("bench-v16"))

	def test_base_clone_device_is_image_keyed(self) -> None:
		self.assertEqual(export_module.base_clone_device("foo"), "atlas-base-foo-clone")

	def test_bytes_to_gib_ceil_rounds_up(self) -> None:
		gib = 1024**3
		self.assertEqual(export_module._bytes_to_gib_ceil(0), 0)
		self.assertEqual(export_module._bytes_to_gib_ceil(1), 1)
		self.assertEqual(export_module._bytes_to_gib_ceil(gib), 1)
		self.assertEqual(export_module._bytes_to_gib_ceil(gib + 1), 2)


class TestExportRowContract(IntegrationTestCase):
	def test_source_equals_target_raises(self) -> None:
		source, _ = _servers()
		image = _local_image()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Image Export",
					"image": image,
					"source_server": source,
					"target_server": source,
				}
			).insert(ignore_permissions=True)

	def test_target_server_immutable_after_insert(self) -> None:
		source, target = _servers()
		row = _export_row(_local_image(), source, target)
		row.target_server = source
		with self.assertRaises(frappe.ValidationError):
			row.save(ignore_permissions=True)

	def test_active_export_for(self) -> None:
		source, target = _servers()
		# A dedicated image name so a row inserted by another test in this class (which
		# shares the DB within the case) can't pre-populate this image+target slot.
		image = _local_image("exp-active-for-image")
		self.assertIsNone(active_export_for(image, target))
		row = _export_row(image, source, target)
		self.assertEqual(active_export_for(image, target), row.name)
		# A different target is a distinct slot — no false in-flight.
		self.assertIsNone(active_export_for(image, source))
		# Terminal rows don't count as in-flight.
		row.db_set("status", "Done")
		self.assertIsNone(active_export_for(image, target))

	def test_retry_only_from_failed_and_resumes_recorded_phase(self) -> None:
		source, target = _servers()
		row = _export_row(_local_image(), source, target)
		# Not Failed → retry refused.
		with self.assertRaises(frappe.ValidationError):
			row.retry()
		row.db_set({"status": "Failed", "error_at_status": "Hydrating"})
		row.reload()
		row.retry()
		row.reload()
		self.assertEqual(row.status, "Hydrating")
		self.assertIsNone(row.error_message)


class TestExportPhaseMachine(IntegrationTestCase):
	"""Drive the phase machine with a faked host, asserting the copy walks
	Pending → Exporting → Hydrating(hold→…→100%) → Finalizing → … → Done."""

	def _fake_run_task(self, *, script, variables, server, timeout_seconds, **_):
		if script == "migration-export-base":
			return fake_task(
				stdout='ATLAS_RESULT={"nbd_port": 20010, "nbd_pid": 4242, '
				'"base_size_bytes": 1, "meta_port": 20011, "meta_pid": 4243, '
				'"meta_size_bytes": 512}'
			)
		if script == "migration-poll-hydration":
			return fake_task(stdout=f'ATLAS_RESULT={{"hydration_percent": {self._percent}}}')
		return fake_task(stdout="ok")

	def test_advance_reports_more_work_until_hydration_holds(self) -> None:
		source, target = _servers()
		row = _export_row(_local_image(), source, target)
		self._percent = 20  # hydration holds below 100

		with patch.object(export_module, "run_task", side_effect=self._fake_run_task):
			# Pending → Exporting → Hydrating each advance to a further non-terminal phase.
			row.reload()
			while row.status != "Hydrating":
				self.assertTrue(export_module.advance_export(row))
				row.reload()
			# Exporting recorded the NBD handle + base size off the ATLAS_RESULT.
			self.assertEqual(row.nbd_port, 20010)
			self.assertEqual(row.nbd_pid, 4242)
			self.assertEqual(row.base_size_bytes, 1)
			# Hydrating at 20% holds — no further phase to run now.
			self.assertFalse(export_module.advance_export(row))
			row.reload()
			self.assertEqual(row.hydration_percent, 20)

	def test_full_walk_to_done(self) -> None:
		source, target = _servers()
		row = _export_row(_local_image(), source, target)
		self._percent = 100  # hydration completes immediately

		with patch.object(export_module, "run_task", side_effect=self._fake_run_task):
			# Walk every phase; advance_export returns False only at the terminal step.
			for _ in range(len(export_module.PHASE_ORDER) + 2):
				row.reload()
				if row.status == "Done":
					break
				export_module.advance_export(row)
			row.reload()
			self.assertEqual(row.status, "Done")
			self.assertIsNotNone(row.completed_at)

	def test_start_export_self_drives_and_reenqueues(self) -> None:
		source, target = _servers()
		row = _export_row(_local_image(), source, target)
		self._percent = 20  # holds, so the row stays non-terminal → re-enqueues

		with (
			patch.object(export_module, "run_task", side_effect=self._fake_run_task),
			patch.object(export_module.frappe, "enqueue") as enqueue,
		):
			export_module.start_export(row.name)
			# A non-terminal step re-enqueues the next drive on the same row.
			self.assertEqual(enqueue.call_count, 1)
			self.assertEqual(enqueue.call_args.kwargs["name"], row.name)

	def test_hydration_stall_guard_fails(self) -> None:
		source, target = _servers()
		row = _export_row(_local_image(), source, target)
		row.db_set(
			{
				"status": "Hydrating",
				"nbd_port": 20010,
				"base_size_bytes": 1,
				"hydration_percent": 20,
				"hydration_stall_ticks": export_module.HYDRATION_STALL_TICKS - 1,
			}
		)
		self._percent = 20  # no progress → stall

		with patch.object(export_module, "run_task", side_effect=self._fake_run_task):
			row.reload()
			with self.assertRaises(frappe.ValidationError):
				export_module.advance_export(row)


class TestExportPreflight(IntegrationTestCase):
	def test_rejects_from_url_image(self) -> None:
		source, target = _servers()
		# A normal from-URL image is placed by sync, not export.
		url_image = make_image("exp-url-image").name
		with self.assertRaises(frappe.ValidationError):
			export_module.preflight_checks(url_image, target, source)

	def test_rejects_same_source_and_target(self) -> None:
		source, _ = _servers()
		image = _local_image()
		with self.assertRaises(frappe.ValidationError):
			export_module.preflight_checks(image, source, source)

	def test_rejects_inactive_target(self) -> None:
		source, target = _servers(target_status="Archived")
		image = _local_image()
		with self.assertRaises(frappe.ValidationError):
			export_module.preflight_checks(image, target, source)

	def test_rejects_unresolvable_source(self) -> None:
		_, target = _servers()
		image = _local_image()
		# No source passed and no promote Task in history → unresolvable.
		with self.assertRaises(frappe.ValidationError):
			export_module.preflight_checks(image, target, None)

	def test_passes_for_local_image_with_explicit_source(self) -> None:
		source, target = _servers()
		image = _local_image()
		# Should not raise: local image, explicit source, distinct Active same-provider target.
		export_module.preflight_checks(image, target, source)
