"""Contract-A subdomain rules, shared by `Site` and `Site Request`.

The one routing string (spec/14-self-serve.md) starts as a single DNS
label the user picks. Two doctypes gate that label: `Site` (the real resource, at
insert) and `Site Request` (the pre-verification holding row). They must
enforce the *same* rules — otherwise a request could reserve a name `Site` would
reject, or two requests pass and collide at fulfilment. Factor the rules here so
there is one source of truth.

These are the label-shape + reserved-name + availability checks only. `Site`
still owns the authoritative uniqueness at insert (its FQDN key); `availability`
here is a best-effort early check so a user learns "taken" at request time, not
after verifying.
"""

import frappe
from frappe import _

# A subdomain label that is not the user's to take. `www admin api …` are the
# operational names a fleet reserves; everything else is taken at insert by the
# FQDN uniqueness check. Frozen here (Contract A, spec/14-self-serve.md); `Site` re-exports it.
RESERVED_SUBDOMAINS = frozenset(
	{
		"www",
		"admin",
		"api",
		"proxy",
		"app",
		"dashboard",
		"mail",
		"ns",
		"root",
	}
)

# DNS label rules: 1-63 chars, lowercase alphanumerics and hyphens, no leading
# or trailing hyphen. The dot ban is enforced separately so the message is clear
# (a dot would escape the one regional wildcard the proxy terminates).
LABEL_MAX_LENGTH = 63


def normalize(subdomain: str | None) -> str:
	"""The canonical label: stripped, as the user typed it (case is *validated*,
	not silently lowered, so `Acme` fails loud rather than quietly becoming
	`acme`)."""
	return (subdomain or "").strip()


def validate_label(subdomain: str | None) -> None:
	"""Single DNS label, no dots. A dot would escape the regional wildcard and
	need its own cert (deferred). Enforce lowercase `[a-z0-9-]`, no leading/
	trailing hyphen, length cap. Throws a clear, field-specific message."""
	label = normalize(subdomain)
	if not label:
		frappe.throw(_("A subdomain is required"))
	if "." in label:
		frappe.throw(_("Subdomain must be a single label with no dots"))
	if label != label.lower():
		frappe.throw(_("Subdomain must be lowercase"))
	if len(label) > LABEL_MAX_LENGTH:
		frappe.throw(f"Subdomain must be at most {LABEL_MAX_LENGTH} characters")
	if label.startswith("-") or label.endswith("-"):
		frappe.throw(_("Subdomain must not start or end with a hyphen"))
	if not all((c.isascii() and c.isalnum()) or c == "-" for c in label):
		frappe.throw(_("Subdomain may only contain lowercase letters, digits, and hyphens"))


def validate_reserved(subdomain: str | None) -> None:
	if normalize(subdomain).lower() in RESERVED_SUBDOMAINS:
		frappe.throw(f"Subdomain '{normalize(subdomain)}' is reserved — choose another")


def is_taken(subdomain: str | None) -> bool:
	"""True if a live `Site` already owns this label under the active domain.

	Best-effort early check (spec/14-self-serve.md): lets a signup form reject a taken name at
	request time. NOT authoritative — the authoritative uniqueness is `Site`'s
	FQDN key at insert (handle the race with a clean "taken" message there). A
	Terminated Site's FQDN is gone (the row is deleted on terminate's VM path? no
	— the row stays Terminated), so we count any existing `Site` row with that
	label: a Terminated label is still spoken for until the row is removed."""
	from atlas.atlas.placement import active_root_domain

	label = normalize(subdomain)
	if not label:
		return False
	fqdn = f"{label}.{active_root_domain().domain}"
	return bool(frappe.db.exists("Site", fqdn))
