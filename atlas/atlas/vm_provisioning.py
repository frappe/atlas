"""Assembling a VM's provision Task variables — the desired-state payload the
`provision-vm.py` host script is driven with (spec/05-vm-lifecycle, spec/06-networking).

Extracted from the `Virtual Machine` controller: building the guest's boot/network/
jail/routing environment is one cohesive reason to change (the payload shape), separate
from the VM's lifecycle state machine. The controller keeps thin `_provision_variables`
/ `_guest_authorized_keys` methods that delegate here, since `migration` and the tests
call them on the doc. Every function reads a VM row and returns plain data — no writes.
"""

from __future__ import annotations

import ipaddress

import frappe

from atlas.atlas.networking import (
	cgroup_args,
	derive_ipv4_link,
	derive_netns,
	derive_private_address,
	derive_tenant_prefix,
	derive_uid,
	derive_veth_pair,
	resource_limit_args,
)


def ipv4_link_variables(vm) -> dict:
	"""The per-VM NAT44 egress link, derived from the v6 address — no
	stored field. The guest gets a private v4 + default route; the host
	masquerades it (see scripts/vm-network-up.py, spec/06-networking.md).
	Shared by provision (clone too) and rebuild, which both re-inject the
	guest network env.

	A dark VM (public_networking=0, §6) has NO public ipv6_address to index the
	/30 off, so it indexes off its private /128's low bits (per-host unique the
	same way the public allocator is: the private address is HKDF-derived, so we
	pass the explicit index). egress_nat44=0 opts a VM out of v4 egress entirely
	(air-gapped), so no link is emitted and vm-network-up skips the NAT block."""
	if not vm.egress_nat44:
		return {}
	if vm.ipv6_address:
		host_cidr, guest_cidr = derive_ipv4_link(vm.ipv6_address)
	else:
		# Dark VM: index off the private /128's low 14 bits (unique per host).
		index = int(ipaddress.IPv6Address(vm.private_address)) & 0x3FFF
		host_cidr, guest_cidr = derive_ipv4_link(index=index)
	return {
		"IPV4_HOST_CIDR": host_cidr,
		"IPV4_GUEST_CIDR": guest_cidr,
		"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
	}


def private_network_variables(vm) -> dict:
	"""The private-plane identity written into network.env (§5): the VM's derived
	fdaa:: /128 and its tenant /48. vm-network-up.py gates the whole private block on
	BOTH being present, so this is empty (and the block a no-op) for a VM with no
	tenant. Shared by provision + rebuild, which both re-inject the guest network env,
	so a rebuild re-creates the private routes + isolation rules on first boot."""
	if not vm.tenant:
		return {}
	private_address = vm.private_address or derive_private_address(vm.tenant, vm.name)
	return {
		"PRIVATE_ADDRESS": private_address,
		"TENANT_PREFIX": derive_tenant_prefix(vm.tenant),
	}


def data_disk_variables(vm) -> dict:
	"""The data-disk Task vars, shared by provision/rebuild/resize. Empty when
	the VM has no data disk (DATA_DISK_GB unset → the script's `0` default → no
	data disk created). DATA_DISK_FORMAT is "1"/"0" (an int flag, not a bool —
	the Task runner would render a bool as a truthy string); DATA_DISK_MOUNT_AT
	is empty when format-and-mount is off, so the script skips the fstab line."""
	if not vm.data_disk_gigabytes:
		return {}
	return {
		"DATA_DISK_GB": str(vm.data_disk_gigabytes),
		"DATA_DISK_FORMAT": "1" if vm.data_disk_format_and_mount else "0",
		"DATA_DISK_MOUNT_AT": vm.data_disk_mount_point if vm.data_disk_format_and_mount else "",
	}


def guest_authorized_keys(vm) -> str:
	"""The guest's root authorized_keys: the VM owner's key plus an external
	service's (e.g. chef) key(s) (spec/28), one per line. Atlas hands over a bare
	Ubuntu box; injecting the service's key here is what lets the service SSH in and
	set up services. The rootfs writes this value verbatim, so each extra line is one
	more authorized key. No-op (just the owner's key) on an Atlas with no such service."""
	from atlas.atlas.atlas_settings import service_public_keys

	keys = [vm.ssh_public_key, *service_public_keys()]
	return "\n".join(key.strip() for key in keys if key and key.strip())


def cgroup_values(interleaved: list[str]) -> list[str]:
	"""Drop the flag tokens from networking.cgroup_args/resource_limit_args,
	which interleave `["--cgroup", "<value>", "--cgroup", "<value>"]`. The
	provision task wants values only — it owns the --cgroup / --resource-limit
	prefix when it builds the per-VM launcher — so keep every token that is not
	itself a flag (does not start with '--')."""
	return [token for token in interleaved if not token.startswith("--")]


def routing_base_url() -> str:
	"""The Satellite orchestrator base URL a guest's routing client POSTs to (spec/28:
	routing moved off Atlas to the Satellite).

	Read from `Atlas Settings.satellite_routing_base_url` — the Satellite's public site
	URL (e.g. `https://orchestrator.blr1.frappe.dev`). Returns "" when unset, which the
	Task runner drops, leaving /etc/atlas-routing.env unwritten and the guest client a
	clean no-op (an Atlas with no Satellite, or before the URL is configured). NON-SECRET,
	so there is no harm in injecting it broadly."""
	return frappe.db.get_single_value("Atlas Settings", "satellite_routing_base_url") or ""


def provision_variables(vm) -> dict:
	image = frappe.get_doc("Virtual Machine Image", vm.image)
	host_veth, namespace_veth = derive_veth_pair(vm.name)
	variables = {
		"VIRTUAL_MACHINE_NAME": vm.name,
		"IMAGE_NAME": vm.image,
		"KERNEL_FILENAME": image.kernel_filename,
		"ROOTFS_FILENAME": image.rootfs_filename,
		"VCPUS": str(vm.vcpus),
		"MEMORY_MB": str(vm.memory_megabytes),
		"DISK_GB": str(vm.disk_gigabytes),
		"MAC_ADDRESS": vm.mac_address,
		"TAP_DEVICE": vm.tap_device,
		"VIRTUAL_MACHINE_IPV6": vm.ipv6_address,
		"SSH_PUBLIC_KEY": guest_authorized_keys(vm),
		# Jail isolation parameters. All derived from the VM's own UUID and
		# resource fields, so the on-host jail is reconstructible from the
		# row. provision-vm.py bakes these into the per-VM jailer-launch.sh
		# (exec'd by the systemd unit) and writes network.env (read by
		# vm-network-up.py) from them.
		"ATLAS_FC_UID": str(derive_uid(vm.name)),
		"ATLAS_NETNS": derive_netns(vm.name),
		"HOST_VETH": host_veth,
		"NAMESPACE_VETH": namespace_veth,
		# cgroup/resource LIMITS as values-only lists. The runner renders each
		# as a repeatable CLI flag (--cgroup-arg <value>); provision-vm.py
		# prefixes each with --cgroup / --resource-limit when it builds the
		# per-VM launcher. A value with an internal space (cpu.max's "<quota>
		# <period>") is one argv token end to end — no systemd word-splitting,
		# so the shell's newline-join + mapfile workaround is gone.
		"CGROUP_ARG": cgroup_values(
			cgroup_args(
				vm.cpu_max_cores,
				vm.memory_megabytes,
				vm.disk_gigabytes,
				vm.cpu_mode,
				vm.vcpus,
			)
		),
		"RESOURCE_ARG": cgroup_values(resource_limit_args(vm.disk_gigabytes)),
		# Per-VM NAT44 v4 egress link (host/guest /30 + gateway). Empty when
		# egress_nat44=0 (an air-gapped VM), leaving the env's v4 block unwritten.
		**ipv4_link_variables(vm),
		# The private-plane identity on the WireGuard host mesh (§5): the derived
		# fdaa:: /128 + tenant /48. Empty for a tenant-less VM, so vm-network-up
		# skips the whole private block and the VM keeps today's public-only behavior.
		**private_network_variables(vm),
		# An attached Reserved IP (if any) so a fresh provision re-creates its
		# inbound 1:1-NAT on first boot. Empty/None is dropped by the Task
		# runner's flag rendering, leaving the env clean for ordinary VMs.
		"RESERVED_IPV4": vm.public_ipv4,
		# The Atlas controller base URL written into the guest at
		# /etc/atlas-routing.env — the trusted-edge FQDN a bench VM's in-guest routing
		# client POSTs the register/deregister/check_label/list endpoints to (spec/18).
		# NON-SECRET — uniform on every VM, like the MMDS device: a non-bench VM's guest
		# client simply has no choke point that calls it. Empty (no request context,
		# e.g. a bare `bench execute`) is dropped by the Task runner, leaving the env
		# clean.
		"ROUTING_BASE_URL": routing_base_url(),
	}
	# Clone: seed the disk from a snapshot's rootfs instead of the pristine
	# image. The kernel still comes from the image; provision-vm.py's image
	# probe (step 0) stays meaningful. Identity is re-derived from this VM's
	# own UUID, so the clone never shares host keys / machine-id with its
	# source.
	if vm.clone_source_rootfs:
		variables["SNAPSHOT_ROOTFS_PATH"] = vm.clone_source_rootfs
	# Warm clone: provision-vm.py additionally stages the golden memory pair
	# behind a READY marker and this VM's identity as MMDS metadata, and the
	# disk stays a byte-exact CoW (no grow/inject — the frozen RAM must keep
	# matching it). The tap NAME already flows above: clone_to_new_vm pinned
	# vm.tap_device to the golden's (the vmstate binds the tap by name).
	if vm.warm_snapshot:
		variables["WARM_SNAPSHOT_DIRECTORY"] = frappe.db.get_value(
			"Virtual Machine Snapshot", vm.warm_snapshot, "memory_directory"
		)
	# Data disk (the root disk's peer): size + format/mount config, plus —
	# when cloning — the data-disk snapshot to seed it from, so the clone's
	# /home comes up with the source's data.
	variables.update(data_disk_variables(vm))
	if vm.clone_source_data_rootfs:
		variables["DATA_SNAPSHOT_ROOTFS_PATH"] = vm.clone_source_data_rootfs
	return variables
