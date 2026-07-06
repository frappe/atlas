"""Central-facing provisioning — the operator entry point Central calls to lay
down a tenant VM.

Central owns end-users; it talks to Atlas as the operator (token auth as the
Central service user). It supplies *what* to run (the tenant it belongs to + the
size), never *where* — placement (server) and the base image are Atlas's concern.

WIRE SHAPE (unchanged) vs. WHAT'S BEHIND IT (changed): Central still calls
`create_vm` and mirrors a VM-shaped row — `name`, `ipv6_address`, `gateway_url`,
`login_url`, etc. But a *bench* VM (a baked-image tenant environment) is now owned
by a `Pilot` DocType, not the `Virtual Machine` itself: the bench provision (boot a
bench image, deploy in-guest, mint the one-click login URL) lives on the Pilot so
the Virtual Machine stays a pure microVM. So `create_vm` creates a **Pilot** (which
creates and owns the VM), and the bench fields in the mirror row — `gateway_url`
(`https://<subdomain>.<region domain>`) and, once Running, `login_url` + its expiry
— are read back THROUGH the Pilot. The plain VM facts (name, ipv6) are read through
the VM the Pilot created. Central sees the same VM-shaped payload as before.

This is the write half of the Central↔Atlas tenancy contract whose read half is
the Tenant DocType (resources stamped with the owning `team`). It returns the VM
in the exact shape Central's Asset mirror upserts, so Central can reflect the new
server immediately without waiting for a reconcile.
"""

from __future__ import annotations

import frappe

from atlas.atlas.doctype.tenant.tenant import ensure_tenant


@frappe.whitelist()
def create_vm(
	team: str,
	title: str,
	vcpus: int,
	memory_megabytes: int,
	disk_gigabytes: int,
	email: str | None = None,
	cpu_max_cores: float | None = None,
	frappe_version: str | None = None,
) -> dict:
	"""Provision a bench VM for a Central team and return its (VM-shaped) mirror row.

	`team` is the Central `Team.name`; `email` seeds the Tenant on first use (the
	team owner). Resources come from the size Central picked. `title` is the single
	DNS label Central chose — it doubles as the pilot's subdomain: Atlas fronts the
	bench at `<title>.<region domain>` (derived, never stored — the same Contract-A
	rule `create_site` uses, so region/domain never leave Atlas).

	Creates a `Pilot`, which owns the backing VM: the Pilot's `before_insert`
	validates the label (a bad one fails at the boundary) and its `after_insert`
	creates the VM synchronously (so its identity is available for this return) and
	enqueues the boot→deploy→mint job. Runs with `ignore_permissions`: operator
	orchestration authorized by the Central token, not desk RBAC.
	"""
	if not team:
		frappe.throw("team is required.")

	from atlas.atlas.placement import image_for_version, version_from_image

	tenant = ensure_tenant(team, email)

	spec = {
		"vcpus": int(vcpus),
		"memory_megabytes": int(memory_megabytes),
		"disk_gigabytes": int(disk_gigabytes),
		# The Frappe version Central picked selects the bench image; an unknown/unbuilt
		# version resolves to the default, so it never blocks the create. Server placement
		# stays Atlas's concern: the Pilot's _provision_backing_vm picks a server that HOLDS
		# this image (placement.default_server_for_image) rather than a hard-coded UUID — a
		# local bench image lives only where it was baked/exported, so the host is chosen
		# from the image's home set.
		"image": image_for_version(frappe_version),
	}
	if cpu_max_cores:
		spec["cpu_max_cores"] = float(cpu_max_cores)

	pilot = frappe.get_doc({"doctype": "Pilot", "subdomain": title or "server", "tenant": tenant})
	# The VM spec rides the insert (flags → after_insert) to the VM the Pilot creates;
	# it is never persisted on the Pilot row, which stores only bench-level state.
	pilot.flags.vm_spec = spec
	pilot.insert(ignore_permissions=True)

	# The Pilot created its VM in after_insert; read the plain VM facts through the
	# link and the bench fields off the Pilot. Shape matches central.atlas._mirror_vm
	# so Central can upsert verbatim. login_url is minted after boot (auto_provision),
	# so it (and its expiry) are empty here — Central learns them from the event.
	vm = frappe.get_doc("Virtual Machine", pilot.virtual_machine)
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
		"gateway_url": pilot.gateway_url,
		# The version actually laid down (from the resolved image), so Central mirrors
		# ground truth — not merely what it requested.
		"frappe_version": version_from_image(vm.image),
	}


@frappe.whitelist()
def regenerate_vm_login(name: str) -> dict:
	"""Re-mint a bench VM's one-click login URL and return its (VM-shaped) mirror row.

	Central calls this on Open when the Asset's stored URL has expired or never
	arrived (the admin JWT lasts 5 minutes, so a login is almost always a fresh mint).
	Central knows the VM only — it mirrors VMs — but the login URL lives on the front
	door that owns the VM (a `Pilot` for a bench, a `Site` for a self-serve site), not
	the pure-microVM `Virtual Machine`. So this resolves the VM to its front door,
	re-mints in the guest via its `regenerate_login_url` (re-stamps + commits), then
	returns the VM-shaped payload — always the Asset shape Central re-reads, whichever
	aggregate backs the VM (the Site's own regenerate returns a site-shaped mirror; the
	Asset caller needs the VM shape keyed by VM id, so we re-derive it here).

	Raises if the VM has no front door (a plain proxy/operator VM has no login to
	regenerate) — Central only ever calls this for a bench/site Asset.
	"""
	from atlas.atlas.central_report import _vm_payload
	from atlas.atlas.front_door import front_door_for_vm

	front_door = front_door_for_vm(name)
	if front_door is None:
		frappe.throw(f"No bench or site front door backs VM {name}.")
	front_door.regenerate_login_url()
	# Re-read the VM: its front door just committed the fresh login_url, and _vm_payload
	# reads the handoff back through that front door — the VM-shaped Asset mirror row.
	return _vm_payload(frappe.get_doc("Virtual Machine", name))


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


@frappe.whitelist()
def resize_capacity(vm: str) -> dict:
	"""The largest shape `vm` can resize to on its current host — Central's pre-resize
	check, the in-place twin of `capacity()`.

	A resize reshapes the VM on the host it already occupies, so — unlike `capacity()`,
	which sizes a NEW machine against the best host's free headroom — the ceiling is
	THIS host's free room with the VM's own current footprint added back (a resize frees
	it before re-reserving). The VM can therefore always keep its size or shrink; Central
	offers only resize targets that will fit, so an oversized resize never fails on the
	host.

	Returns `{available, unmeasured, largest_vm}` in the same shape as `capacity()`:
	`largest_vm` is `{vcpus, memory_megabytes, disk_gigabytes}`, null (and `available`
	False) only when the VM or its host is unknown. `unmeasured` flags a host with an
	unreported axis (sentinel numbers — treat the ceiling as "size unknown"). Advisory:
	the resize path on the host is the authoritative gate. Runs with the Central token,
	like `capacity()` / `create_vm`."""
	from atlas.atlas.placement import resize_headroom

	shape = resize_headroom(vm)
	if shape is None:
		return {"available": False, "unmeasured": False, "largest_vm": None}

	unmeasured = shape.pop("unmeasured")
	return {"available": True, "unmeasured": unmeasured, "largest_vm": shape}
