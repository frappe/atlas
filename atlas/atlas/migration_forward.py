"""Collapse a VM's keep-address forward back to change-address (spec/24 §2.9.5).

Extracted from migration.py: the forward-collapse is an operator-initiated,
out-of-band teardown, not a phase of the migration saga, so it lives on its own.
The final routing re-point (Subdomain denorm + proxy reconcile) fires core's
`vm.address_changed` callback — the same PaaS-blind seam a change-address
cutover uses — so this module never imports the routing/proxy code directly.

The host work still runs through `run_boat_migration_phase` / `run_task`, so
tests patch those seams on THIS module.
"""

from __future__ import annotations

import ipaddress

import frappe

from atlas.atlas.boat_client import run_boat_migration_phase
from atlas.atlas.core import callbacks
from atlas.atlas.networking import (
	allocate_ipv6,
	derive_ipv4_link,
	derive_vm_tunnel,
	derive_vm_tunnel_port,
	derive_vm_tunnel_table,
)
from atlas.atlas.ssh import run_task


def collapse_forward(vm) -> None:
	"""Tear down a VM's keep-address forward and fall it back to change-address
	(spec/24 §2.9.5). The forward is permanent by default; this is the ONLY point at
	which a kept address can still change, and it is entirely operator-initiated
	(via the VM-form Collapse-forward button). Steps, in order:

	  1. Tear the tunnel down on BOTH hosts — the target's return-rule + table, then
	     the source's route/nft/(DO)proxy-NDP, then the tunnel device + socat.
	  2. Allocate a NEW /128 from the CURRENT (post-migration) server's range and
	     re-provision the VM in place to inject it, preserving host keys — the same
	     shape a change-address cutover uses, but the disk is already local so
	     provision-vm just rewrites network.env + relaunches the unit on the new /128.
	  3. Re-point every Subdomain to the new /128 and reconcile the proxy fleet.
	  4. Clear the VM's forward markers.

	Idempotent enough to retry: a re-invoked collapse re-runs best-effort teardown
	(the down scripts tolerate missing state), and step 2's allocate is skipped once
	the VM already sits on a fresh in-range /128. The source host is the VM's
	traffic_forwarded_from; the current host is vm.server."""
	source_server = vm.traffic_forwarded_from
	if not source_server:
		frappe.throw(f"Virtual Machine {vm.name} has no active forward to collapse")

	tunnel_device = derive_vm_tunnel(vm.name)
	tunnel_port = derive_vm_tunnel_port(vm.name)
	route_table = derive_vm_tunnel_table(vm.name)
	old_ipv6 = vm.ipv6_address

	# 1a. Target end (the VM's current host): remove the return-route policy.
	run_boat_migration_phase(
		server=vm.server,
		script="migration-forward-down",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"VIRTUAL_MACHINE_IPV6": old_ipv6,
			"ROLE": "target",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
			"ROUTE_TABLE": str(route_table),
		},
		virtual_machine=vm.name,
		timeout_seconds=60,
	)
	# 1b. Source end: remove the /128 route, nft rules, and the proxy-NDP entry.
	#     Deassert proxy-NDP for EVERY provider (mirror of the unconditional re-assert
	#     in _install_forward_routes) — the source answered NDP for the /128 while
	#     forwarding, so collapse must stop it on all providers, not just DigitalOcean.
	run_boat_migration_phase(
		server=source_server,
		script="migration-forward-down",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"VIRTUAL_MACHINE_IPV6": old_ipv6,
			"ROLE": "source",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
			"DEASSERT_PROXY_NDP": "1",
		},
		virtual_machine=vm.name,
		timeout_seconds=60,
	)

	# 2. Allocate a fresh /128 on the current host and re-provision the VM onto it.
	#    Skip the allocate if a prior collapse attempt already moved the VM off
	#    old_ipv6.
	new_ipv6 = vm.ipv6_address
	if new_ipv6 == old_ipv6:
		new_ipv6 = allocate_ipv6(vm.server)
	variables = vm._provision_variables()
	host_cidr, guest_cidr = derive_ipv4_link(new_ipv6)
	variables.update(
		{
			"VIRTUAL_MACHINE_IPV6": new_ipv6,
			"IPV4_HOST_CIDR": host_cidr,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
			"PRESERVE_HOST_KEYS": "1",
		}
	)
	# STOP the VM first, for two reasons: (a) collapse runs on a LIVE VM, and
	# provision-vm's `systemctl start` is a no-op on an already-running unit — the
	# guest would never reboot onto the new /128 (its host veth route + guest eth0 are
	# re-laid only at unit (re)start). (b) A boot-then-hydrate migration left the disk
	# behind a collapsed-linear dm-clone that holds the plain LV BUSY; stop-vm CONVERGES
	# that clone (removes it once the guest's fd is released), so the plain LV is then
	# directly mountable and provision-vm's ordinary inject+launch just works. This is a
	# brief operator-initiated blip, not a latency-critical cutover.
	vm.reload()
	if vm.status == "Running":
		vm.flags.migrating = True
		vm.stop(memory_snapshot=False)
	run_task(
		server=vm.server,
		script="provision-vm",
		variables=variables,
		virtual_machine=vm.name,
		timeout_seconds=120,
	)

	# 3. Commit the new address on the VM row, clear the forward markers, then
	#    re-point the Subdomains at it (the change-address path — now the address
	#    really did change). db_set (not save): it bypasses the optimistic-lock
	#    timestamp check — the long-running host tasks above leave a stale in-memory
	#    doc, and a trailing migration self-drive tick may have touched the row in the
	#    meantime (a save() would raise TimestampMismatchError) — and it skips the
	#    validate() immutability gate on ipv6_address cleanly (these are the sanctioned
	#    post-cutover writes, like _finalize_cutover's).
	vm.db_set(
		{
			"ipv6_address": new_ipv6,
			"status": "Running",
			# Same walk-back _finalize_cutover does: the stop above stated Stopped, and
			# the re-provision has the guest live again, so the intent must follow.
			"desired_power": "Running",
			"traffic_forwarded_from": None,
			"traffic_forwarded_since": None,
		}
	)
	# Same routing re-point a change-address cutover fires — the address really
	# did change here too. Core is PaaS-blind: services registered the
	# `vm.address_changed` handler (Subdomain denorm + proxy reconcile).
	callbacks.run("vm.address_changed", vm.name, new_ipv6)
