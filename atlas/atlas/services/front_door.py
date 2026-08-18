"""Resolve a Virtual Machine to the Site front door that owns it.

Central mirrors VMs (the Asset), but the one-click login handoff — gateway_url,
login_url, its expiry — never lives on the pure-microVM `Virtual Machine`. It
lives on the tenant-owned `Site` that CREATED the VM: a bench-site (self-serve
site) or its pilot-console (bench admin console) — two kinds of the one Site
DocType. This module is the single place that, given a VM, finds whichever Site
backs it and reads the handoff uniformly.

A `FrontDoor` normalizes the shape: a bench-site's `gateway_url` is `https://<fqdn>`
(its name IS the fqdn), a pilot-console's is its derived console host; the login
handoff is surfaced only once the Site is Running (before that the mint hasn't run —
the same gate the payloads already applied). A plain VM (proxy, operator machine)
has no front door → `front_door_for_vm` returns None and all three fields stay None.
"""

from __future__ import annotations

import frappe

class FrontDoor:
	"""A VM's owning Site (a bench-site or its pilot-console), normalized to the handoff
	shape the Asset mirror reads. Wraps the underlying doc so the caller reads gateway_url
	+ the (Running-gated) login handoff without caring which kind backs the VM."""

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
		# A bench-site has no console of its own; its name IS the fqdn (Contract A) and its
		# handoff is the one-click `login_url`, not the sid, so it keeps `name`. A
		# pilot-console derives its console host from build_mode (site-mode adds `-pilot`).
		if self.doc.get("kind") == "pilot-console":
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
		"""Re-mint the handoff — delegates to the Site's own whitelisted method (both kinds
		expose it with the same return shape, dispatched on kind)."""
		return self.doc.regenerate_login_url()


def front_door_for_vm(vm_name: str) -> FrontDoor | None:
	"""The Site backing a VM as a `FrontDoor`, or None for a plain VM.

	The single VM→front-door resolver at the Central seam, so any Site-backed VM
	(create_site or create_vm) resolves its login handoff.

	Returns the FIRST match, CONSOLE before bench-site: a self-serve VM carries BOTH a
	bench-site Site and its pilot-console, and what Central deep-links is the console, not
	the tenant site. Use `front_doors_for_vm` when you need every aggregate rather than the
	one that owns the handoff."""
	lookups = (
		{"virtual_machine": vm_name, "kind": "pilot-console"},
		{"virtual_machine": vm_name},
	)
	for filters in lookups:
		name = frappe.db.get_value("Site", filters, "name")
		if name:
			return FrontDoor(frappe.get_doc("Site", name))
	return None


def front_doors_for_vm(vm_name: str) -> list["FrontDoor"]:
	"""EVERY Site backed by this VM, not just the one that owns the handoff.

	A self-serve VM is backed by two Sites: the bench-site (the tenant's Frappe site) and
	its attached pilot-console (the admin console), which share the one microVM.
	`front_door_for_vm` answers "who owns the login handoff" and stops at the first;
	anything that must act on the VM's whole ownership — terminating it, above all — has to
	reach both, or one is left claiming to be Running over a VM that no longer exists."""
	return [
		FrontDoor(frappe.get_doc("Site", name))
		for name in frappe.get_all("Site", filters={"virtual_machine": vm_name}, pluck="name")
	]
