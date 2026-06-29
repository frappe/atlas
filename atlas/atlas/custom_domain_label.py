"""Custom-domain FQDN rules (spec/18 Phase 2, the full-FQDN sibling of
`subdomain_label`).

A `Subdomain` is a single DNS label under the one regional wildcard; a `Custom
Domain` is an arbitrary external host the customer already owns (`shop.acme.com`).
The rules are different and deliberately separate (the dot ban + per-VM cap are
correct for wildcard labels and must not loosen on the hot path):

- A custom domain MUST have at least one dot (it is a multi-label FQDN, not a bare
  label — a bare label is a wildcard-subdomain `register(label)`, never this path).
- A custom domain MUST NOT be under the active regional wildcard (`*.<region>`):
  a name under the wildcard is already routable as a `Subdomain`, served by the
  wildcard cert — routing it as a Custom Domain would issue a redundant per-domain
  cert and split the route across two tables. The guest binary peels such a name to
  a label and takes the `register(label)` path; the controller rejects it here as a
  belt-and-suspenders guard.
- Each label is a valid DNS label (1-63 chars, lowercase `[a-z0-9-]`, no leading /
  trailing hyphen), total length <= 253. We do NOT validate the public suffix /
  registrability (that is the customer's DNS provider's concern); we only reject a
  shape that could never be a hostname or that collides with our own namespace.

`normalize_domain` lowercases and strips a trailing dot (a FQDN may be written
`shop.acme.com.`); case is normalized here (unlike `subdomain_label.normalize`,
which validates case loudly) because an external hostname is conventionally
case-insensitive and the customer pastes it from their DNS provider.
"""

import frappe
from frappe import _

DOMAIN_MAX_LENGTH = 253
LABEL_MAX_LENGTH = 63


def normalize_domain(domain: str | None) -> str:
	"""The canonical custom domain: stripped, lowercased, trailing dot removed.

	Hostnames are case-insensitive and a FQDN may carry a trailing root dot
	(`shop.acme.com.`); both are normalized away so the routing key is canonical."""
	return (domain or "").strip().lower().rstrip(".")


def validate_custom_domain(domain: str | None, region_domain: str) -> None:
	"""Raise unless `domain` is a well-formed external FQDN routable as a Custom Domain.

	`region_domain` is the active regional wildcard suffix (e.g. `blr1.frappe.dev`); a
	name under it is rejected (it belongs in the `register(label)` wildcard path).
	Throws a clear, field-specific message the guest surfaces verbatim."""
	name = normalize_domain(domain)
	if not name:
		frappe.throw(_("A domain is required"))
	if "." not in name:
		frappe.throw(
			_("A custom domain must be a full domain name (e.g. shop.example.com), not a bare label")
		)
	if len(name) > DOMAIN_MAX_LENGTH:
		frappe.throw(f"Domain must be at most {DOMAIN_MAX_LENGTH} characters")

	# A name under our regional wildcard is a Subdomain, not a Custom Domain.
	region_domain = (region_domain or "").strip().lower().rstrip(".")
	if region_domain and (name == region_domain or name.endswith(f".{region_domain}")):
		frappe.throw(
			f"{name!r} is under the regional wildcard {region_domain!r}; "
			"register it as a subdomain, not a custom domain"
		)

	for label in name.split("."):
		_validate_label(label, name)


def _validate_label(label: str, domain: str) -> None:
	if not label:
		frappe.throw(f"{domain!r} has an empty label (a doubled or leading/trailing dot)")
	if len(label) > LABEL_MAX_LENGTH:
		frappe.throw(f"Label {label!r} in {domain!r} exceeds {LABEL_MAX_LENGTH} characters")
	if label.startswith("-") or label.endswith("-"):
		frappe.throw(f"Label {label!r} in {domain!r} must not start or end with a hyphen")
	if not all((c.isascii() and c.isalnum()) or c == "-" for c in label):
		frappe.throw(f"Label {label!r} in {domain!r} may only contain lowercase letters, digits, and hyphens")
