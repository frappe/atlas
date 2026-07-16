import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task


def _stopped_vm() -> "frappe.model.document.Document":
	vm = _new_vm()
	vm.db_set("status", "Stopped")
	vm.reload()
	return vm


def _make_snapshot(vm) -> "frappe.model.document.Document":
	from atlas.atlas.doctype.virtual_machine import virtual_machine as module

	with patch.object(module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1024}')):
		name = vm.snapshot("snap")
	return frappe.get_doc("Virtual Machine Snapshot", name)


class TestVirtualMachineSnapshot(IntegrationTestCase):
	def setUp(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		_ensure_test_server()
		_ensure_test_image()
		# Snapshot.on_trash fires a real delete-snapshot-vm.py over SSH for any
		# leftover row whose VM is still live (the test server's 10.0.0.99 is
		# unreachable, so a real call hangs until timeout). Cleanup is harness
		# bookkeeping, not the behaviour under test — stub run_task while we
		# clear prior-test rows.
		with patch.object(module, "run_task", return_value=fake_task()):
			for name in frappe.get_all("Virtual Machine Snapshot", pluck="name"):
				frappe.delete_doc("Virtual Machine Snapshot", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_on_trash_runs_delete_script_for_live_vm(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		with patch.object(module, "run_task", return_value=fake_task()) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "delete-snapshot-vm")
		self.assertEqual(mocked.call_args.kwargs["variables"]["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)

	def test_on_trash_runs_delete_script_for_terminated_vm(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		# A snapshot LV lives in the thin pool, OUTSIDE the VM directory that
		# terminate-vm.py rm -rf'd — so it survives terminate and on_trash MUST
		# still lvremove it, even for a Terminated VM. (The old file-backed model
		# could skip this because the files were already gone with the directory.)
		vm = _stopped_vm()
		snapshot = _make_snapshot(vm)
		vm.db_set("status", "Terminated")
		with patch.object(module, "run_task", return_value=fake_task()) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "delete-snapshot-vm")

	def test_clone_to_new_vm_creates_fresh_identity(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _stopped_vm()
		snapshot = _make_snapshot(source)

		# Don't let the enqueued auto_provision run in-process.
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="cloned vm", ssh_public_key="ssh-ed25519 CLONE")

		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertNotEqual(clone.name, source.name)
		self.assertNotEqual(clone.ipv6_address, source.ipv6_address)
		self.assertNotEqual(clone.mac_address, source.mac_address)
		self.assertEqual(clone.server, source.server)
		self.assertEqual(clone.image, snapshot.source_image)
		self.assertEqual(clone.clone_source_rootfs, snapshot.rootfs_path)
		self.assertEqual(clone.ssh_public_key, "ssh-ed25519 CLONE")
		self.assertEqual(clone.status, "Pending")

	def test_clone_provision_variables_carry_snapshot_path(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _stopped_vm()
		snapshot = _make_snapshot(source)
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="cloned vm 2", ssh_public_key="ssh-ed25519 CLONE2")
		clone = frappe.get_doc("Virtual Machine", clone_name)
		variables = clone._provision_variables()
		self.assertEqual(variables["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)
		# Kernel still comes from the image.
		self.assertEqual(variables["IMAGE_NAME"], clone.image)

	def test_clone_inherits_fractional_cpu_cap_from_source(self) -> None:
		# A fractional-CPU source clones to the SAME fraction: the cap is carried,
		# not defaulted up to vcpus. (Regression guard for the sizing-fallback path
		# that still runs when the build VM is present.)
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _stopped_vm()
		source.db_set("cpu_max_cores", 0.0625)
		source.reload()
		snapshot = _make_snapshot(source)
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="fractional clone", ssh_public_key="ssh-ed25519 F")
		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertEqual(clone.vcpus, source.vcpus)
		self.assertEqual(clone.cpu_max_cores, 0.0625)
		self.assertEqual(clone.memory_megabytes, source.memory_megabytes)

	def test_clone_when_build_vm_gone_uses_snapshot_server_and_explicit_size(self) -> None:
		# The golden is a DURABLE artifact: its build VM is scratch that gets
		# terminated and its row deleted, so the snapshot OUTLIVES it. A clone with
		# explicit sizing (the self-serve Site path) must still work — server from
		# the snapshot's own row, sizing from the args — not throw DoesNotExistError
		# on the dangling `virtual_machine` link.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _stopped_vm()
		snapshot = _make_snapshot(source)
		frappe.delete_doc("Virtual Machine", source.name, force=1, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Virtual Machine", snapshot.virtual_machine))

		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(
				title="orphan clone",
				ssh_public_key="ssh-ed25519 ORPHAN",
				vcpus=1,
				cpu_max_cores=0.0625,
				memory_megabytes=512,
			)
		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertEqual(clone.server, snapshot.server)
		self.assertEqual(clone.vcpus, 1)
		self.assertEqual(clone.cpu_max_cores, 0.0625)
		self.assertEqual(clone.memory_megabytes, 512)
		self.assertEqual(clone.disk_gigabytes, snapshot.disk_gigabytes)
		self.assertEqual(clone.clone_source_rootfs, snapshot.rootfs_path)

	def test_clone_when_build_vm_gone_and_no_size_fails_loud(self) -> None:
		# With no source VM to inherit from AND no explicit sizing, fail with a
		# clear message at the boundary — not a DoesNotExistError deep in get_doc.
		source = _stopped_vm()
		snapshot = _make_snapshot(source)
		frappe.delete_doc("Virtual Machine", source.name, force=1, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			snapshot.clone_to_new_vm(title="no size", ssh_public_key="ssh-ed25519 X")
		self.assertIn("build VM no longer exists", str(raised.exception))

	def test_clone_disk_cannot_shrink_below_snapshot(self) -> None:
		source = _stopped_vm()
		snapshot = _make_snapshot(source)
		# Snapshot captured disk_gigabytes from the source VM (2 in fixtures).
		with self.assertRaises(frappe.ValidationError) as raised:
			snapshot.clone_to_new_vm(title="too small", ssh_public_key="ssh-ed25519 X", disk_gigabytes=1)
		self.assertIn("cannot be smaller", str(raised.exception))

	def test_clone_rejects_unavailable_snapshot(self) -> None:
		source = _stopped_vm()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "pending",
				"virtual_machine": source.name,
				"server": source.server,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			snapshot.clone_to_new_vm(title="x", ssh_public_key="ssh-ed25519 X")
		self.assertIn("not Available", str(raised.exception))

	def test_clone_carries_data_disk(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _new_vm(data_disk_gigabytes=2, data_disk_format_and_mount=1, data_disk_mount_point="/home")
		source.db_set("status", "Stopped")
		source.reload()
		with patch.object(
			vm_module,
			"run_task",
			return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1024, "data_size_bytes": 2048}'),
		):
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source.snapshot("snap-data"))

		with patch.object(vm_module.frappe, "enqueue"):
			clone = frappe.get_doc(
				"Virtual Machine",
				snapshot.clone_to_new_vm(title="clone-with-data", ssh_public_key="ssh-ed25519 C"),
			)
		# The clone inherits the data disk's size + mount config and seeds it from
		# the snapshot's data half.
		self.assertEqual(clone.data_disk_gigabytes, 2)
		self.assertEqual(clone.data_disk_mount_point, "/home")
		self.assertEqual(clone.clone_source_data_rootfs, snapshot.data_rootfs_path)
		variables = clone._provision_variables()
		self.assertEqual(variables["DATA_SNAPSHOT_ROOTFS_PATH"], snapshot.data_rootfs_path)
		self.assertEqual(variables["DATA_DISK_GB"], "2")

	def test_on_trash_removes_data_snapshot_lv(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		source = _new_vm(data_disk_gigabytes=2)
		source.db_set("status", "Stopped")
		source.reload()
		with patch.object(
			vm_module,
			"run_task",
			return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1, "data_size_bytes": 2}'),
		):
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source.snapshot("doomed-data"))

		with patch.object(module, "run_task", return_value=fake_task()) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		self.assertEqual(
			mocked.call_args.kwargs["variables"]["DATA_SNAPSHOT_ROOTFS_PATH"], snapshot.data_rootfs_path
		)

	def test_promote_inserts_inactive_and_enqueues(self) -> None:
		# The button inserts the image row INACTIVE and hands the long host dd to a
		# background job — no run_task in the request. is_active=0 keeps placement from
		# provisioning a half-baked image while the dd is still in flight.
		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "promoted-async"})
		with patch("frappe.enqueue") as enqueue:
			image_name = snapshot.promote_to_image("promoted-async")

		image = frappe.get_doc("Virtual Machine Image", image_name)
		self.assertEqual(image.name, "promoted-async")
		self.assertEqual(image.is_active, 0)  # not provisionable until the job finishes
		self.assertTrue(image.is_local)
		# The job is enqueued to run the host half, keyed by snapshot + image name.
		call = enqueue.call_args
		self.assertEqual(
			call.args[0],
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_promote",
		)
		self.assertTrue(call.kwargs["enqueue_after_commit"])
		self.assertEqual(call.kwargs["snapshot_name"], snapshot.name)
		self.assertEqual(call.kwargs["image_name"], "promoted-async")

	def test_run_promote_runs_dd_and_activates(self) -> None:
		# The background job runs promote-snapshot-image.py on the snapshot's server
		# with the snapshot's LV path as the dd source, then flips the row active.
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "promoted-v1"})
		with patch("frappe.enqueue"):
			image_name = snapshot.promote_to_image("promoted-v1")

		with patch.object(
			module,
			"run_task",
			return_value=fake_task(
				stdout='ATLAS_RESULT={"image_lv": "atlas-image-promoted-v1", "size_bytes": 4096}'
			),
		) as mocked:
			module.run_promote(snapshot.name, image_name)

		self.assertEqual(mocked.call_args.kwargs["script"], "promote-snapshot-image")
		self.assertEqual(mocked.call_args.kwargs["server"], snapshot.server)
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)
		self.assertEqual(variables["IMAGE_NAME"], "promoted-v1")
		self.assertEqual(variables["DISK_GIGABYTES"], str(snapshot.disk_gigabytes))
		self.assertEqual(variables["ROOTFS_FILENAME"], "atlas-image-promoted-v1")
		# Kernel is reused from the snapshot's source image (already on the server).
		self.assertEqual(variables["SOURCE_IMAGE"], snapshot.source_image)
		source = frappe.get_doc("Virtual Machine Image", snapshot.source_image)
		self.assertEqual(variables["KERNEL_FILENAME"], source.kernel_filename)

		# A local (URL-less) image row, kernel inherited from the snapshot's source
		# image, rootfs_filename = the promoted LV name — and now ACTIVE.
		image = frappe.get_doc("Virtual Machine Image", image_name)
		self.assertEqual(image.name, "promoted-v1")
		self.assertEqual(image.kernel_url, "")
		self.assertEqual(image.rootfs_url, "")
		self.assertEqual(image.rootfs_filename, "atlas-image-promoted-v1")
		self.assertEqual(image.default_disk_gigabytes, snapshot.disk_gigabytes)
		self.assertEqual(image.kernel_filename, source.kernel_filename)
		self.assertTrue(image.is_local)
		self.assertEqual(image.is_active, 1)

	def test_run_promote_deletes_image_row_when_host_dd_fails(self) -> None:
		# The host dd raising (SSH error, non-zero exit, timeout) must leave NO image
		# row behind — not even the inactive anchor. Otherwise a retry collides with a
		# ghost row, and (the old bug) a survivor with is_active=1 would be provisioned
		# from despite its LV never being written.
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "promoted-doomed"})
		with patch("frappe.enqueue"):
			image_name = snapshot.promote_to_image("promoted-doomed")
		self.assertTrue(frappe.db.exists("Virtual Machine Image", image_name))  # inactive anchor

		with patch.object(module, "run_task", side_effect=frappe.ValidationError("host dd blew up")):
			with self.assertRaises(frappe.ValidationError):
				module.run_promote(snapshot.name, image_name)
		self.assertFalse(
			frappe.db.exists("Virtual Machine Image", "promoted-doomed"),
			"a failed promote must leave no image row",
		)

	def test_promote_skips_sync_fanout(self) -> None:
		# A promoted (local) image must NOT enqueue a sync Task on after_insert —
		# its bytes are an LV already on its server, nothing to download. (promote
		# itself enqueues run_promote for the host dd; that is the only allowed enqueue.)
		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "promoted-nosync"})
		with patch("frappe.enqueue") as enqueue:
			snapshot.promote_to_image("promoted-nosync")

		enqueued = [c.args[0] for c in enqueue.call_args_list]
		self.assertEqual(
			enqueued,
			["atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_promote"],
			"only the promote host-job may be enqueued — no image sync fan-out",
		)

	def test_promote_rejects_warm_snapshot(self) -> None:
		# A warm snapshot's value is its frozen memory pair — promoting it would
		# discard that. Rejected before any host work (no run_task call).
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		vm = _stopped_vm()
		warm = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "warm-golden",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Available",
				"kind": "Warm",
				"source_image": vm.image,
				"disk_gigabytes": 2,
				"rootfs_path": "/dev/atlas/atlas-snap-warm",
				"memory_directory": "/var/lib/atlas/snapshots/warm-golden",
			}
		).insert(ignore_permissions=True)
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				warm.promote_to_image("from-warm")
		self.assertIn("warm snapshot cannot be promoted", str(raised.exception).lower())
		mocked.assert_not_called()

	def test_promote_rejects_data_disk_snapshot(self) -> None:
		# Promote is root-only: a base image has no data-disk fields, so promoting a
		# data-disk snapshot would silently drop the data. Reject loudly before any
		# host work — clone instead to keep the data disk.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		source = _new_vm(data_disk_gigabytes=2)
		source.db_set("status", "Stopped")
		source.reload()
		with patch.object(
			vm_module,
			"run_task",
			return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1, "data_size_bytes": 2}'),
		):
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source.snapshot("snap-with-data"))
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.promote_to_image("from-data-disk")
		self.assertIn("data disk", str(raised.exception))
		mocked.assert_not_called()

	def test_promote_rejects_snapshot_without_source_image(self) -> None:
		# source_image is how the promoted image inherits its kernel; a row without
		# one (a malformed/legacy snapshot) can't be promoted.
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		snapshot.db_set("source_image", None)
		snapshot.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.promote_to_image("from-no-source")
		self.assertIn("source image", str(raised.exception))
		mocked.assert_not_called()

	def test_promote_rejects_source_image_without_kernel(self) -> None:
		# The promoted image reuses the source image's kernel; a source image with no
		# kernel_filename has nothing to inherit. Use a DEDICATED kernel-less source
		# image (not a mutation of the shared one) so this test can't leak into others.
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "kernel-less-image"})
		kernel_less = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": "kernel-less-image",
				"title": "kernel-less",
				"kernel_filename": "",
				"rootfs_filename": "atlas-image-kernel-less-image",
				"default_disk_gigabytes": 4,
				"is_active": 1,
			}
		)
		kernel_less.flags.ignore_mandatory = True  # kernel_filename is reqd; we want it empty here
		kernel_less.insert(ignore_permissions=True)
		snapshot.db_set("source_image", kernel_less.name)
		snapshot.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.promote_to_image("from-no-kernel")
		self.assertIn("kernel_filename", str(raised.exception))
		mocked.assert_not_called()

	def test_promote_rejects_unavailable_snapshot(self) -> None:
		vm = _stopped_vm()
		pending = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "pending-snap",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Pending",
				"source_image": vm.image,
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			pending.promote_to_image("from-pending")
		self.assertIn("not Available", str(raised.exception))

	def test_promote_rejects_invalid_image_name(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.promote_to_image("Has Spaces!")
		self.assertIn("invalid", str(raised.exception).lower())
		mocked.assert_not_called()

	def test_promote_rejects_duplicate_image_name(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module
		from atlas.tests.fixtures import make_image

		make_image("already-taken")
		snapshot = _make_snapshot(_stopped_vm())
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.promote_to_image("already-taken")
		self.assertIn("already exists", str(raised.exception))
		mocked.assert_not_called()

	def test_promote_requires_typed_result_line(self) -> None:
		# A truncated/failed Task with no ATLAS_RESULT line fails loud in run_promote.
		# The except handler deletes the inactive anchor, so promote is all-or-nothing
		# (no row pointing at an LV the failed Task never finished) — without relying on
		# a request rollback.
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "promote-truncated"})
		with patch("frappe.enqueue"):
			image_name = snapshot.promote_to_image("promote-truncated")
		with patch.object(module, "run_task", return_value=fake_task(stdout="no marker here")):
			with self.assertRaises(ValueError):
				module.run_promote(snapshot.name, image_name)
		self.assertFalse(frappe.db.exists("Virtual Machine Image", "promote-truncated"))

	def test_on_trash_skips_when_no_rootfs_path(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		vm = _stopped_vm()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "incomplete",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		with patch.object(module, "run_task") as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		mocked.assert_not_called()


class TestBuildModeCarry(IntegrationTestCase):
	"""The bench bake mode (site/admin) rides build VM → snapshot → clone, so a
	customer VM's first-boot deploy maps its FQDN to the baked site or the admin
	console. An ordinary VM (no mode) stays empty all the way through."""

	def setUp(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		_ensure_test_server()
		_ensure_test_image()
		with patch.object(module, "run_task", return_value=fake_task()):
			for name in frappe.get_all("Virtual Machine Snapshot", pluck="name"):
				frappe.delete_doc("Virtual Machine Snapshot", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_snapshot_copies_admin_mode_from_vm(self) -> None:
		vm = _new_vm(build_mode="admin")
		vm.db_set("status", "Stopped")
		vm.reload()
		snapshot = _make_snapshot(vm)
		self.assertEqual(snapshot.build_mode, "admin")

	def test_clone_carries_admin_mode(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		vm = _new_vm(build_mode="admin")
		vm.db_set("status", "Stopped")
		vm.reload()
		snapshot = _make_snapshot(vm)
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="admin clone", ssh_public_key="ssh-ed25519 A")
		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertEqual(clone.build_mode, "admin")

	def test_ordinary_vm_has_no_mode_through_the_chain(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		vm = _stopped_vm()  # no build_mode
		self.assertFalse(vm.build_mode)
		snapshot = _make_snapshot(vm)
		self.assertFalse(snapshot.build_mode)
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="plain clone", ssh_public_key="ssh-ed25519 P")
		self.assertFalse(frappe.get_doc("Virtual Machine", clone_name).build_mode)

	def test_promote_carries_admin_mode_onto_image_and_vm(self) -> None:
		# The promote→image path's equivalent of test_clone_carries_admin_mode: an
		# admin snapshot promotes to an image carrying build_mode=admin, and a VM
		# created from that image via the ordinary `image` field inherits it (so its
		# first-boot deploy maps the FQDN to the admin console). spec/08.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		vm = _new_vm(build_mode="admin")
		vm.db_set("status", "Stopped")
		vm.reload()
		snapshot = _make_snapshot(vm)
		frappe.db.delete("Virtual Machine Image", {"image_name": "promoted-admin"})
		with patch.object(
			module,
			"run_task",
			return_value=fake_task(
				stdout='ATLAS_RESULT={"image_lv": "atlas-image-promoted-admin", "size_bytes": 4096}'
			),
		):
			image_name = snapshot.promote_to_image("promoted-admin")
		self.assertEqual(frappe.db.get_value("Virtual Machine Image", image_name, "build_mode"), "admin")
		# A VM created from the admin image inherits the mode without restating it.
		with patch.object(vm_module.frappe, "enqueue"):
			vm_from_image = frappe.get_doc(
				{
					"doctype": "Virtual Machine",
					"title": "from-admin-image",
					"server": vm.server,
					"image": image_name,
					"vcpus": 1,
					"memory_megabytes": 512,
					"disk_gigabytes": frappe.db.get_value(
						"Virtual Machine Image", image_name, "default_disk_gigabytes"
					),
					"ssh_public_key": "ssh-ed25519 A",
				}
			).insert(ignore_permissions=True)
		self.assertEqual(vm_from_image.build_mode, "admin")

	def test_promote_ordinary_snapshot_makes_mode_less_image(self) -> None:
		# An ordinary (no-mode) snapshot promotes to an image with no build_mode, and a
		# VM from it stays mode-less (→ site default). The image-inherit path must not
		# invent a mode where the golden had none.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		frappe.db.delete("Virtual Machine Image", {"image_name": "promoted-plain"})
		with patch.object(
			module,
			"run_task",
			return_value=fake_task(
				stdout='ATLAS_RESULT={"image_lv": "atlas-image-promoted-plain", "size_bytes": 4096}'
			),
		):
			image_name = snapshot.promote_to_image("promoted-plain")
		self.assertFalse(frappe.db.get_value("Virtual Machine Image", image_name, "build_mode"))
		with patch.object(vm_module.frappe, "enqueue"):
			vm_from_image = frappe.get_doc(
				{
					"doctype": "Virtual Machine",
					"title": "from-plain-image",
					"server": snapshot.server,
					"image": image_name,
					"vcpus": 1,
					"memory_megabytes": 512,
					"disk_gigabytes": frappe.db.get_value(
						"Virtual Machine Image", image_name, "default_disk_gigabytes"
					),
					"ssh_public_key": "ssh-ed25519 P",
				}
			).insert(ignore_permissions=True)
		self.assertFalse(vm_from_image.build_mode)


class TestSnapshotS3Backup(IntegrationTestCase):
	"""Upload a snapshot's artifacts to S3 and restore them back
	(spec/29-snapshot-backup.md). The host Task and the boto3 presign are both
	mocked — these cover the controller's guards, background-job wiring, manifest
	recording, the cold-rollback vs. warm-rehydrate-only split, and on_trash S3
	cleanup, none of which need a host or real S3."""

	def setUp(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		_ensure_test_server()
		_ensure_test_image()
		with patch.object(module, "run_task", return_value=fake_task()):
			for name in frappe.get_all("Virtual Machine Snapshot", pluck="name"):
				frappe.delete_doc("Virtual Machine Snapshot", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _configure_s3(self) -> None:
		frappe.get_doc("S3 Settings").setup(
			bucket="atlas-backups-test",
			access_key_id="A",
			secret_access_key="S",
			key_prefix="atlas/snapshots",
		)

	def _uploaded_cold_snapshot(self):
		"""An Available cold snapshot marked Uploaded, with a one-object manifest —
		the shape restore_from_s3 / _run_restore consume."""
		snapshot = _make_snapshot(_stopped_vm())
		manifest = [
			{
				"name": "rootfs",
				"object_name": "rootfs.img.zst",
				"source": snapshot.rootfs_path,
				"block": True,
				"compress": True,
				"disk_gigabytes": snapshot.disk_gigabytes,
				"sha256": "deadbeef",
				"compressed_bytes": 100,
				"raw_bytes": 1000,
			}
		]
		snapshot.db_set({"s3_status": "Uploaded", "s3_objects": json.dumps(manifest)})
		snapshot.reload()
		return snapshot

	# --- upload guards + wiring ---

	def test_upload_rejects_unavailable_snapshot(self) -> None:
		vm = _stopped_vm()
		pending = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "pending-upload",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			pending.upload_to_s3()
		self.assertIn("not Available", str(raised.exception))

	def test_upload_requires_s3_config(self) -> None:
		# S3 Settings left unconfigured — upload must refuse before any job.
		frappe.db.set_single_value("S3 Settings", "bucket", "")
		snapshot = _make_snapshot(_stopped_vm())
		with patch("frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.upload_to_s3()
		self.assertIn("not configured", str(raised.exception))
		enqueue.assert_not_called()

	def test_upload_enqueues_background_job(self) -> None:
		self._configure_s3()
		snapshot = _make_snapshot(_stopped_vm())
		with patch("frappe.enqueue") as enqueue:
			snapshot.upload_to_s3()
		snapshot.reload()
		self.assertEqual(snapshot.s3_status, "Uploading")
		call = enqueue.call_args
		self.assertEqual(
			call.args[0],
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_upload",
		)
		self.assertTrue(call.kwargs["enqueue_after_commit"])
		self.assertEqual(call.kwargs["snapshot_name"], snapshot.name)

	def test_run_upload_records_manifest(self) -> None:
		from atlas.atlas import s3
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		self._configure_s3()
		snapshot = _make_snapshot(_stopped_vm())
		result = (
			'ATLAS_RESULT={"objects": [{"name": "rootfs", "object_name": "rootfs.img.zst", '
			'"sha256": "deadbeef", "compressed_bytes": 100, "raw_bytes": 1000}], '
			'"total_compressed_bytes": 100}'
		)
		with (
			patch.object(s3.S3Backup, "presign_put", return_value="https://signed-put"),
			patch.object(module, "run_task", return_value=fake_task(stdout=result)) as mocked,
		):
			snapshot._run_upload()

		snapshot.reload()
		self.assertEqual(snapshot.s3_status, "Uploaded")
		self.assertEqual(snapshot.s3_bucket, "atlas-backups-test")
		self.assertEqual(snapshot.s3_key_prefix, f"atlas/snapshots/{snapshot.name}/")
		self.assertEqual(snapshot.s3_size_bytes, 100)
		manifest = json.loads(snapshot.s3_objects)
		self.assertEqual(manifest[0]["sha256"], "deadbeef")
		self.assertEqual(manifest[0]["source"], snapshot.rootfs_path)
		self.assertEqual(manifest[0]["disk_gigabytes"], snapshot.disk_gigabytes)
		# The stored manifest must NOT leak the time-limited presigned url.
		self.assertNotIn("url", manifest[0])
		# The host Task got the plan with the presigned PUT url.
		self.assertEqual(mocked.call_args.kwargs["script"], "upload-snapshot-s3")
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["SNAPSHOT_NAME"], snapshot.name)
		plan = json.loads(variables["OBJECTS_JSON"])
		self.assertEqual(plan[0]["url"], "https://signed-put")

	def test_run_upload_failure_sets_failed(self) -> None:
		from atlas.atlas import s3
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		self._configure_s3()
		snapshot = _make_snapshot(_stopped_vm())
		with (
			patch.object(s3.S3Backup, "presign_put", return_value="https://signed-put"),
			patch.object(module, "run_task", side_effect=frappe.ValidationError("host blew up")),
		):
			with self.assertRaises(frappe.ValidationError):
				snapshot._run_upload()
		snapshot.reload()
		self.assertEqual(snapshot.s3_status, "Failed")

	# --- restore guards + wiring ---

	def test_restore_rejects_when_not_uploaded(self) -> None:
		snapshot = _make_snapshot(_stopped_vm())
		with self.assertRaises(frappe.ValidationError) as raised:
			snapshot.restore_from_s3()
		self.assertIn("No S3 backup", str(raised.exception))

	def test_restore_cold_requires_stopped_vm(self) -> None:
		snapshot = self._uploaded_cold_snapshot()
		frappe.db.set_value("Virtual Machine", snapshot.virtual_machine, "status", "Running")
		with patch("frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.ValidationError) as raised:
				snapshot.restore_from_s3()
		self.assertIn("Stop", str(raised.exception))
		enqueue.assert_not_called()

	def test_restore_enqueues_background_job(self) -> None:
		snapshot = self._uploaded_cold_snapshot()
		with patch("frappe.enqueue") as enqueue:
			snapshot.restore_from_s3()
		snapshot.reload()
		self.assertEqual(snapshot.s3_status, "Restoring")
		call = enqueue.call_args
		self.assertEqual(
			call.args[0],
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_restore",
		)
		self.assertEqual(call.kwargs["snapshot_name"], snapshot.name)

	def test_run_restore_cold_rehydrates_then_rolls_back(self) -> None:
		from atlas.atlas import s3
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		self._configure_s3()
		snapshot = self._uploaded_cold_snapshot()
		with (
			patch.object(s3.S3Backup, "presign_get", return_value="https://signed-get"),
			patch.object(
				module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"objects": ["rootfs"]}')
			) as rehydrate,
			patch.object(vm_module, "run_task", return_value=fake_task()) as rollback,
		):
			task_name = snapshot._run_restore()

		# Rehydrate first (restore-snapshot-s3), then the in-place rollback (rebuild-vm).
		self.assertEqual(rehydrate.call_args.kwargs["script"], "restore-snapshot-s3")
		self.assertEqual(rollback.call_args.kwargs["script"], "rebuild-vm")
		self.assertTrue(task_name)  # the rollback Task name is returned
		snapshot.reload()
		self.assertEqual(snapshot.s3_status, "Uploaded")  # the backup stays in S3
		# The rehydrate Task got the presigned GET url + the manifest sha256.
		plan = json.loads(rehydrate.call_args.kwargs["variables"]["OBJECTS_JSON"])
		self.assertEqual(plan[0]["url"], "https://signed-get")
		self.assertEqual(plan[0]["sha256"], "deadbeef")

	def test_run_restore_warm_rehydrates_only(self) -> None:
		from atlas.atlas import s3
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		self._configure_s3()
		vm = _stopped_vm()
		manifest = [
			{
				"name": "rootfs",
				"object_name": "rootfs.img.zst",
				"source": "/dev/atlas/atlas-snap-warm",
				"block": True,
				"compress": True,
				"disk_gigabytes": 2,
				"sha256": "d",
				"compressed_bytes": 1,
				"raw_bytes": 2,
			}
		]
		warm = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "warm-uploaded",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Available",
				"kind": "Warm",
				"source_image": vm.image,
				"disk_gigabytes": 2,
				"rootfs_path": "/dev/atlas/atlas-snap-warm",
				"memory_directory": "/var/lib/atlas/snapshots/warm-uploaded",
				"s3_status": "Uploaded",
				"s3_objects": json.dumps(manifest),
			}
		).insert(ignore_permissions=True)

		with (
			patch.object(s3.S3Backup, "presign_get", return_value="https://signed-get"),
			patch.object(
				module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"objects": ["rootfs"]}')
			) as rehydrate,
			patch.object(vm_module, "run_task") as rollback,
		):
			result = warm._run_restore()

		# Warm rehydrates only — no in-place rollback (consume it via Clone to new VM).
		self.assertIsNone(result)
		self.assertEqual(rehydrate.call_args.kwargs["script"], "restore-snapshot-s3")
		rollback.assert_not_called()
		warm.reload()
		self.assertEqual(warm.s3_status, "Uploaded")

	# --- on_trash S3 cleanup ---

	def test_on_trash_deletes_s3_backup(self) -> None:
		from atlas.atlas import s3
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		self._configure_s3()
		snapshot = self._uploaded_cold_snapshot()
		with (
			patch.object(s3.S3Backup, "delete_prefix", return_value=1) as deleter,
			patch.object(module, "run_task", return_value=fake_task()),
		):
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		deleter.assert_called_once_with(snapshot.name)

	def test_on_trash_skips_s3_when_not_uploaded(self) -> None:
		from atlas.atlas import s3
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())  # s3_status empty
		with (
			patch.object(s3.S3Backup, "delete_prefix") as deleter,
			patch.object(module, "run_task", return_value=fake_task()),
		):
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		deleter.assert_not_called()
