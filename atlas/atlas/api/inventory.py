"""Central-facing inventory read for the Asset-mirror reconcile (spec/16-central.md).

Central pulls the authoritative VM list per Atlas to correct any drift the event
push missed. One row per tenant-tagged VM: its id, the owning `team`, status, and
gateway_url. Operator-only (Central calls with its service operator key);
untenanted operator VMs are never returned.
"""

import frappe


@frappe.whitelist()
def tenant_vms(team: str | None = None) -> list[dict]:
	"""Tenant-tagged VMs, optionally scoped to one `team` (the Central `Team.name`)."""
	frappe.only_for("System Manager")

	# The Tenant `name` *is* the Central `Team.name`, so the VM's `tenant` link is the
	# owning team directly — scope on it with no Tenant lookup.
	vm_filter = {"tenant": team} if team else {"tenant": ["is", "set"]}

	vms = frappe.get_all(
		"Virtual Machine",
		filters=vm_filter,
		fields=[
			"name",
			"tenant",
			"title",
			"status",
			"vcpus",
			"memory_megabytes",
			"disk_gigabytes",
			"ipv6_address",
			"public_ipv4",
		],
	)
	# Same shape as central_report._vm_payload so push and pull stay in lockstep.
	return [
		{
			"name": vm.name,
			"team": vm.tenant,
			"title": vm.title,
			"status": vm.status,
			"vcpus": vm.vcpus,
			"memory_megabytes": vm.memory_megabytes,
			"disk_gigabytes": vm.disk_gigabytes,
			"ipv6_address": vm.ipv6_address,
			"public_ipv4": vm.public_ipv4,
			"gateway_url": None,
		}
		for vm in vms
	]
