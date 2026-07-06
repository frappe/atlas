"""Central-facing inventory read for the Asset-mirror reconcile (spec/16-central.md).

Central pulls the authoritative VM list per Atlas to correct any drift the event
push missed. One row per tenant-tagged VM: its id, the owning `team`, status, and
— for a bench VM — the front-door handoff read through its owning Pilot.
Operator-only (Central calls with its service operator key); untenanted operator
VMs are never returned.
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
			"image",
		],
	)

	# The front door lives on the aggregate that created the VM — a Pilot (bench) or a
	# Site (self-serve) — not on the VM. Fold it in per VM via the shared resolver so a
	# bench/site VM's row carries gateway_url (`https://<fqdn>`) and, once the aggregate is
	# Running, the login handoff. A VM with no front door (proxy, operator machine) leaves
	# all three None. Resolving through EITHER aggregate is what stops a Site-backed VM
	# (create_site) from reconciling into a login-less Asset (spec/14-self-serve.md).
	#
	# Same shape as central_report._vm_payload / _pilot_vm_payload so push and pull stay
	# in lockstep — including the login handoff (gateway_url + the login URL/expiry, the
	# latter only once Running, exactly as the event gates them). The reconcile is the
	# backstop if a status_changed event is lost, so it must carry them.
	from atlas.atlas.front_door import front_door_for_vm
	from atlas.atlas.placement import version_from_image

	rows = []
	for vm in vms:
		front_door = front_door_for_vm(vm.name)
		rows.append(
			{
				"name": vm.name,
				"team": vm.tenant,
				"title": vm.title,
				# A bench/site VM's status is the aggregate's, not the raw VM's: it boots to
				# Running before deploy-site + the login mint, so reporting the raw status
				# would reconcile the Asset to usable while it isn't. The push path gates the
				# same way (central_report._merge_bench_fields), so pull stays in lockstep.
				"status": front_door.status if front_door else vm.status,
				"vcpus": vm.vcpus,
				"memory_megabytes": vm.memory_megabytes,
				"disk_gigabytes": vm.disk_gigabytes,
				"ipv6_address": vm.ipv6_address,
				"public_ipv4": vm.public_ipv4,
				"frappe_version": version_from_image(vm.image),
				"gateway_url": front_door.gateway_url if front_door else None,
				"login_url": front_door.login_url if front_door else None,
				"login_url_expires_at": front_door.login_url_expires_at if front_door else None,
			}
		)
	return rows


@frappe.whitelist()
def available_frappe_versions() -> list[str]:
	"""Frappe versions Central can offer on the new-server form: the tokens of the
	active admin bench images (`bench-<token>-admin`). A Central "server" is a Pilot —
	the bench admin console — so its version picker is the versions that have an active
	*admin* image, not the plain `bench-<token>` site images (those back self-serve
	Sites, spec/14). Central derives its picker from this so the two never drift."""
	from atlas.atlas.placement import version_image_map

	return list(version_image_map())
