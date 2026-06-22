"""The public signup on-ramp (spec/14-self-serve.md, Contract C).

The one guest-reachable write in Atlas: a visitor states an email + a subdomain,
we hold the intent as a `Site Request` (Pending), and email them a verification
link. **Nothing is provisioned here** — no User, no Site, no VM. That all waits
for the verification click (`atlas/www/verify.py` → `SiteRequest.verify`), which
is the gate against a typo'd or hostile address triggering billable compute.

This module is the form's submit endpoint + the verification email. It is
guest-writable, sends mail, and (downstream) provisions — i.e. an abuse surface —
so it is rate-limited per IP (decorator) AND caps outstanding unverified requests
per email (an attacker can't fan out a thousand Pending rows from one address).
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import validate_email_address

from atlas.atlas.placement import active_root_domain
from atlas.atlas.subdomain_label import is_taken, normalize, validate_label, validate_reserved

# Per-email cap on outstanding (Pending) requests. One verification in flight per
# address is the norm; a small cap absorbs an honest resend without letting one
# email mint unbounded rows. Beyond this we ask them to check their inbox.
MAX_PENDING_PER_EMAIL = 3


# nosemgrep: guest-whitelisted-method -- public signup on-ramp; email and subdomain are validated, the per-email pending count is capped, and the endpoint is rate-limited 5/hour per email
@frappe.whitelist(allow_guest=True)
@rate_limit(key="email", limit=5, seconds=60 * 60)
def request_site(email: str, subdomain: str) -> dict:
	"""Validate, create a Pending `Site Request`, and email the verification link.

	Guest-callable (the signup form posts here). Validates the email + the
	subdomain with the SAME Contract-A rules `Site` enforces, rejects a label
	already taken by a live Site (best-effort early check — the authoritative
	uniqueness is still `Site`'s key at fulfilment), caps outstanding requests per
	email, then inserts the request (`ignore_permissions` — a guest can't normally
	write) and sends the mail. Returns a small dict the form renders as
	"check your inbox"; never leaks the token.

	Rate-limited 5/hour per email (the decorator) — the email is the abuse key, not
	the IP, because a hostile actor behind one NAT and a botnet behind many IPs
	both hammer the same address space."""
	email = (email or "").strip().lower()
	if not validate_email_address(email):
		frappe.throw(_("Please enter a valid email address."))

	# Same Contract-A gate as Site, BEFORE we touch the DB or send mail.
	validate_label(subdomain)
	validate_reserved(subdomain)
	subdomain = normalize(subdomain)

	if is_taken(subdomain):
		frappe.throw(f"Subdomain '{subdomain}' is already taken — choose another.")

	_enforce_pending_cap(email)

	request = frappe.get_doc(
		{
			"doctype": "Site Request",
			"email": email,
			"subdomain": subdomain,
		}
	).insert(ignore_permissions=True)

	_send_verification_email(request)

	result = {
		"email": email,
		"subdomain": subdomain,
		"message": "Check your inbox for a verification link to finish creating your site.",
	}
	# Developer convenience only: surface the link the email carries so a dev can
	# click through without a working mail account. Gated on developer_mode so the
	# token is never returned in production (it bypasses email verification).
	if frappe.conf.developer_mode:
		result["verification_url"] = _verification_url(request.token)
	return result


def _enforce_pending_cap(email: str) -> None:
	"""Throw if this email already has the max Pending requests in flight."""
	pending = frappe.db.count("Site Request", {"email": email, "status": "Pending"})
	if pending >= MAX_PENDING_PER_EMAIL:
		frappe.throw(
			_(
				"You already have a verification email in flight — check your inbox (including spam) before requesting another."
			)
		)


def _verification_url(token: str) -> str:
	"""The absolute link the email carries: `<site>/verify?token=<token>`. Built
	from the request host so it is correct behind the proxy + in dev."""
	return f"{frappe.utils.get_url()}/verify?token={token}"


def _send_verification_email(request) -> None:
	"""Email the verification link. The fqdn-to-be is shown so the user confirms
	the subdomain they picked before committing. `frappe.sendmail` queues through
	the site's outbound email — a default outgoing Email Account must be configured
	or the send raises OutgoingEmailError."""
	fqdn = f"{request.subdomain}.{active_root_domain().domain}"
	link = _verification_url(request.token)
	frappe.sendmail(
		recipients=[request.email],
		subject="Verify your email to create your Frappe site",
		template="site_verification",
		args={"fqdn": fqdn, "subdomain": request.subdomain, "link": link},
		now=False,
	)
