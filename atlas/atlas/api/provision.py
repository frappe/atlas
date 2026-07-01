"""Central-facing provisioning — the operator entry point Central calls to lay
down a tenant VM.

Central owns end-users; it talks to Atlas as the operator (token auth as the
Central service user). It supplies *what* to run (the tenant it belongs to + the
size), never *where* — placement (server) and the base image are Atlas's
concern, filled by `VirtualMachine.before_insert` via `apply_user_defaults`. The
insert's `after_insert` enqueues the provision job, so the VM provisions itself
through the configured provider (the `fake` provider in dev).

This is the write half of the Central↔Atlas tenancy contract whose read half is
the Tenant DocType (resources stamped with the owning `team`). It returns the VM
in the exact shape Central's Asset mirror upserts, so Central can reflect the new
server immediately without waiting for a reconcile.
"""

from __future__ import annotations

import frappe

from atlas.atlas.doctype.tenant.tenant import ensure_tenant
from atlas.bootstrap import load_vm_ssh_public_key


@frappe.whitelist()
def create_vm(
	team: str,
	title: str,
	vcpus: int,
	memory_megabytes: int,
	disk_gigabytes: int,
	email: str | None = None,
	cpu_max_cores: float | None = None,
	ssh_public_key: str | None = None,
) -> dict:
	"""Provision a VM for a Central team and return its mirror row.

	`team` is the Central `Team.name`; `email` seeds the Tenant on first use (the
	team owner). Resources come from the size Central picked. Placement, image,
	ipv6, cpu/mac defaults and auto-provisioning are all handled by the Virtual
	Machine controller — we only insert. Runs with `ignore_permissions`: this is
	operator orchestration authorized by the Central token, not desk RBAC.
	"""
	if not team:
		frappe.throw("team is required.")

	tenant = ensure_tenant(team, email)

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": title or "server",
			"tenant": tenant,
			"vcpus": int(vcpus),
			"memory_megabytes": int(memory_megabytes),
			"disk_gigabytes": int(disk_gigabytes),
			"ssh_public_key": ssh_public_key or load_vm_ssh_public_key(),
		}
	)

	if cpu_max_cores:
		vm.cpu_max_cores = float(cpu_max_cores)
	vm.insert(ignore_permissions=True)

	# Shape matches central.atlas._mirror_vm so Central can upsert verbatim.
	return {
		"name": vm.name,
		"team": team,
		"status": vm.status,
		"title": vm.title,
		"vcpus": vm.vcpus,
		"memory_megabytes": vm.memory_megabytes,
		"disk_gigabytes": vm.disk_gigabytes,
		"ipv6_address": vm.ipv6_address,
		"public_ipv4": vm.public_ipv4,
		"gateway_url": None,
	}


@frappe.whitelist()
def capacity() -> dict:
	"""What can this region provision right now? — Central's pre-create check.

	Central speaks in resources (CPU / RAM / disk), not Atlas size presets, and
	never sees hosts — placement is Atlas's concern. So this answers two things in
	resource terms:

	- `available`: can *some* Active host seat a minimal VM? Central shows
	  "Capacity not available" when False. Checked via `largest_vm` returning a
	  shape at all — an Active host exists with room.
	- `largest_vm`: the biggest single VM shape placeable right now —
	  `{vcpus, memory_megabytes, disk_gigabytes}` — the free headroom on the best
	  host (a VM lands on one host, so this is a real co-schedulable shape, not a
	  fleet sum). `null` when no Active host exists.

	`unmeasured` is True when the winning host has an axis the on-host agent hasn't
	reported yet: `largest_vm` then contains large sentinel values, not
	measurements, and Central should treat the shape as "effectively unlimited /
	size unknown" rather than a fact. It goes False once the agent stamps totals.

	`available` reuses placement's real gate (`default_server`) for the smallest
	provisionable VM, so the pre-check and the create-time gate can never disagree
	on logic, only on timing.

	Advisory: the authoritative gate is placement's NoCapacityError at create time
	(capacity can change between this call and the create). Runs with the Central
	token, like create_vm — operator orchestration, not desk RBAC.
	"""
	from atlas.atlas.placement import NoCapacityError, default_server
	from atlas.atlas.placement import largest_vm as _largest_vm
	from atlas.atlas.sizes import SIZE_PRESETS

	# Floor of "can we provision anything?" — the smallest preset must fit some
	# host under the same predicate the create path uses.
	smallest = next(iter(SIZE_PRESETS.values()))
	try:
		default_server(
			float(smallest["cpu_max_cores"]),
			float(smallest["memory_megabytes"]),
			float(smallest["disk_gigabytes"]),
		)
		available = True
	except NoCapacityError:
		available = False

	shape = _largest_vm()
	if shape is None:
		return {"available": False, "unmeasured": False, "largest_vm": None}

	unmeasured = shape.pop("unmeasured")
	return {
		"available": available,
		"unmeasured": unmeasured,
		"largest_vm": shape,
	}
