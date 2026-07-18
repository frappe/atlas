import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.script_uploads import files_to_upload
from atlas.tests.fixtures import make_image, make_provider, make_server, no_commit_enqueue


def _provider_and_server(title: str, status: str) -> str:
	"""Ensure a Server row with the given title exists. Returns its UUID `name`."""
	provider = make_provider("test-provider-image")
	server = make_server(provider, title, status=status)
	return server.name


class TestVirtualMachineImage(IntegrationTestCase):
	def setUp(self) -> None:
		self.image = make_image()

	def test_validate_urls_https(self) -> None:
		bad = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": "bad-image",
				"kernel_url": "http://example.com/vmlinux",
				"kernel_filename": "vmlinux-1.0",
				"kernel_sha256": "a" * 64,
				"rootfs_url": "https://example.com/rootfs.squashfs",
				"rootfs_filename": "rootfs.ext4",
				"rootfs_sha256": "b" * 64,
				"default_disk_gigabytes": 4,
				"is_active": 1,
			}
		)
		with self.assertRaises(frappe.ValidationError):
			bad.insert(ignore_permissions=True)

	def test_sync_to_server_enqueues_task(self) -> None:
		server_name = _provider_and_server("test-srv-sync", "Active")
		with no_commit_enqueue() as enqueue:
			task_name = self.image.sync_to_server(server_name)
		enqueue.assert_called_once()
		task = frappe.get_doc("Task", task_name)
		self.assertEqual(task.status, "Pending")
		self.assertEqual(task.script, "sync-image")
		self.assertEqual(task.server, server_name)

	def test_sync_to_all_servers_enqueues_one_per_active(self) -> None:
		active_name = _provider_and_server("srv-active-1", "Active")
		_provider_and_server("srv-broken-1", "Broken")
		_provider_and_server("srv-archived-1", "Archived")
		with no_commit_enqueue() as enqueue:
			tasks = self.image.sync_to_all_servers()
		# Active servers are: srv-active-1 plus any previous Active servers
		# from other tests; we filter to the ones we just created.
		our_tasks = [t for t in tasks if frappe.db.get_value("Task", t, "server") == active_name]
		self.assertEqual(len(our_tasks), 1)
		# enqueue called once per Active server in the system (>=1 from ours).
		self.assertGreaterEqual(enqueue.call_count, 1)

	def test_files_to_upload_for_sync_image(self) -> None:
		uploads = files_to_upload("sync-image")
		self.assertTrue(any("atlas-network.service" in remote for _, remote in uploads))


class TestVirtualMachineImageAutoSync(IntegrationTestCase):
	def test_after_insert_enqueues_one_task_per_active_server(self) -> None:
		# Two Active servers + one Broken server + one Archived: only the
		# two Active should get a sync task.
		active_1 = _provider_and_server("auto-srv-active-1", "Active")
		active_2 = _provider_and_server("auto-srv-active-2", "Active")
		_provider_and_server("auto-srv-broken", "Broken")

		# Reset image name to ensure a fresh insert.
		frappe.db.delete("Virtual Machine Image", {"image_name": "auto-sync-image"})
		with no_commit_enqueue() as enqueue:
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Image",
					"image_name": "auto-sync-image",
					"title": "auto sync image",
					"kernel_url": "https://example.com/k",
					"kernel_filename": "k",
					"kernel_sha256": "a" * 64,
					"rootfs_url": "https://example.com/r",
					"rootfs_filename": "r",
					"rootfs_sha256": "b" * 64,
					"default_disk_gigabytes": 4,
					"is_active": 1,
				}
			).insert(ignore_permissions=True)

		# Inserts a sync Task per Active server we just created.
		our_tasks = frappe.get_all(
			"Task",
			filters={
				"script": "sync-image",
				"server": ("in", [active_1, active_2]),
			},
			pluck="name",
		)
		self.assertGreaterEqual(len(our_tasks), 2)
		# enqueue called once per Task insert (execute_task background worker).
		self.assertGreaterEqual(enqueue.call_count, 2)

	def test_after_insert_skips_when_inactive(self) -> None:
		_provider_and_server("inactive-srv-1", "Active")
		frappe.db.delete("Virtual Machine Image", {"image_name": "inactive-image"})
		with no_commit_enqueue() as enqueue:
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Image",
					"image_name": "inactive-image",
					"title": "inactive image",
					"kernel_url": "https://example.com/k",
					"kernel_filename": "k",
					"kernel_sha256": "a" * 64,
					"rootfs_url": "https://example.com/r",
					"rootfs_filename": "r",
					"rootfs_sha256": "b" * 64,
					"default_disk_gigabytes": 4,
					"is_active": 0,
				}
			).insert(ignore_permissions=True)
		# No syncs enqueued when is_active=0.
		self.assertEqual(enqueue.call_count, 0)


class TestShippedImageConstants(IntegrationTestCase):
	"""The DEFAULT_IMAGE/MINIMAL_IMAGE dicts operators copy into the form (and
	bootstrap.run inserts) must be well-formed: https URLs, 64-hex digests, and
	insertable through the same validation an operator hits. A typo in a pinned
	digest or URL is a real Phase-1 risk this catches without a bench."""

	def _assert_shaped(self, image: dict) -> None:
		for url_field in ("kernel_url", "rootfs_url"):
			self.assertTrue(image[url_field].startswith("https://"), image[url_field])
		for sha_field in ("kernel_sha256", "rootfs_sha256"):
			value = image[sha_field]
			self.assertEqual(len(value), 64, sha_field)
			int(value, 16)  # raises if not hex

	def test_constants_well_formed(self) -> None:
		from atlas.bootstrap import DEFAULT_IMAGE, MINIMAL_IMAGE

		self._assert_shaped(DEFAULT_IMAGE)
		self._assert_shaped(MINIMAL_IMAGE)
		# Two distinct image rows.
		self.assertNotEqual(DEFAULT_IMAGE["image_name"], MINIMAL_IMAGE["image_name"])
		# Distinct rootfs filenames so they don't clobber each other on a server.
		self.assertNotEqual(DEFAULT_IMAGE["rootfs_filename"], MINIMAL_IMAGE["rootfs_filename"])

	def test_bootstrap_and_config_constants_match(self) -> None:
		"""bootstrap.py and tests/e2e/_config.py pin the same bytes — drift
		between them means the operator and the e2e suite test different images."""
		from atlas.bootstrap import DEFAULT_IMAGE as B_DEFAULT
		from atlas.bootstrap import MINIMAL_IMAGE as B_MINIMAL
		from atlas.tests.e2e._config import DEFAULT_IMAGE as C_DEFAULT
		from atlas.tests.e2e._config import MINIMAL_IMAGE as C_MINIMAL

		for field in ("kernel_url", "kernel_sha256", "rootfs_url", "rootfs_sha256"):
			self.assertEqual(B_DEFAULT[field], C_DEFAULT[field], field)
			self.assertEqual(B_MINIMAL[field], C_MINIMAL[field], field)

	def test_default_constant_inserts(self) -> None:
		from atlas.bootstrap import DEFAULT_IMAGE

		frappe.db.delete("Virtual Machine Image", {"image_name": DEFAULT_IMAGE["image_name"]})
		image = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				**DEFAULT_IMAGE,
				"is_active": 0,
			}
		).insert(ignore_permissions=True)
		self.assertEqual(image.name, DEFAULT_IMAGE["image_name"])

	def test_seed_default_images_idempotent(self) -> None:
		"""The desk `seed_default_images` action inserts the canonical rows once
		and skips them on a second call — the same idempotency `ensure_image` has."""
		from atlas.atlas.doctype.virtual_machine_image.virtual_machine_image import (
			SEEDABLE_IMAGES,
			seed_default_images,
		)

		names = [image["image_name"] for image in SEEDABLE_IMAGES]
		for name in names:
			frappe.db.delete("Virtual Machine Image", {"image_name": name})

		first = seed_default_images()
		self.assertCountEqual(first["created"], names)
		self.assertEqual(first["skipped"], [])
		for name in names:
			self.assertTrue(frappe.db.exists("Virtual Machine Image", name))

		# Second call is a no-op: everything already present.
		second = seed_default_images()
		self.assertEqual(second["created"], [])
		self.assertCountEqual(second["skipped"], names)


class TestLocalImage(IntegrationTestCase):
	"""A local image — promoted from a snapshot, no rootfs URL — is non-syncable:
	its bytes are an LV already on its one server. after_insert skips the fan-out
	and sync_to_server throws cleanly (spec/08-images.md § Promoting a snapshot)."""

	def _local_image(self, name: str = "local-image") -> "frappe.model.document.Document":
		frappe.db.delete("Virtual Machine Image", {"image_name": name})
		return frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": name,
				"title": "local image",
				"kernel_url": "",
				"kernel_filename": "vmlinux-6.1",
				"kernel_sha256": "",
				"rootfs_url": "",
				"rootfs_filename": f"atlas-image-{name}",
				"rootfs_sha256": "",
				"default_disk_gigabytes": 4,
				"is_active": 1,
			}
		)

	def test_url_less_image_validates(self) -> None:
		# The plan's claim: a row with empty kernel/rootfs URLs is legal (validate
		# only enforces https when a value is present; the reqd flags are relaxed).
		image = self._local_image().insert(ignore_permissions=True)
		self.assertTrue(image.is_local)

	def test_after_insert_skips_sync_fanout_for_local(self) -> None:
		_provider_and_server("local-img-active-srv", "Active")
		with no_commit_enqueue() as enqueue:
			self._local_image("local-image-noinsert").insert(ignore_permissions=True)
		enqueue.assert_not_called()

	def test_sync_to_server_throws_for_local(self) -> None:
		server_name = _provider_and_server("local-img-sync-srv", "Active")
		image = self._local_image("local-image-nosync").insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			image.sync_to_server(server_name)
		self.assertIn("local image", str(raised.exception))

	def test_partial_url_shape_rejected_at_insert(self) -> None:
		# A URL image missing only its rootfs_sha256 used to be blocked by reqd=1;
		# now validate()'s coherence check must reject it AT INSERT, not let it fan
		# out a sync Task that fails later on the host (where the digest is required).
		frappe.db.delete("Virtual Machine Image", {"image_name": "partial-url-image"})
		bad = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": "partial-url-image",
				"title": "partial url image",
				"kernel_url": "https://example.com/k",
				"kernel_filename": "k",
				"kernel_sha256": "a" * 64,
				"rootfs_url": "https://example.com/r",
				"rootfs_filename": "r",
				"rootfs_sha256": "",  # the one missing piece
				"default_disk_gigabytes": 4,
				"is_active": 1,
			}
		)
		with self.assertRaises(frappe.ValidationError) as raised:
			bad.insert(ignore_permissions=True)
		self.assertIn("rootfs_sha256", str(raised.exception))

	def test_url_image_still_fans_out(self) -> None:
		# Regression guard: an ordinary URL-backed image still auto-syncs.
		_provider_and_server("url-img-active-srv", "Active")
		frappe.db.delete("Virtual Machine Image", {"image_name": "url-image-fanout"})
		with no_commit_enqueue() as enqueue:
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Image",
					"image_name": "url-image-fanout",
					"title": "url image",
					"kernel_url": "https://example.com/k",
					"kernel_filename": "k",
					"kernel_sha256": "a" * 64,
					"rootfs_url": "https://example.com/r",
					"rootfs_filename": "r",
					"rootfs_sha256": "b" * 64,
					"default_disk_gigabytes": 4,
					"is_active": 1,
				}
			).insert(ignore_permissions=True)
		self.assertGreaterEqual(enqueue.call_count, 1)


class TestVirtualMachineImageImmutability(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete(
			"Virtual Machine Image",
			{"image_name": "immutable-image"},
		)
		self.image = make_image("immutable-image")

	def test_kernel_url_is_immutable(self) -> None:
		self.image.kernel_url = "https://example.com/new-vmlinux"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.image.save(ignore_permissions=True)
		self.assertIn("kernel_url is immutable", str(raised.exception))

	def test_title_is_immutable(self) -> None:
		self.image.title = "renamed image"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.image.save(ignore_permissions=True)
		self.assertIn("title is immutable", str(raised.exception))

	def test_is_active_remains_editable(self) -> None:
		# is_active is the one field we don't lock; the Archive flow flips it.
		self.image.is_active = 0
		self.image.save(ignore_permissions=True)
		self.image.reload()
		self.assertEqual(self.image.is_active, 0)

	def test_archive_sets_is_active_zero(self) -> None:
		self.image.reload()
		self.image.archive()
		self.assertEqual(
			frappe.db.get_value("Virtual Machine Image", self.image.name, "is_active"),
			0,
		)
