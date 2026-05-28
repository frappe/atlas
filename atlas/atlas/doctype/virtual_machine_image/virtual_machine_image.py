import frappe
from frappe.model.document import Document

IMMUTABLE_AFTER_INSERT = (
	"image_name",
	"title",
	"default_disk_gigabytes",
	"kernel_url",
	"kernel_filename",
	"kernel_sha256",
	"rootfs_url",
	"rootfs_filename",
	"rootfs_sha256",
)


class VirtualMachineImage(Document):
	def validate(self) -> None:
		for field in ("kernel_url", "rootfs_url"):
			value = self.get(field) or ""
			if value and not value.startswith("https://"):
				frappe.throw(f"{field} must be an https:// URL, got: {value}")
		self._validate_immutability()

	def after_insert(self) -> None:
		"""Auto-sync: enqueue one Task per Active server so the operator never
		has to click `Sync to All Servers` on a fresh image row."""
		if not self.is_active:
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
			frappe.throw("Image is already archived")
		frappe.db.set_value(self.doctype, self.name, "is_active", 0)

	@frappe.whitelist()
	def sync_status(self) -> list[dict]:
		"""For each Active server, the last successful sync-image.sh Task
		referencing this image. None if never synced.

		Returned shape:
		  [{"server": "...", "synced_at": "...", "task": "..."}, ...]
		"""
		servers = frappe.get_all(
			"Server",
			filters={"status": "Active"},
			fields=["name", "region"],
			order_by="name asc",
		)
		results: list[dict] = []
		for server in servers:
			last = frappe.db.sql(
				"""
				SELECT name, modified
				FROM `tabTask`
				WHERE server = %(server)s
				  AND script = 'sync-image.sh'
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
			results.append({
				"server": server.name,
				"region": server.region,
				"synced_at": row["modified"].isoformat() if row else None,
				"task": row["name"] if row else None,
			})
		return results

	@frappe.whitelist()
	def sync_to_all_servers(self, servers: list[str] | str | None = None) -> list[str]:
		"""Enqueue one sync Task per server in `servers` (defaults to every
		Active server). Returns Task names."""
		if isinstance(servers, str):
			servers = frappe.parse_json(servers) or None
		if not servers:
			servers = frappe.get_all(
				"Server", filters={"status": "Active"}, pluck="name"
			)
		return [self.sync_to_server(server) for server in servers]

	@frappe.whitelist()
	def sync_to_server(self, server_name: str) -> str:
		"""Insert a Pending Task row and enqueue execute_task. Returns Task name."""
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
		task = frappe.get_doc({
			"doctype": "Task",
			"server": server_name,
			"script": "sync-image.sh",
			"status": "Pending",
			"triggered_by": frappe.session.user if frappe.session else "Administrator",
		})
		task.variables_dict = variables
		task.insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.enqueue(
			"atlas.atlas.ssh.execute_task",
			queue="long",
			timeout=1800,
			task_name=task.name,
		)
		return task.name
