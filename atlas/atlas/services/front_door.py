"""Resolve a Virtual Machine to the bench/site front door that owns it.

Central mirrors VMs (the Asset), but the one-click login handoff — gateway_url,
login_url, its expiry — never lives on the pure-microVM `Virtual Machine`. It
lives on the tenant-owned aggregate that CREATED the VM: a `Pilot` (bench front
door) or a `Site` (self-serve site). Both were split off the VM for the same
reason and expose the same three handoff fields; this module is the single place
that, given a VM, finds whichever one backs it and reads the handoff uniformly.

The VM→front-door lookup was Pilot-only (`pilot_for_vm`), so a `create_site`
backing VM — owned by a Site, never a Pilot — surfaced as an Asset with no
login_url and a dead "Open". Resolving through EITHER aggregate fixes that
without merging the two DocTypes (spec/14-self-serve.md).

A `FrontDoor` normalizes the two shapes: `gateway_url` is `https://<fqdn>` for
both (the aggregate's name IS the fqdn), and the login handoff is surfaced only
once the aggregate is Running (before that the mint hasn't run — the same gate
the payloads already applied). A plain VM (proxy, operator machine) has no front
door → `front_door_for_vm` returns None and all three fields stay None, exactly
as before.
"""

from __future__ import annotations

import frappe

# The aggregates that own a backing VM and carry a login handoff, in resolution
# order. A VM is backed by at most one of these (its creator), so the first hit
# wins; order is immaterial for correctness.
_FRONT_DOOR_DOCTYPES = ("Pilot", "Site")


class FrontDoor:
	"""A VM's owning aggregate (Pilot or Site), normalized to the handoff shape the
	Asset mirror reads. Wraps the underlying doc so the caller reads gateway_url +
	the (Running-gated) login handoff without caring which DocType backs the VM."""

	def __init__(self, doc) -> None:
		self.doc = doc

	@property
	def running(self) -> bool:
		return self.doc.status == "Running"

	@property
	def status(self) -> str:
		# The authoritative status Central mirrors for a bench/site VM: the aggregate's,
		# NOT the raw VM's. The VM boots to Running the moment the microVM is up — before
		# deploy-site runs and the login handoff is minted — so the raw VM status would
		# report the Asset usable while it isn't. The aggregate flips Running only after
		# the in-guest mint, so its status gates usability. Both push (_vm_payload) and
		# pull (tenant_vms) read through here so the mirror never sees the premature boot.
		return self.doc.status

	@property
	def gateway_url(self) -> str:
		# What Central DEEP-LINKS: it opens this URL with a Central-signed `?sid=`, which
		# only the bench's admin console verifies. So for a Pilot the answer is its
		# console host, not its name. Those are the same thing for an admin-mode pilot
		# (including every attached console, whose name already carries `-pilot`), but a
		# site-mode pilot — a server — is named after the host serving the TENANT SITE,
		# and that site has no idea what the sid means: opening it drops the user on the
		# site's login page as Guest instead of in their bench.
		#
		# A bench-site Site has no console of its own; its name IS the fqdn (Contract A)
		# and its handoff is the one-click `login_url`, not the sid, so it keeps `name`. A
		# `pilot-console` Site is a console like a Pilot — same console_fqdn derivation.
		if self.doc.doctype == "Pilot":
			return f"https://{self.doc.console_fqdn}"
		if self.doc.doctype == "Site" and self.doc.kind == "pilot-console":
			from atlas.atlas.services import site_console

			return f"https://{site_console.console_fqdn(self.doc)}"
		return f"https://{self.doc.name}"

	@property
	def login_url(self) -> str | None:
		# Gated on Running: Atlas stamps the handoff only once the aggregate is serving,
		# so before that there is nothing to hand off (and the field may be unstamped).
		return self.doc.get("login_url") if self.running else None

	@property
	def login_url_expires_at(self):
		return self.doc.get("login_url_expires_at") if self.running else None

	def regenerate_login_url(self) -> dict:
		"""Re-mint the handoff — delegates to the aggregate's own whitelisted method
		(Pilot and Site both expose it with the same return shape)."""
		return self.doc.regenerate_login_url()


def front_door_for_vm(vm_name: str) -> FrontDoor | None:
	"""The Pilot or Site backing a VM as a `FrontDoor`, or None for a plain VM.

	The single VM→front-door resolver: replaces the Pilot-only `pilot_for_vm` at the
	Central seam so a Site-backed VM (create_site) resolves its login handoff too.

	Returns the FIRST match, CONSOLE before bench-site: a self-serve VM carries BOTH a
	bench-site Site and its console, and what Central deep-links is the console, not the
	tenant site. A console is a `Pilot` (transitional, until the Site/Pilot merge folds it)
	or a `Site(kind="pilot-console")`; a bench-site is a `Site(kind="bench-site")`. Use
	`front_doors_for_vm` when you need every aggregate rather than the one that owns the
	handoff."""
	lookups = (
		("Pilot", {"virtual_machine": vm_name}),
		("Site", {"virtual_machine": vm_name, "kind": "pilot-console"}),
		("Site", {"virtual_machine": vm_name}),
	)
	for doctype, filters in lookups:
		name = frappe.db.get_value(doctype, filters, "name")
		if name:
			return FrontDoor(frappe.get_doc(doctype, name))
	return None


def front_doors_for_vm(vm_name: str) -> list["FrontDoor"]:
	"""EVERY aggregate backed by this VM, not just the one that owns the handoff.

	A self-serve VM is backed by two: the `Site` (the tenant's Frappe site) and the
	attached `Pilot` (its admin console), which share the one microVM. `front_door_for_vm`
	answers "who owns the login handoff" and stops at the first; anything that must act on
	the VM's whole ownership — terminating it, above all — has to reach both, or one
	aggregate is left claiming to be Running over a VM that no longer exists."""
	doors = []
	for doctype in _FRONT_DOOR_DOCTYPES:
		for name in frappe.get_all(doctype, filters={"virtual_machine": vm_name}, pluck="name"):
			doors.append(FrontDoor(frappe.get_doc(doctype, name)))
	return doors
