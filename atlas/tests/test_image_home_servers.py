"""Image presence-per-server: `placement.image_home_servers` and the placement
helper that uses it, `default_server_for_image`.

A `Virtual Machine Image` is ONE fleet-wide row, but its bytes are per-server and
the row records nothing about where they landed. `image_home_servers` reconstructs
that presence from the authoritative trail:

- a URL image → servers with a successful `sync-image` Task;
- a local (snapshot-promoted) image → its promote home plus every Done export target.

`default_server_for_image` then restricts placement to those hosts, so a bench VM is
never scheduled onto a server that lacks the image's LV. These tests pin that trail
mapping and the placement restriction — no host (pure Task/export-row logic).
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.placement import (
	NoCapacityError,
	default_server_for_image,
	image_home_servers,
)
from atlas.tests.fixtures import make_image, make_provider, make_server


class TestImageHomeServers(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("atlas-imagehome-provider")
		self.addCleanup(frappe.set_user, "Administrator")
		# Per-test unique prefix: Task rows carry an image name in their `variables`
		# and the runner commits some of them, so a name shared across tests would leak
		# presence between them. Deriving every server/image name from the running test's
		# id keeps each test's Task/export trail its own. (`_testMethodName` is the
		# active test — stable within a method, distinct across them.)
		self.uid = self._testMethodName.replace("test_", "")
		# Isolate from other suites: drain every Active server and clear the export trail
		# this suite reads, so only rows THIS test creates count as presence.
		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			frappe.db.set_value("Server", name, "status", "Draining")
		for name in frappe.get_all("Virtual Machine Image Export", pluck="name"):
			frappe.delete_doc("Virtual Machine Image Export", name, force=1, ignore_permissions=True)

	def _name(self, suffix: str) -> str:
		return f"imagehome-{self.uid}-{suffix}"

	def _server(self, suffix: str) -> str:
		server = make_server(self.provider, title=self._name(suffix), status="Active")
		frappe.db.set_value("Server", server.name, "status", "Active")
		return server.name

	def _sync_task(self, image: str, server: str, status: str = "Success") -> None:
		"""A `sync-image` Task recording that `image`'s bytes reached `server` — the
		presence signal `_synced_image_home_servers` reads."""
		frappe.get_doc(
			{
				"doctype": "Task",
				"server": server,
				"script": "sync-image",
				"variables": json.dumps({"IMAGE_NAME": image}),
				"status": status,
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

	def _promote_task(self, image: str, server: str) -> None:
		"""A successful `promote-snapshot-image` Task — the home of a local image."""
		frappe.get_doc(
			{
				"doctype": "Task",
				"server": server,
				"script": "promote-snapshot-image",
				"variables": json.dumps({"IMAGE_NAME": image}),
				"status": "Success",
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

	def _local_image(self, name: str):
		"""A local image row: no rootfs/kernel URL (promoted from a snapshot)."""
		return make_image(
			name,
			rootfs_url="",
			rootfs_sha256="",
			kernel_url="",
			kernel_sha256="",
		)

	# ---- URL image: synced-server presence -------------------------------------

	def test_url_image_home_is_synced_servers_only(self) -> None:
		a = self._server("a")
		b = self._server("b")
		self._server("c")  # active but never synced → not a home
		image = make_image(self._name("url")).name
		self._sync_task(image, a)
		self._sync_task(image, b)

		self.assertEqual(image_home_servers(image), {a, b})

	def test_url_image_unsuccessful_sync_is_not_a_home(self) -> None:
		a = self._server("a")
		image = make_image(self._name("url")).name
		# A sync that failed / is still pending hasn't placed the bytes.
		self._sync_task(image, a, status="Failure")
		self.assertEqual(image_home_servers(image), set())

	# ---- local image: promote home + export targets ----------------------------

	def test_local_image_home_is_promote_plus_export_targets(self) -> None:
		home = self._server("promote")
		target = self._server("export-target")
		self._server("elsewhere")  # active, but image never shipped here
		image = self._local_image(self._name("local")).name
		self._promote_task(image, home)
		self._export_row(image, source=home, target=target, status="Done")

		self.assertEqual(image_home_servers(image), {home, target})

	def test_local_image_in_flight_export_is_not_yet_a_home(self) -> None:
		home = self._server("promote")
		target = self._server("export-target")
		image = self._local_image(self._name("local")).name
		self._promote_task(image, home)
		# Export still shipping (not Done) → target doesn't hold the bytes yet.
		self._export_row(image, source=home, target=target, status="Exporting")

		self.assertEqual(image_home_servers(image), {home})

	def test_local_image_does_not_read_sync_tasks(self) -> None:
		# A local image is non-syncable; a stray sync-image Task must not fake presence.
		home = self._server("promote")
		stray = self._server("stray")
		image = self._local_image(self._name("local")).name
		self._promote_task(image, home)
		self._sync_task(image, stray)  # would be a home for a URL image, not a local one

		self.assertEqual(image_home_servers(image), {home})

	# ---- Active intersection ----------------------------------------------------

	def test_home_excludes_drained_server(self) -> None:
		a = self._server("a")
		b = self._server("b")
		image = make_image(self._name("url")).name
		self._sync_task(image, a)
		self._sync_task(image, b)
		# b leaves the fleet after syncing — no longer a placement candidate.
		frappe.db.set_value("Server", b, "status", "Draining")

		self.assertEqual(image_home_servers(image), {a})

	# ---- default_server_for_image ----------------------------------------------

	def test_default_server_for_image_picks_a_home(self) -> None:
		home = self._server("a")
		image = make_image(self._name("url")).name
		self._sync_task(image, home)

		chosen = default_server_for_image(image, required_vcpus=1, required_memory_mb=512, required_disk_gb=4)
		self.assertEqual(chosen, home)

	def test_default_server_for_image_skips_non_home_with_room(self) -> None:
		# The image lives ONLY on `home`; `other` is Active with room but lacks the
		# bytes. Placement must pick home, never other — the bug the pin masked.
		other = self._server("other")  # created first → default_server would prefer it
		home = self._server("home")
		image = make_image(self._name("url")).name
		self._sync_task(image, home)

		chosen = default_server_for_image(image, required_vcpus=1, required_memory_mb=512, required_disk_gb=4)
		self.assertEqual(chosen, home)
		self.assertNotEqual(chosen, other)

	def test_default_server_for_image_throws_when_image_nowhere(self) -> None:
		# Active fleet exists, but the image has no home at all → a clear boundary
		# error ("export it first"), distinct from NoCapacityError.
		self._server("a")
		image = make_image(self._name("url")).name
		with self.assertRaises(frappe.ValidationError) as raised:
			default_server_for_image(image, required_vcpus=1, required_memory_mb=512, required_disk_gb=4)
		self.assertNotIsInstance(raised.exception, NoCapacityError)
		self.assertIn("not present on any active server", str(raised.exception))

	def test_default_server_for_image_no_capacity_on_home(self) -> None:
		# The image IS present, but its only home has no room → NoCapacityError (a
		# distinct signal from "image nowhere"), so Central can tell them apart.
		home = make_server(
			self.provider,
			title=self._name("tight"),
			size="DigitalOcean/s-4vcpu-8gb",
			status="Active",
			ipv6_address="2001:db8:9::1",
			ipv6_prefix="2001:db8:9::/64",
			ipv6_virtual_machine_range="2001:db8:9::/124",
		)
		frappe.db.set_value("Server", home.name, "status", "Active")
		# Tight memory total, fully spent, so a 512 MB VM can't fit on the memory axis.
		frappe.db.set_value("Server", home.name, "memory_megabytes_total", 512)
		frappe.db.set_value("Server", home.name, "vcpus_total", 0)
		frappe.db.set_value("Server", home.name, "pool_disk_gigabytes_total", 0)
		image = make_image(self._name("url")).name
		self._sync_task(image, home.name)
		# Spend the memory with a first VM placed explicitly on this home.
		from atlas.tests.fixtures import make_virtual_machine

		make_virtual_machine(home.name, image, vcpus=1, memory_megabytes=512, disk_gigabytes=4)

		with self.assertRaises(NoCapacityError):
			default_server_for_image(image, required_vcpus=1, required_memory_mb=512, required_disk_gb=4)

	def _export_row(self, image: str, source: str, target: str, status: str) -> None:
		"""A `Virtual Machine Image Export` row. `source_server` is set explicitly so the
		insert doesn't depend on promote-Task denormalization, and status is set post-insert
		(before_insert defaults it to Pending)."""
		doc = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image Export",
				"image": image,
				"source_server": source,
				"target_server": target,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Virtual Machine Image Export", doc.name, "status", status)
