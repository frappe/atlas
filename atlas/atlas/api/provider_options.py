"""Whitelisted helpers used by the Server Provider's Provision Server dialog.

The Region / Size / Image dropdowns on that dialog need to render as standard
Frappe Select fields, but the option lists are provider-specific. We keep
hand-maintained dicts (same model as `default_image` / cost dict) so the dialog
doesn't have to round-trip to DigitalOcean's API on every open — and so the
options stay deterministic in tests.
"""

import frappe

# Region slugs. Hand-maintained; matches the slugs Atlas operators have
# historically deployed into. Missing regions still resolve fine because the
# field also accepts the user's typed default via a single-line description.
KNOWN_REGIONS: list[str] = [
	"blr1",
	"nyc1",
	"nyc3",
	"sfo3",
	"ams3",
	"fra1",
	"lon1",
	"sgp1",
	"tor1",
]

KNOWN_SIZES: list[str] = [
	"s-1vcpu-1gb",
	"s-1vcpu-2gb",
	"s-2vcpu-2gb",
	"s-2vcpu-4gb-intel",
	"s-2vcpu-4gb",
	"s-4vcpu-8gb",
	"c-2",
	"c-4",
]

KNOWN_IMAGES: list[str] = [
	"ubuntu-24-04-x64",
	"ubuntu-22-04-x64",
]


@frappe.whitelist()
def provider_options() -> dict[str, list[str]]:
	"""Return the Region / Size / Image option lists for the Provision dialog.

	Called once when the dialog opens. Returning all three lists in one call
	keeps the dialog construction sync — no spinner needed.
	"""
	return {
		"regions": KNOWN_REGIONS,
		"sizes": KNOWN_SIZES,
		"images": KNOWN_IMAGES,
	}
