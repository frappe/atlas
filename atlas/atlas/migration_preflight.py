"""The cheap, synchronous pre-flight gate for a migration and the address-scheme
policy behind it. All DB-answerable checks (no host SSH): they reject before a
Migration row is even made. Extracted from migration.py so the saga file carries
the phase machine, not the admission policy.

`preflight_checks` and `_will_keep_address` are the two names other modules
reach (VirtualMachine.migrate calls the gate; the Migration row's before_insert
asks the scheme); the two `_assert_*` helpers are internal to the gate.
"""

from __future__ import annotations

import ipaddress

import frappe
from frappe import _

from atlas.atlas.networking import address_is_free_on_server


def preflight_checks(vm, target_server: str, release_reserved_ip: bool) -> None:
	"""The cheap, synchronous gate. On-host checks (image present, pool headroom,
	kernel modules) run in ExportingSnapshot/TargetPreparing where SSH is in hand;
	these are the DB-answerable ones that should reject before a row is even made."""
	from atlas.atlas.doctype.virtual_machine_migration.virtual_machine_migration import (
		active_migration_for,
	)
	from atlas.atlas.placement import assert_visible

	if active_migration_for(vm.name):
		frappe.throw("This VM already has an in-flight migration")
	if vm.status == "Sleeping":
		# A sleeping VM's RAM lives in an on-host memory snapshot that is not
		# transportable between hosts (spec/32, and the non-goal in ch.24), so
		# there is nothing to migrate until it is resumed or discarded.
		frappe.throw(_("Cannot migrate a sleeping VM — wake or stop it first"))
	if vm.status not in ("Stopped", "Running", "Paused"):
		frappe.throw(f"Cannot migrate from {vm.status}")
	if vm.server == target_server:
		frappe.throw("VM is already on that server")

	target = frappe.db.get_value("Server", target_server, ["status", "provider_type"], as_dict=True)
	if not target:
		frappe.throw(f"Target server {target_server} does not exist")
	if target.status != "Active":
		frappe.throw(f"Target server {target_server} is not Active (status is {target.status})")
	# A migration target is an ARRIVAL, and every arrival goes through the placement
	# gate (spec/33 §9). Active alone let a LIVE VM be moved onto a host Atlas had
	# lost sight of — the one arrival where "it will fail loudly on the box" is not
	# an acceptable answer, because the VM is already stopped by then. The SOURCE is
	# deliberately not gated: moving a VM OFF an unseen host is the migration an
	# operator most wants to be able to run.
	assert_visible(target_server)

	# Same provider: cross-provider migration is out of scope. The Server's own
	# frozen `provider_type` is the vendor (a real column, not a derived property).
	source_provider = frappe.db.get_value("Server", vm.server, "provider_type")
	if source_provider != target.provider_type:
		frappe.throw(
			"Cross-provider migration is out of scope (source and target must share a provider): "
			f"{source_provider} != {target.provider_type}"
		)
	# Region is same-by-construction: one region per Atlas instance (spec/24 §1),
	# and Subdomain has no region field. Nothing to compare.

	# IPv6 on the target. Two schemes, two different gates (both probed here, read-only
	# intent, so the operator learns at click time rather than three phases deep):
	#   - change-address: allocate_ipv6 raises if the range is full. Authoritative
	#     allocation is in InjectingIdentity.
	#   - keep-address: no address is allocated (the VM keeps its /128, the source
	#     forwards it), so range fullness is irrelevant — BUT the kept /128 must not
	#     already be live on a DIFFERENT VM on the target, or the two collide on one
	#     host (a single `<vmv6>/128 dev <veth>` route can point at only one; the
	#     other VM silently steals the traffic — observed in the field). Authoritative
	#     re-check is in InjectingIdentity.
	if _will_keep_address(vm.server, target_server):
		_assert_kept_address_free(vm, target_server)
	else:
		_assert_ipv6_capacity(target_server)

	if vm.public_ipv4 and not release_reserved_ip:
		frappe.throw(
			"This VM has an attached public IPv4 (Reserved IP) bound to the source host. "
			"Stage-1 migration cannot move it; pass release_reserved_ip=True to acknowledge "
			"inbound v4 will be released, then re-attach a target-server Reserved IP afterward."
		)


def _will_keep_address(source_server: str, target_server: str) -> bool:
	"""Whether a migration between these two servers keeps the VM's /128 (spec/24
	§2.8). True iff BOTH hosts' provider can forward a /128 from the source
	(vm_range_is_forwardable). The single source of truth for the address scheme,
	shared by pre-flight (to skip the target-capacity check) and the Migration row's
	before_insert (to set keep_address/forward_address)."""
	from atlas.atlas.providers import for_provider_type

	provider_type = frappe.db.get_value("Server", source_server, "provider_type")
	provider = for_provider_type(provider_type)
	source_resource = frappe.db.get_value("Server", source_server, "provider_resource_id")
	target_resource = frappe.db.get_value("Server", target_server, "provider_resource_id")
	return bool(
		provider.vm_range_is_forwardable(source_resource)
		and provider.vm_range_is_forwardable(target_resource)
	)


def _assert_ipv6_capacity(server: str) -> None:
	"""Probe-only. allocate_ipv6 holds the Server row for_update and would actually
	consume a slot, so we replicate its capacity question read-only: is there a free
	address in the range? The authoritative gate is allocate_ipv6() in
	InjectingIdentity; a race that fills the last slot between now and then is caught
	there and fails that migration cleanly."""
	network = ipaddress.IPv6Network(frappe.db.get_value("Server", server, "ipv6_virtual_machine_range"))
	used = {
		str(ipaddress.IPv6Address(address))
		for address in frappe.get_all(
			"Virtual Machine",
			filters={"server": server, "status": ["!=", "Terminated"]},
			pluck="ipv6_address",
		)
		if address
	}
	for index, candidate in enumerate(network.hosts()):
		if index < 1:  # ::1 is the host
			continue
		if str(candidate) not in used:
			return
	frappe.throw(f"Target server {server} has no free IPv6 address in its range")


def _assert_kept_address_free(vm, target_server: str) -> None:
	"""keep-address gate: the VM's /128 must not already be live on a different VM on
	the target. Excludes the migrating VM's own row (a resume may have denormalized it
	onto the target already). The target's own native VMs allocate from ::2 up, so a
	source-::2 VM kept onto a target that already has a ::2 VM is a guaranteed
	collision — this is the check that stops it before the disks move."""
	if not address_is_free_on_server(target_server, vm.ipv6_address, ignore_vm=vm.name):
		frappe.throw(
			f"Cannot keep address {vm.ipv6_address}: target server {target_server} already "
			f"hosts a live VM on that /128. Two VMs cannot share a /128 on one host. "
			f"Terminate the conflicting VM or migrate to a different target."
		)
