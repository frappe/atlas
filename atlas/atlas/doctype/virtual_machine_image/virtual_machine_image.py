import frappe
from frappe import _
from frappe.model.document import Document

# Canonical base-image catalog. These are the rows `atlas.bootstrap.ensure_image`
# seeds on a fresh site; they live here (not in bootstrap) so the desk "Seed
# default images" list action and the bootstrap path share ONE source of truth
# and can't drift. URLs/digests are pinned dated Ubuntu cloud images — see
# bootstrap.py's release-pin comment and spec/08-images.md.
_NOBLE_RELEASE = "https://cloud-images.ubuntu.com/releases/noble/release-20260518"
_NOBLE_MINIMAL_RELEASE = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260521"

DEFAULT_IMAGE = {
	"image_name": "ubuntu-24.04",
	"title": "Ubuntu 24.04 server cloud image",
	"kernel_url": f"{_NOBLE_RELEASE}/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-server",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_RELEASE}/ubuntu-24.04-server-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-server.ext4",
	"rootfs_sha256": "bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74",
	"default_disk_gigabytes": 4,
}

# The minimal flavor lives under a different upstream tree and ships the same
# generic kernel as server (identical digest). Seeded as a second image row so
# operators can pick the smaller rootfs.
MINIMAL_IMAGE = {
	"image_name": "ubuntu-24.04-minimal",
	"title": "Ubuntu 24.04 minimal cloud image",
	"kernel_url": f"{_NOBLE_MINIMAL_RELEASE}/unpacked/ubuntu-24.04-minimal-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-minimal",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_MINIMAL_RELEASE}/ubuntu-24.04-minimal-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-minimal.ext4",
	"rootfs_sha256": "a288f0bd499e1a747f86fda8ec9822dd99a4e3c0721d89ffd9dd57608ff21072",
	"default_disk_gigabytes": 4,
}

SEEDABLE_IMAGES = (DEFAULT_IMAGE, MINIMAL_IMAGE)

IMMUTABLE_AFTER_INSERT = (
	"image_name",
	"title",
	"default_disk_gigabytes",
	"build_mode",
	"kernel_url",
	"kernel_filename",
	"kernel_sha256",
	"rootfs_url",
	"rootfs_filename",
	"rootfs_sha256",
	"tenant",
)


class VirtualMachineImage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_mode: DF.Literal["", "site", "admin"]
		default_disk_gigabytes: DF.Int
		image_name: DF.Data
		is_active: DF.Check
		kernel_filename: DF.Data
		kernel_sha256: DF.Data | None
		kernel_url: DF.Data | None
		rootfs_filename: DF.Data
		rootfs_sha256: DF.Data | None
		rootfs_url: DF.Data | None
		tenant: DF.Link | None
		title: DF.Data | None
	# end: auto-generated types

	# The four fields that describe a from-URL image's download. They used to be
	# `reqd` in the JSON; now that a local (promoted-from-snapshot) image legitimately
	# leaves them ALL empty, `reqd` is gone and validate() enforces the coherent
	# shape instead: a URL image sets all four, a local image sets none. (Without
	# this, an operator could insert a URL image missing only its sha256 — it would
	# validate, classify as non-local, fan out a sync, and fail only later on the
	# host where the digest is required. The old `reqd` flags caught that at insert.)
	_URL_FIELDS = ("kernel_url", "rootfs_url", "kernel_sha256", "rootfs_sha256")

	def validate(self) -> None:
		for field in ("kernel_url", "rootfs_url"):
			value = self.get(field) or ""
			if value and not value.startswith("https://"):
				frappe.throw(f"{field} must be an https:// URL, got: {value}")
		self._validate_url_coherence()
		self._validate_immutability()

	def _validate_url_coherence(self) -> None:
		"""All four download fields set (a from-URL image) or none set (a local image
		promoted from a snapshot). A partial set — e.g. a URL with no checksum —
		would pass `is_local` as non-local, fan out a `sync-image` Task, and only
		then fail on the host (the script requires the digest), so reject it at insert
		instead. Restores the insert-time gate the dropped `reqd` flags gave."""
		present = {field: bool((self.get(field) or "").strip()) for field in self._URL_FIELDS}
		if any(present.values()) and not all(present.values()):
			missing = [field for field, is_set in present.items() if not is_set]
			frappe.throw(
				"A from-URL image must set kernel_url, rootfs_url, kernel_sha256 and "
				"rootfs_sha256; a local image (promoted from a snapshot) sets none of them. "
				f"Missing: {', '.join(missing)}."
			)

	@property
	def is_local(self) -> bool:
		"""A local image was promoted from a snapshot on one server: its rootfs is
		an `atlas-image-<name>` LV already on that host, not a downloadable URL. With
		no rootfs URL there is nothing for `sync-image` to fetch, so a local image
		is non-syncable — it lives on exactly the server it was promoted on.
		(`Virtual Machine Snapshot.promote_to_image`, spec/08-images.md.)"""
		return not (self.rootfs_url or "").strip()

	def after_insert(self) -> None:
		"""Auto-sync: enqueue one Task per Active server so the operator never
		has to click `Sync to All Servers` on a fresh image row.

		A local image (promoted from a snapshot, no rootfs URL) is skipped: its
		bytes are an LV already on its one server, and there is no download a sync
		Task could run. Same-server scope is deliberate (no fleet distribution this
		iteration); see spec/08-images.md § Promoting a snapshot."""
		if not self.is_active or self.is_local:
			return
		for server in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			self.sync_to_server(server)

	def _validate_immutability(self) -> None:
		"""Every non-`is_active` field is immutable from insert onward.
		The operator's escape hatch is to insert a new image row (e.g.
		`ubuntu-24.04-v2`) — never rewriting an existing one."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def archive(self) -> None:
		"""Decommission this image. Sets is_active=0; the row stays in the
		DB so historical Task references remain queryable."""
		if not self.is_active:
			frappe.throw(_("Image is already archived"))
		frappe.db.set_value(self.doctype, self.name, "is_active", 0)

	@frappe.whitelist()
	def sync_status(self) -> list[dict]:
		"""For each Active server, the last successful sync-image Task
		referencing this image. None if never synced.

		Returned shape:
		  [{"server": "...", "synced_at": "...", "task": "..."}, ...]
		"""
		servers = frappe.get_all(
			"Server",
			filters={"status": "Active"},
			fields=["name"],
			order_by="name asc",
		)
		results: list[dict] = []
		for server in servers:
			last = frappe.db.sql(
				"""
				SELECT name, modified
				FROM `tabTask`
				WHERE server = %(server)s
				  -- Task.script is now the verb 'sync-image'; older rows recorded the
				  -- filename ('sync-image.py'/'.sh'). Task history is immutable, so match
				  -- both the new verb and the legacy filenames.
				  AND script IN ('sync-image', 'sync-image.py', 'sync-image.sh')
				  AND status = 'Success'
				  AND variables LIKE %(image_pattern)s
				ORDER BY modified DESC
				LIMIT 1
				""",
				{
					"server": server.name,
					"image_pattern": f'%"IMAGE_NAME": "{self.name}"%',
				},
				as_dict=True,
			)
			row = last[0] if last else None
			results.append(
				{
					"server": server.name,
					"synced_at": row["modified"].isoformat() if row else None,
					"task": row["name"] if row else None,
				}
			)
		return results

	@frappe.whitelist()
	def sync_to_all_servers(self, servers: list[str] | str | None = None) -> list[str]:
		"""Enqueue one sync Task per server in `servers` (defaults to every
		Active server). Returns Task names."""
		if isinstance(servers, str):
			servers = frappe.parse_json(servers) or None
		if not servers:
			servers = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")
		return [self.sync_to_server(server) for server in servers]

	@frappe.whitelist()
	def sync_to_server(self, server_name: str) -> str:
		"""Insert a Pending Task row and enqueue execute_task. Returns Task name.

		A local image (promoted from a snapshot, no rootfs URL) cannot be synced —
		`sync-image` has nothing to download, and the image lives only on the
		server it was promoted on. Throw cleanly rather than enqueue a Task that
		would fail on the host."""
		if self.is_local:
			frappe.throw(
				f"{self.name} is a local image (promoted from a snapshot) and cannot be synced — "
				"its rootfs LV lives only on the server it was promoted on."
			)
		variables = {
			"IMAGE_NAME": self.image_name,
			"KERNEL_URL": self.kernel_url,
			"KERNEL_FILENAME": self.kernel_filename,
			"KERNEL_SHA256": self.kernel_sha256,
			"ROOTFS_URL": self.rootfs_url,
			"ROOTFS_FILENAME": self.rootfs_filename,
			"ROOTFS_SHA256": self.rootfs_sha256,
			"DEFAULT_DISK_GB": str(self.default_disk_gigabytes),
			"GUEST_NETWORK_UNIT": "/tmp/atlas/atlas-network.service",
		}
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"server": server_name,
				"script": "sync-image",
				"status": "Pending",
				"triggered_by": frappe.session.user if frappe.session else "Administrator",
			}
		)
		task.variables_dict = variables
		task.insert(ignore_permissions=True)
		# nosemgrep: frappe-manual-commit -- persist the Pending sync Task before enqueuing execute_task so the background job can find it cross-transaction
		frappe.db.commit()

		frappe.enqueue(
			"atlas.atlas.ssh.execute_task",
			queue="long",
			timeout=1800,
			task_name=task.name,
		)
		return task.name


@frappe.whitelist()
def seed_default_images() -> dict[str, list[str]]:
	"""Insert the canonical base-image rows (`SEEDABLE_IMAGES`) that bootstrap
	seeds, skipping any that already exist. This is the desk equivalent of
	`atlas.bootstrap.ensure_image` — the list-view "Seed default images" action
	calls it so an operator never hand-types kernel/rootfs URLs and digests.

	Idempotent: returns the rows it created and the ones it left untouched.
	`after_insert` on each fresh `is_active` row fans out a sync to every Active
	server, same as the bootstrap path.
	"""
	frappe.only_for("System Manager")
	created: list[str] = []
	skipped: list[str] = []
	for image in SEEDABLE_IMAGES:
		if frappe.db.exists("Virtual Machine Image", image["image_name"]):
			skipped.append(image["image_name"])
			continue
		doc = frappe.get_doc({"doctype": "Virtual Machine Image", **image, "is_active": 1})
		doc.insert()
		created.append(doc.name)
	return {"created": created, "skipped": skipped}
