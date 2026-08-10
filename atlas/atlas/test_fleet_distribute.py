"""Unit tests for atlas.atlas.fleet_distribute — fanning a LOCAL base image out to
the fleet over HTTP (squash on the home host → serve over the mesh → sync-image
fan-out; the kernel is inherited from the promote provenance, not transferred).

The two host-touching steps (`_serve_rootfs_over_http`, `_teardown_http_server`) and
the blocking poll are monkeypatched away — no SSH, no live hosts, no sleeps. The
promote provenance is a real `promote-snapshot-image` Task row (the same trail
`_image_home_server`/`_source_image_kernel` read), and `frappe.db.commit` /
`frappe.enqueue` are patched so the sync Task's commit-before-enqueue can't leak rows
past the test transaction. See spec/08-images.md § Distributing a local image over HTTP.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import fleet_distribute
from atlas.tests import fixtures
from atlas.tests.fixtures import no_commit_enqueue

ROOTFS_SHA256 = "c" * 64
KERNEL_SHA256 = "d" * 64
SOURCE_KERNEL_URL = "https://cloud-images.example.com/vmlinux-distribute-test"


def _make_local_image(name: str) -> "frappe.model.document.Document":
	"""A URL-less (local, snapshot-promoted) image row: kernel/rootfs filenames set,
	every download field empty — exactly what promote_to_image inserts."""
	return frappe.get_doc(
		{
			"doctype": "Virtual Machine Image",
			"image_name": name,
			"title": f"{name} (local test image)",
			"kernel_url": "",
			"kernel_filename": "vmlinux-distribute-test",
			"kernel_sha256": "",
			"rootfs_url": "",
			"rootfs_filename": f"atlas-image-{name}",
			"rootfs_sha256": "",
			"default_disk_gigabytes": 8,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)


def _make_promote_task(image: str, source_image: str, server: str) -> None:
	"""The promote Task trail fleet_distribute resolves the home host + kernel from."""
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"server": server,
			"script": "promote-snapshot-image",
			"status": "Success",
			"triggered_by": "Administrator",
		}
	)
	task.variables_dict = {"IMAGE_NAME": image, "SOURCE_IMAGE": source_image}
	task.insert(ignore_permissions=True)


class TestDistributeLocalImage(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete(
			"Virtual Machine Image",
			{"image_name": ("in", ["distribute-source-image", "distribute-localimg"])},
		)
		self.provider = fixtures.make_provider("distribute-test-provider")
		self.source = fixtures.make_server(
			self.provider, title="distribute-source", status="Active", ipv4_address="203.0.113.60"
		)
		self.target = fixtures.make_server(
			self.provider, title="distribute-target", status="Active", ipv4_address="203.0.113.61"
		)
		self.source_image = fixtures.make_image(
			"distribute-source-image", kernel_filename="vmlinux-distribute-test"
		)
		self.source_image.db_set("kernel_url", SOURCE_KERNEL_URL)
		self.source_image.db_set("kernel_sha256", KERNEL_SHA256)
		self.image = _make_local_image("distribute-localimg")
		_make_promote_task("distribute-localimg", self.source_image.name, self.source.name)

	def test_run_distribution_serves_and_fans_out_sync_image(self) -> None:
		with (
			no_commit_enqueue() as enqueue,
			patch.object(
				fleet_distribute, "_serve_rootfs_over_http", return_value=ROOTFS_SHA256
			) as serve,
			patch.object(fleet_distribute, "_teardown_http_server") as teardown,
			patch.object(fleet_distribute, "_poll_tasks_to_terminal", return_value={}),
		):
			fleet_distribute._run_distribution("distribute-localimg", [self.target.name])

		# Squashed + served on the home host, torn down in the finally.
		serve.assert_called_once()
		self.assertEqual(serve.call_args.args[0], self.source.name)
		self.assertEqual(serve.call_args.args[1], "distribute-localimg")
		teardown.assert_called_once()
		enqueue.assert_called()

		# Exactly one sync-image Task, to the target, with the mesh HTTP URL, the computed
		# rootfs digest, the inherited kernel, and the LV-named rootfs filename.
		tasks = frappe.get_all(
			"Task",
			filters={"server": self.target.name, "script": "sync-image"},
			pluck="name",
		)
		self.assertEqual(len(tasks), 1)
		variables = frappe.get_doc("Task", tasks[0]).variables_dict
		self.assertEqual(variables["IMAGE_NAME"], "distribute-localimg")
		self.assertEqual(variables["ROOTFS_SHA256"], ROOTFS_SHA256)
		self.assertEqual(variables["KERNEL_URL"], SOURCE_KERNEL_URL)
		self.assertEqual(variables["KERNEL_SHA256"], KERNEL_SHA256)
		self.assertEqual(variables["KERNEL_FILENAME"], "vmlinux-distribute-test")
		self.assertEqual(variables["ROOTFS_FILENAME"], "atlas-image-distribute-localimg")
		self.assertTrue(variables["ROOTFS_URL"].startswith("http://["))
		self.assertTrue(variables["ROOTFS_URL"].endswith("/rootfs.sqfs"))

	def test_distribute_enqueues_and_drops_the_source_host(self) -> None:
		with no_commit_enqueue() as enqueue:
			result = fleet_distribute.distribute_local_image("distribute-localimg")

		self.assertEqual(result["image"], "distribute-localimg")
		self.assertEqual(result["source"], self.source.name)
		# Default targets = every Active host minus the source (which already holds it).
		self.assertIn(self.target.name, result["servers"])
		self.assertNotIn(self.source.name, result["servers"])
		enqueue.assert_called()

	def test_distribute_rejects_a_from_url_image(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			fleet_distribute.distribute_local_image(self.source_image.name)

	def test_distribute_rejects_a_missing_image(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			fleet_distribute.distribute_local_image("no-such-image")

	def test_image_http_port_is_stable_and_disjoint_from_nbd(self) -> None:
		port = fleet_distribute.image_http_port("distribute-localimg")
		self.assertEqual(port, fleet_distribute.image_http_port("distribute-localimg"))
		self.assertGreaterEqual(port, 26000)
		self.assertLess(port, 28000)
