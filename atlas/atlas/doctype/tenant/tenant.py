"""Tenant — the unit of ownership/grouping for Atlas resources.

A tenant is created and managed by **Central** (the external system that owns
end-users and talks to Atlas as the operator). Central sets the immutable
`email` and `central_reference` once at creation, then stamps the optional,
set-only-once `tenant` link on the resources it provisions (Virtual Machine,
Virtual Machine Image, Virtual Machine Snapshot).

This is operator/Central-facing only (System Manager permission, no `Atlas User`
row, no SPA nav item). It is pure data plus list helpers — no Tasks, no
lifecycle. The existing owner-based scoping (spec 11) is unchanged for now;
Central-driven tenancy that supersedes it is a follow-on.
"""

import uuid

import frappe
from frappe.model.document import Document

# Identity Central sets once at creation. Both carry a unique index in the JSON;
# this controller guard adds the same belt-and-suspenders immutability the other
# resource DocTypes use (Virtual Machine, Virtual Machine Image).
IMMUTABLE_AFTER_INSERT = (
	"email",
	"central_reference",
)


class Tenant(Document):
	def autoname(self) -> None:
		self.name = str(uuid.uuid4())

	def before_insert(self) -> None:
		# Central often omits `title`; default it so Desk lists read by a name, not a
		# UUID. Editable afterwards (unlike `email` / `central_reference`).
		if not self.title:
			self.title = (self.email or "").strip().lower() or self.central_reference

	def validate(self) -> None:
		self.email = (self.email or "").strip().lower()
		self._validate_immutability()

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def virtual_machines(self) -> list[dict]:
		"""Virtual Machines stamped with this tenant, newest first."""
		return frappe.get_all(
			"Virtual Machine",
			filters={"tenant": self.name},
			fields=["name", "title", "status", "server"],
			order_by="creation desc",
		)

	@frappe.whitelist()
	def images(self) -> list[dict]:
		"""Virtual Machine Images stamped with this tenant, newest first."""
		return frappe.get_all(
			"Virtual Machine Image",
			filters={"tenant": self.name},
			fields=["name", "image_name", "title", "is_active"],
			order_by="creation desc",
		)

	@frappe.whitelist()
	def snapshots(self) -> list[dict]:
		"""Virtual Machine Snapshots stamped with this tenant, newest first."""
		return frappe.get_all(
			"Virtual Machine Snapshot",
			filters={"tenant": self.name},
			fields=["name", "title", "kind", "virtual_machine", "server"],
			order_by="creation desc",
		)

	@frappe.whitelist()
	def resources(self) -> dict:
		"""Every resource stamped with this tenant, in one round-trip. Reuses the
		individual helpers so there is one source of truth for fields/filters."""
		return {
			"virtual_machines": self.virtual_machines(),
			"images": self.images(),
			"snapshots": self.snapshots(),
		}
