"""Unit tests for atlas.atlas.core.fleet_image — fleet distribution of a promoted
snapshot as a NON-LOCAL base image (rootfs squashfs → S3 → sync-image fan-out;
the kernel is inherited from the source image, not re-uploaded).

`_produce_and_upload_rootfs` (the only host-touching step) is monkeypatched away —
no SSH, no live hosts. S3 is a stub (fixed key prefix + a public-url stub + no-op
make_public, so the minted image row passes VirtualMachineImage.validate), and
`frappe.db.commit` / `frappe.enqueue` are patched so the sync Tasks'
commit-before-enqueue pattern cannot leak rows past the test transaction or pile
jobs onto the `long` queue. See spec/08-images.md, spec/29-snapshot-backup.md.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.core import fleet_image
from atlas.tests import fixtures
from atlas.tests.fixtures import no_commit_enqueue

ROOTFS_SHA256 = "a" * 64
KERNEL_SHA256 = "b" * 64
SOURCE_KERNEL_URL = "https://cloud-images.example.com/vmlinux-chef-test"


class _StubS3Backup:
	"""Stand-in for s3.S3Backup: a fixed key prefix, a presign-put stub, a no-op
	make_public and a short plain public-url (so the row's 140-char url fits)."""

	key_prefix = "atlas/snapshots"

	def presign_put(self, key: str) -> str:
		return "https://example.com/put/" + key

	def make_public(self, key: str) -> None:
		return None

	def public_url(self, key: str) -> str:
		return "https://nyc3.example.com/bucket/" + key


class TestPublishSnapshotAsFleetImage(IntegrationTestCase):
	def test_publish_mints_non_local_image_and_fans_out(self) -> None:
		# image_name is the VMI docname (autoname field:image_name), so a row leaked
		# by a crashed run would collide with the mint — clear any first. Plain
		# db.delete (no hooks): VirtualMachineImage has no on_trash to worry about.
		frappe.db.delete(
			"Virtual Machine Image", {"image_name": ("in", ["chef-source-image", "chef-testimg"])}
		)

		provider = fixtures.make_provider("fleet-test-provider")
		server = fixtures.make_server(
			provider,
			title="fleet-test-server",
			status="Active",
			ipv4_address="203.0.113.50",
			ipv6_address="2001:db8:abcd::1",
			ipv6_prefix="2001:db8:abcd::/64",
			ipv6_virtual_machine_range="2001:db8:abcd::/124",
		)
		source_image = fixtures.make_image(
			"chef-source-image",
			kernel_filename="vmlinux-chef-test",
			build_mode="site",
		)
		# The distributed image reuses the source's kernel url + digest verbatim.
		source_image.db_set("kernel_url", SOURCE_KERNEL_URL)
		source_image.db_set("kernel_sha256", KERNEL_SHA256)

		with (
			no_commit_enqueue() as enqueue,
			patch.object(fleet_image, "_produce_and_upload_rootfs", return_value=ROOTFS_SHA256),
			patch("atlas.atlas.core.s3.is_configured", return_value=True),
			patch("atlas.atlas.core.s3.S3Backup", _StubS3Backup),
		):
			vm = fixtures.make_virtual_machine(server, source_image, title="fleet-test-vm")
			snapshot = frappe.get_doc(
				{
					"doctype": "Virtual Machine Snapshot",
					"title": "fleet test snapshot",
					"virtual_machine": vm.name,
					"server": server.name,
					"source_image": source_image.name,
					"status": "Available",
					"kind": "Cold",
					"disk_gigabytes": 8,
					"rootfs_path": "/dev/atlas/atlas-snap-fleet-test",
				}
			).insert(ignore_permissions=True)

			result = fleet_image.publish_snapshot_as_fleet_image(
				snapshot.name, "chef-testimg", servers=[server.name]
			)

		image = frappe.get_doc("Virtual Machine Image", "chef-testimg")
		self.assertEqual(image.is_active, 1)
		self.assertFalse(image.is_local)  # rootfs_url set -> non-local, syncable
		self.assertEqual(image.rootfs_sha256, ROOTFS_SHA256)
		# kernel is inherited verbatim from the source image
		self.assertEqual(image.kernel_sha256, KERNEL_SHA256)
		self.assertEqual(image.kernel_url, SOURCE_KERNEL_URL)
		self.assertEqual(image.kernel_filename, "vmlinux-chef-test")
		self.assertEqual(image.rootfs_filename, "chef-testimg.ext4")
		self.assertEqual(image.default_disk_gigabytes, 8)
		self.assertEqual(image.build_mode, "site")
		# the rootfs is a plain (public) https url carrying the fleet-images key layout
		self.assertTrue(image.rootfs_url.startswith("https://"))
		self.assertIn("fleet-images/chef-testimg/rootfs.sqfs", image.rootfs_url)

		self.assertEqual(result["image"], "chef-testimg")
		self.assertTrue(result["tasks"])
		# the explicit fan-out went to exactly the requested server, via sync-image
		self.assertEqual(len(result["tasks"]), 1)
		task = frappe.get_doc("Task", result["tasks"][0])
		self.assertEqual(task.server, server.name)
		self.assertEqual(task.script, "sync-image")
		enqueue.assert_called()
