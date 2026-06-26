"""Tenant — the unit of ownership/grouping for Atlas resources.

A tenant is created and managed by **Central** (the external system that owns
end-users and talks to Atlas as the operator). The tenant's `name` **is** the
Central `Team.name`: Central passes that id as `team`, and `ensure_tenant` names
the row by it. There is no translation table — the primary key carries the
mapping, so the `tenant` link stamped on a resource is already the Central team.
Central also sets the immutable `email`
once at creation, then stamps the set-only-once `tenant` link on the resources it
provisions (Virtual Machine, Virtual Machine Image, Virtual Machine Snapshot).

This is operator/Central-facing only (System Manager permission). It is pure data
plus list helpers — no Tasks, no lifecycle. Atlas no longer owns end-users or
end-user row-level scoping; tenancy attribution (the `tenant` link on a Virtual
Machine / Site) is how resources are tied back to a Central team.
"""

import frappe
from frappe.model.document import Document

# Identity Central sets once at creation. `email` carries a unique index in the
# JSON; this controller guard adds the same belt-and-suspenders immutability the
# other resource DocTypes use (Virtual Machine, Virtual Machine Image). `name`
# (the Central `Team.name`) is immutable by virtue of being the primary key.
IMMUTABLE_AFTER_INSERT = ("email",)


def ensure_tenant(team: str, email: str | None) -> str:
	"""Get-or-create the Tenant for a Central team and return its name.

	The tenant is named by `team` (the Central `Team.name`) — its `name` *is* that
	id — so the get-or-create is a primary-key lookup. `email` is immutable after
	insert, so an existing tenant is reused as-is (the `email` is only consulted on
	first creation). Shared by the Central-facing provisioning APIs (Virtual
	Machine, Site) so there is one get-or-create path. Runs `ignore_permissions` —
	this is operator orchestration authorized by the Central token, not desk RBAC."""
	if not team:
		frappe.throw("team is required.")
	if frappe.db.exists("Tenant", team):
		return team
	if not email:
		frappe.throw("email is required to create a tenant.")
	return (
		frappe.get_doc(
			{
				"doctype": "Tenant",
				"team": team,
				"email": email,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class Tenant(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		email: DF.Data
		title: DF.Data | None
	# end: auto-generated types

	def autoname(self) -> None:
		# The tenant's `name` is its Central `Team.name`. Central passes it as the
		# `team` kwarg on create; it is not a stored field, so read it off the
		# in-memory doc and let it become the primary key.
		team = self.get("team")
		if not team:
			frappe.throw("team is required.")
		self.name = team

	def before_insert(self) -> None:
		# Central often omits `title`; default it so Desk lists read by a name, not the
		# raw Central reference. Editable afterwards (unlike `email`).
		if not self.title:
			self.title = (self.email or "").strip().lower() or self.name

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
