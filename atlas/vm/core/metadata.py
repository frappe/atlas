from collections.abc import Iterable

import frappe
from frappe import _


def validate_metadata_rows(rows: Iterable[object]) -> None:
	seen: set[str] = set()
	for row in rows:
		key = (getattr(row, "key", None) or "").strip()
		if not key:
			frappe.throw(_("Metadata key cannot be empty."))
		if key in seen:
			frappe.throw(_("Duplicate metadata key {0}.").format(key))
		seen.add(key)
