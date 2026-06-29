"""Custom Domain — an arbitrary external domain (shop.acme.com) routed to a bench
site (spec/18 Phase 2, the custom-domain layer).

This is the full-FQDN sibling of `Subdomain`. A `Subdomain` row keys on a bare
label under the one regional wildcard (`app` → `app.<region>.frappe.dev`), terminated
at the proxy under the regional wildcard cert; a `Custom Domain` row keys on the
**whole host** the customer owns (`shop.acme.com`). The dot ban and the per-VM cap
stay on `Subdomain` (correct for wildcard labels); custom domains are a separate table
so loosening neither rule touches the hot path.

**TLS is SNI passthrough — the proxy holds no per-domain cert.** The proxy reads the
SNI at L4 (`ssl_preread`) and forwards the RAW TLS stream to the backend site VM's
`:443`; the BENCH terminates TLS with its own cert (pilot's `setup-letsencrypt`). So a
Custom Domain row only declares "this host → that backend": the row's `address` is the
target VM's `/128`, and insert / active-toggle / delete each reconcile the proxy fleet's
SEPARATE custom-domain SNI map (a second `lua_shared_dict`, looked up by full host).

A row is `Active` on register and enters BOTH proxy maps (`:80` ACME and `:443` SNI)
immediately — there is no readiness gate. If the VM's cert isn't issued yet, the proxy
just forwards a TLS handshake the VM can't complete (a transient client-side cert error
that self-heals once the cert lands); pure passthrough, no cross-tenant effect. `status`
is only Active / Failed (Failed signals a reconcile error). There is no cert on Atlas's
side.
"""

import frappe
from frappe.model.document import Document

# The routing key (the full host) and its target VM are fixed once chosen —
# repointing a live custom domain at a different VM is a delete-and-recreate, so the
# proxy map change is explicit (mirrors Subdomain).
IMMUTABLE_AFTER_INSERT = (
	"domain",
	"virtual_machine",
)


class CustomDomain(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		active: DF.Check
		address: DF.Data
		domain: DF.Data
		site: DF.Data | None
		status: DF.Literal["Active", "Failed"]
		virtual_machine: DF.Link
	# end: auto-generated types

	def validate(self) -> None:
		self._validate_immutability()
		self._denormalize_address()

	def after_insert(self) -> None:
		"""Auto-reconcile: a new active custom-domain mapping changes the region's
		served map, so push it to the fleet (mirrors Subdomain.after_insert)."""
		self._enqueue_reconcile()

	def on_update(self) -> None:
		"""`active` is the only field that changes the served maps (it drops the row from
		both the :443 SNI and :80 ACME maps), so reconcile when it flips. A no-op save (active
		unchanged) does not SSH the fleet."""
		original = self.get_doc_before_save()
		if original and original.active != self.active:
			self._enqueue_reconcile()

	def on_trash(self) -> None:
		"""Deleting an active mapping drops it from the served SNI map; reconcile so the proxy
		fleet stops forwarding the custom domain."""
		self._enqueue_reconcile()

	def _enqueue_reconcile(self) -> None:
		"""Background-reconcile the proxy fleet. Shares the SAME deduplicated job as
		Subdomain (`auto_reconcile_subdomains`): a reconcile reads the WHOLE desired state
		(both the subdomain map and the custom-domain map), so it is the same job no matter
		which kind of row triggered it — N changes of either kind need one reconcile."""
		frappe.enqueue(
			"atlas.atlas.doctype.subdomain.subdomain.auto_reconcile",
			queue="long",
			timeout=300,
			job_id="auto_reconcile_subdomains",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(original, field) != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	def _denormalize_address(self) -> None:
		"""Copy the target VM's public IPv6 onto `address`, so the desired-map query
		(custom_domain_map) is a single SELECT with no join. The proxy dials this literal;
		it never resolves a VM. A VM with no ipv6 yet is a hard error."""
		address = frappe.db.get_value("Virtual Machine", self.virtual_machine, "ipv6_address")
		if not address:
			frappe.throw(
				f"Virtual Machine {self.virtual_machine} has no ipv6_address; cannot map a custom domain to it"
			)
		self.address = address


def custom_domain_sni_map() -> dict[str, str]:
	"""The desired :443 SNI passthrough map: every active custom domain, as
	`host -> "[<v6>]:443"` ready-to-dial literals.

	This is the stream-side `domains` dict the proxy's `ssl_preread` router (sni_router.lua)
	looks up to pass the raw TLS stream through to the backend VM's `:443`. A domain enters
	this map the moment it is registered — there is no readiness gate. If the VM's cert isn't
	issued yet the proxy forwards a handshake the VM can't complete (a transient client-side
	cert error that self-heals once the cert lands); pure passthrough, no cross-tenant effect.
	The proxy reconcile (atlas.atlas.proxy) compares this, serialized canonically, against
	each proxy guest's live SNI map and syncs on drift."""
	rows = frappe.get_all(
		"Custom Domain",
		filters={"active": 1},
		fields=["domain", "address"],
	)
	return {row["domain"]: f"[{row['address']}]:443" for row in rows}


def custom_domain_acme_map() -> dict[str, str]:
	"""The desired :80 ACME-passthrough map: every active custom domain, as
	`host -> "[<v6>]"` bracketed bare-v6 literals.

	This is the http-side `acme_domains` dict the proxy's `:80` Host fork (acme_router.lua)
	looks up to forward a custom domain's HTTP-01 challenge to its VM, so the VM can complete
	its first issuance. It carries the SAME row set as the `:443` SNI map (every active
	domain), differing only in value shape — the bare bracketed v6 (acme_router appends
	`:80`)."""
	rows = frappe.get_all(
		"Custom Domain",
		filters={"active": 1},
		fields=["domain", "address"],
	)
	return {row["domain"]: f"[{row['address']}]" for row in rows}
