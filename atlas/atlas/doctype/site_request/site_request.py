"""Site Request — the pre-verification holding row (Contract C, spec/14-self-serve.md).

The whole point of this doctype is the *ordering*: a user signs up, we hold the
intent here, email them a link, and **only after they click it** do we create
the `User` and insert the `Site` (which provisions a billable VM). No droplet
work ever happens for an unverified email — verification is the gate.

```
signup → Site Request (Pending, token) → email link → verify → User + Site
                                                          (owner = verified user)
```

A `Site Request` is NOT a `Site`: it carries the intent + the verification state,
no VM and no routing. It enforces the same Contract-A label rules as `Site`
(shared `subdomain_label`), resolves the region the same way, and at fulfilment
hands off to `Site` for the authoritative uniqueness + the provision flow.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, get_datetime, now_datetime

from atlas.atlas.placement import active_root_domain
from atlas.atlas.subdomain_label import normalize, validate_label, validate_reserved

# How long a verification token stays valid, measured from the request's
# creation. A link clicked after this is rejected (status → Expired) so a leaked
# or abandoned token can't fulfil indefinitely. 24h matches the plan's sketch.
TOKEN_TTL_HOURS = 24

# Role the fulfilled owner is granted so they can sign in to the dashboard SPA
# and see their own Site (owner_only scoping). Website User keeps them off Desk.
ATLAS_USER_ROLE = "Atlas User"


class SiteRequest(Document):
	def before_insert(self) -> None:
		"""Gate the label (same Contract-A rules as Site), resolve the region the
		user never picks, mint the verification token, and start Pending. The Site
		itself is NOT created here — that waits for `verify()` (Contract C)."""
		validate_label(self.subdomain)
		validate_reserved(self.subdomain)
		self.subdomain = normalize(self.subdomain)
		if not self.region:
			self.region = active_root_domain().region
		if not self.token:
			self.token = frappe.generate_hash(length=32)
		if not self.status:
			self.status = "Pending"

	# ----- verification state ---------------------------------------------

	def is_expired(self) -> bool:
		"""True once the token TTL has lapsed (measured from creation). A Pending
		request past its TTL can no longer be fulfilled.

		`self.creation` is a string off the DB; coerce both sides to datetime so the
		comparison isn't str-vs-datetime (the date-trap that bit TLS issuance)."""
		deadline = get_datetime(add_to_date(self.creation, hours=TOKEN_TTL_HOURS))
		return now_datetime() > deadline

	def verify(self) -> "frappe.model.document.Document":
		"""Fulfil the request (Contract C step 5), all server-side:

		  1. get-or-create the `User` for `email` (Website User + Atlas User role),
		  2. insert the `Site` AS that user so Frappe stamps `owner = user`,
		  3. mark the request Verified → Fulfilled and link the produced Site.

		Returns the created `Site`. Idempotent: a request already Fulfilled returns
		its existing Site rather than provisioning a second one. Throws on an expired
		token or a subdomain that got taken since the request — the caller (the verify
		route) renders that as a clean message.

		Concurrency: a verification link is routinely fetched twice near-simultaneously
		(a mail scanner prefetches it, then the user clicks). Without serialization both
		fetches read `Pending`, both insert the `User`, and both queue `create_contact`
		— which then race on `tabContact` (InnoDB 1020 "Record has changed"). So we take
		a row lock on this request FOR UPDATE and re-read its status under the lock: the
		second fetch blocks until the first commits, then sees `Fulfilled` and returns
		the same Site instead of fulfilling twice."""
		# SELECT ... FOR UPDATE on this row; the second concurrent verify() blocks here
		# until the first commits. Re-read status from the DB (not stale self.status) so
		# the loser observes the winner's Fulfilled and short-circuits below.
		self.status = frappe.db.get_value("Site Request", self.name, "status", for_update=True)
		if self.status == "Fulfilled":
			# The other fetch already fulfilled this request: don't provision twice.
			return frappe.get_doc("Site", frappe.db.get_value("Site Request", self.name, "site"))
		if self.status == "Expired" or self.is_expired():
			self._mark_expired()
			frappe.throw(_("This verification link has expired — please sign up again."))
		if self.status != "Pending":
			frappe.throw(f"This request cannot be verified (status is {self.status}).")

		user = self._ensure_user()
		site = self._insert_site_as(user)

		self.verified_at = now_datetime()
		self.site = site.name
		self.status = "Fulfilled"
		self.save(ignore_permissions=True)
		# Re-own the request to the verified user (it was created by Guest) so the
		# owner_only scoping shows them their own request — matching how the Site it
		# produced is owned (Contract C). `owner` is a constant field (.save() throws
		# CannotChangeConstantError), so set it directly with db_set.
		self.db_set("owner", user)
		return site

	def _mark_expired(self) -> None:
		if self.status != "Expired":
			self.db_set("status", "Expired")

	def _ensure_user(self) -> str:
		"""The verified user: an existing User for this email, or a fresh Website
		User with the Atlas User role. `send_welcome_email = 0` — we already
		emailed them (the verification mail); the welcome flow would be a second,
		confusing mail. An existing User is reused (the account-light model: one
		account, more Sites later via the SPA) — we just make sure they hold the
		role so the SPA scoping admits them."""
		email = (self.email or "").strip()
		if not frappe.db.exists("User", email):
			# Build the role into the doc BEFORE insert so a fresh user is a single
			# write. Inserting and *then* .save()ing to add the role enqueues two
			# `create_contact` jobs at the same commit (User.on_update, after_commit),
			# which race on `tabContact` (InnoDB 1020 "Record has changed"). One write
			# = one enqueue.
			#
			# The verify() row lock serializes two fetches of the SAME request, but an
			# email may hold up to MAX_PENDING_PER_EMAIL requests; verifying two
			# DIFFERENT ones at once would have both reach this insert. `User.name` is
			# the email (primary key), so the loser gets a clean DuplicateEntryError —
			# fall through and reuse the now-existing user rather than 500.
			try:
				return (
					frappe.get_doc(
						{
							"doctype": "User",
							"email": email,
							"first_name": email.split("@")[0],
							"user_type": "Website User",
							"send_welcome_email": 0,
							"roles": [{"role": ATLAS_USER_ROLE}],
						}
					)
					.insert(ignore_permissions=True)
					.name
				)
			except frappe.DuplicateEntryError:
				pass
		# Existing user: reused (account-light model). Grant the role only if it's
		# missing — a no-op .save() otherwise. This user already has a Contact, so
		# the resulting create_contact run takes the update branch, not the INSERT
		# that races. ignore_permissions because fulfilment may run as Guest.
		user = frappe.get_doc("User", email)
		if ATLAS_USER_ROLE not in {row.role for row in user.get("roles")}:
			user.append_roles(ATLAS_USER_ROLE)
			user.save(ignore_permissions=True)
		return user.name

	def _insert_site_as(self, user: str) -> "frappe.model.document.Document":
		"""Insert the Site as the verified user so Frappe stamps `owner = user`
		(Contract C). Site.autoname re-runs the authoritative FQDN uniqueness check
		— a label taken since this request was made throws a clean "already taken",
		which the verify route surfaces. Restores the session user in `finally` so
		fulfilment never leaves the request running as someone else."""
		previous = frappe.session.user
		# nosemgrep: frappe-setuser -- insert the Site AS the verified user so Frappe stamps owner = user (Contract C); reverted in the finally below
		frappe.set_user(user)
		try:
			return frappe.get_doc(
				{
					"doctype": "Site",
					"subdomain": self.subdomain,
					"region": self.region,
				}
			).insert()
		finally:
			# nosemgrep: frappe-setuser -- restore the original session user after insert
			frappe.set_user(previous)
