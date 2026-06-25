"""Central-facing provisioning — the operator entry point Central calls to lay
down a tenant VM.

Central owns end-users; it talks to Atlas as the operator (token auth as the
Central service user). It supplies *what* to run (the tenant it belongs to + the
size), never *where* — placement (server) and the base image are Atlas's
concern, filled by `VirtualMachine.before_insert` via `apply_user_defaults`. The
insert's `after_insert` enqueues the provision job, so the VM provisions itself
through the configured provider (the `fake` provider in dev).

This is the write half of the Central↔Atlas tenancy contract whose read half is
the Tenant DocType (resources stamped with `central_reference`). It returns the
VM in the exact shape Central's Asset mirror upserts, so Central can reflect the
new server immediately without waiting for a reconcile.
"""

from __future__ import annotations

import frappe

from atlas.atlas.doctype.tenant.tenant import ensure_tenant
from atlas.bootstrap import load_vm_ssh_public_key


@frappe.whitelist()
def create_vm(
	central_reference: str,
	title: str,
	vcpus: int,
	memory_megabytes: int,
	disk_gigabytes: int,
	email: str | None = None,
	cpu_max_cores: float | None = None,
	ssh_public_key: str | None = None,
) -> dict:
	"""Provision a VM for a Central team and return its mirror row.

	`central_reference` is the Central team; `email` seeds the Tenant on first
	use (the team owner). Resources come from the size Central picked. Placement,
	image, ipv6, cpu/mac defaults and auto-provisioning are all handled by the
	Virtual Machine controller — we only insert. Runs with `ignore_permissions`:
	this is operator orchestration authorized by the Central token, not desk RBAC.
	"""
	if not central_reference:
		frappe.throw("central_reference is required.")

	tenant = ensure_tenant(central_reference, email)

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
		"central_reference": central_reference,
		"status": vm.status,
		"title": vm.title,
		"vcpus": vm.vcpus,
		"memory_megabytes": vm.memory_megabytes,
		"disk_gigabytes": vm.disk_gigabytes,
		"ipv6_address": vm.ipv6_address,
		"public_ipv4": vm.public_ipv4,
		"gateway_url": None,
	}
