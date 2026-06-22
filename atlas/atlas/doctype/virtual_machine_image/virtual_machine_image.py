import frappe
from frappe import _
from frappe.model.document import Document

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
		would pass `is_local` as non-local, fan out a `sync-image.py` Task, and only
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
		no rootfs URL there is nothing for `sync-image.py` to fetch, so a local image
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
		"""For each Active server, the last successful sync-image.py Task
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
				  AND script IN ('sync-image.py', 'sync-image.sh')
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
		`sync-image.py` has nothing to download, and the image lives only on the
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
				"script": "sync-image.py",
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
