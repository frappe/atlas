import frappe
from frappe.model.document import Document

from atlas.atlas.subdomain_label import normalize


class SubdomainDenylist(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		label: DF.Data
		reason: DF.Data | None
	# end: auto-generated types

	def validate(self) -> None:
		"""Store the label lowercased and dot-free, so the enforcement query
		(`is_denylisted`) — a single indexed `exists` on the lowercased label — never
		has to case-fold or trim at read time. A dotted/blank label is rejected loud:
		the denylist gates a single DNS label, the same shape a `register` carries."""
		label = normalize(self.label).lower()
		if not label:
			frappe.throw("A denylist label is required")
		if "." in label:
			frappe.throw("A denylist label is a single DNS label with no dots")
		self.label = label


def is_denylisted(label: str) -> bool:
	"""True if `label` is on the brand denylist (an enabled `Subdomain Denylist` row).

	The Component-H complement to the frozen `RESERVED_SUBDOMAINS`: a brand/keyword
	a tenant could grab under the valid wildcard cert (phishing-as-a-service). A
	single indexed `exists` on the lowercased label — cheap enough to run inline on
	every `register`/`check_label`, and an operator's new row is honored on the next
	call (no deploy, no migrate). A disabled row (`enabled=0`) lifts the block without
	losing the reason."""
	return bool(frappe.db.exists("Subdomain Denylist", {"label": normalize(label).lower(), "enabled": 1}))


# The brand/keyword labels seeded at install (spec/18 Component H). Payment brands,
# auth keywords, and the obvious account-takeover lures — the names worth blocking
# before an operator ever has to spot one in the audit log. The operator curates the
# DocType from here; this is the floor, not the ceiling.
SEED_DENYLIST: dict[str, str] = {
	"paypal": "payment brand",
	"stripe": "payment brand",
	"visa": "payment brand",
	"mastercard": "payment brand",
	"venmo": "payment brand",
	"login": "auth keyword",
	"signin": "auth keyword",
	"account": "auth keyword",
	"accounts": "auth keyword",
	"secure": "auth keyword",
	"verify": "auth keyword",
	"billing": "auth keyword",
	"payment": "auth keyword",
	"wallet": "auth keyword",
	"support": "auth keyword",
}


def seed_denylist() -> int:
	"""Insert the seed brand/keyword rows that are not already present. Idempotent —
	an existing label (operator-added or a prior seed) is left untouched, so a re-run
	(install patch, a fresh test DB) only fills the gaps. Returns the count inserted."""
	inserted = 0
	for label, reason in SEED_DENYLIST.items():
		if frappe.db.exists("Subdomain Denylist", label):
			continue
		frappe.get_doc(
			{"doctype": "Subdomain Denylist", "label": label, "reason": reason, "enabled": 1}
		).insert(ignore_permissions=True)
		inserted += 1
	return inserted
