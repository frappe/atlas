"""Services side of Central reporting — the Site + pilot-console events and the
VM-payload front-door augmentation. Central mirrors VMs, so a bench Site or console
reports AS its backing VM (carrying the login handoff), and a bench VM's Central
payload folds in the front door's status + URLs.

Split off `central_report.py`, which keeps the PaaS-blind core reporting (VM /
Snapshot / Server events + the delivery machinery). The two front-door reads the
core still needs — suppressing a bench VM's raw status flip, and augmenting its
payload — are exposed here as the `vm.status_suppressed` / `vm.payload_augment`
callbacks (registered in callbacks_register), so `central_report` never imports
`front_door`. This module reuses the core delivery helpers (`_emit`, `_enabled`,
`_status_changed`, `_iso`) — services→core, which is allowed.
"""

from __future__ import annotations

import frappe

from atlas.atlas.core import central_report


def on_console_update(doc, method=None):
	# A pilot-console Site reports AS its backing VM (Central mirrors VMs, not consoles): a
	# console status change is emitted as a vm.status_changed carrying the VM-shaped payload
	# — this is the event that delivers the login handoff, since the console flips
	# Running only after the in-guest mint. A console with no VM yet (created but its
	# after_insert hasn't linked one) has nothing to report.
	if central_report._enabled() and central_report._status_changed(doc) and doc.virtual_machine:
		report_console_status(doc)


def report_console_status(console) -> None:
	"""Emit a console's status as a vm.status_changed, carrying the login handoff.

	The `on_update` doc_event above delivers this for a plain `.save()`. But the
	terminal Running flip in the console's `auto_provision` is a `db_set` (skips validation
	mid-job), and `db_set` runs only `on_change`, never `on_update` — so that flip,
	the very event that carries the freshly-minted login_url, would otherwise never
	push. auto_provision calls this explicitly after its commit to close that gap;
	the periodic reconcile is only the backstop, not the primary delivery."""
	if central_report._enabled() and console.virtual_machine:
		central_report._emit("vm.status_changed", _console_vm_payload(console), console)


def on_site_after_insert(doc, method=None):
	# A pilot-console Site reports AS its backing VM, and the VM's own after_insert emits
	# vm.created — there is no site.created for a console (Central mirrors it as a VM).
	if doc.get("kind") == "pilot-console":
		return
	if central_report._enabled():
		central_report._emit("site.created", _site_payload(doc), doc)


def on_site_update(doc, method=None):
	# A pilot-console Site reports AS its backing VM — its status change is a
	# vm.status_changed carrying the login handoff, never a site.status_changed. The
	# one Site on_update handler dispatches on kind.
	if doc.get("kind") == "pilot-console":
		on_console_update(doc)
		return
	if central_report._enabled() and central_report._status_changed(doc):
		central_report._emit("site.status_changed", _site_payload(doc), doc)


def report_site_status(site) -> None:
	"""Emit a Site's current status as a site.status_changed event.

	The `on_update` doc_event above delivers this for a plain `.save()`. But every
	real lifecycle transition in `Site.auto_provision` (Provisioning → Deploying →
	Running / Failed) goes through `_set_status`, which uses `db_set` — and `db_set`
	runs only `on_change`, never `on_update`, so those transitions would never push.
	auto_provision's `_set_status` calls this explicitly (before its commit) to close
	that gap; without it Central's mirror only ever sees the initial Pending
	(site.created + the insert's on_update) and the site stays stuck at Pending —
	there is no site reconcile pull to correct it. Same shape as report_console_status."""
	if central_report._enabled():
		central_report._emit("site.status_changed", _site_payload(site), site)


def is_status_suppressed(vm_name: str) -> bool:
	"""Whether core should SUPPRESS a VM's raw status flip because a front door owns
	it (spec/14). A front-door-backed VM (console or Site) reports its status THROUGH
	the aggregate, not off its own raw boot: the VM boots Running before deploy-site
	and the login mint, so the raw flip is premature. True iff a console or Site backs
	this VM; a plain VM (proxy, operator machine) is never suppressed."""
	from atlas.atlas.services.front_door import front_door_for_vm

	return front_door_for_vm(vm_name) is not None


def augment_vm_payload(payload: dict, vm_name: str) -> None:
	"""Fold the owning front door's bench fields onto a VM-shaped Central payload,
	in place: the aggregate's status (a bench VM boots Running before the mint, so
	its status is the front door's, not the raw VM's) plus the gateway/login URLs.
	A plain VM has no front door, so the core payload's None defaults stand."""
	from atlas.atlas.services.front_door import front_door_for_vm

	front_door = front_door_for_vm(vm_name)
	if front_door is not None:
		_merge_bench_fields(payload, front_door)


def _console_vm_payload(console) -> dict:
	# The VM-shaped payload for a console's own lifecycle event (and its regenerate
	# return). Central mirrors VMs, so a console reports AS its backing VM: plain VM
	# facts are read through the `virtual_machine` link, the bench fields off the
	# console. This is the event that carries the login handoff (the console flips Running
	# only after the mint), so its status is the CONSOLE's — the VM booted earlier.
	from atlas.atlas.core.placement import version_from_image
	from atlas.atlas.services.front_door import FrontDoor

	vm = frappe.get_doc("Virtual Machine", console.virtual_machine)
	return _merge_bench_fields(
		{
			"name": vm.name,
			"team": console.tenant or None,
			"title": vm.title,
			"status": console.status,
			"server": vm.server,
			"pilot_credential_id": vm.get("pilot_credential_id"),
			"size_preset": vm.get("size_preset"),
			"vcpus": vm.get("vcpus"),
			"memory_megabytes": vm.get("memory_megabytes"),
			"disk_gigabytes": vm.get("disk_gigabytes"),
			"ipv6_address": vm.get("ipv6_address"),
			"public_ipv4": vm.get("public_ipv4"),
			"frappe_version": version_from_image(vm.get("image")),
		},
		FrontDoor(console),
	)


def _merge_bench_fields(payload: dict, front_door) -> dict:
	"""Fold a front door's (console or Site) fields onto a VM-shaped payload. gateway_url
	is the derived FQDN URL (stable once the aggregate exists); login_url + its expiry
	are the one-click handoff, meaningful only once it is Running (before that the mint
	hasn't run — FrontDoor gates them). A None front_door (a plain, non-bench VM) leaves
	all three None.

	The status is taken from the front door too: a bench/site VM boots to Running before
	deploy-site + the login mint, so the raw VM status would report the Asset usable while
	it isn't. The aggregate flips Running only after the mint, so its status is the one
	Central mirrors (this is a no-op for _console_vm_payload, which already passed the
	console's status). A plain VM keeps the VM status the payload already carries."""
	if front_door is not None:
		payload["status"] = front_door.status
	payload["gateway_url"] = front_door.gateway_url if front_door is not None else None
	payload["login_url"] = front_door.login_url if front_door is not None else None
	payload["login_url_expires_at"] = (
		central_report._iso(front_door.login_url_expires_at) if front_door is not None else None
	)
	return payload


def _site_payload(doc) -> dict:
	# The owning Central team, so the control plane can attribute this site to a
	# tenant. The Tenant `name` *is* the Central `Team.name`, so the Site's `tenant`
	# link is the owning team directly; None for operator/e2e sites.
	# The login URL + its expiry + live URL are the tenant handoff — only
	# meaningful once the site is serving (Running), and the fields are stamped
	# before the readiness wait. login_url_expires_at is when the URL stops working
	# (mint time + the `bench browse --sid` session's 24h TTL), so Central compares
	# against it and regenerates a fresh one for a late click. Before Running there
	# is nothing to hand off.
	running = doc.status == "Running"
	return {
		"name": doc.name,
		"team": doc.tenant or None,
		"subdomain": doc.get("subdomain"),
		"status": doc.status,
		"fqdn": doc.name,
		"url": f"https://{doc.name}" if running else None,
		"login_url": doc.get("login_url") if running else None,
		"login_url_expires_at": central_report._iso(doc.get("login_url_expires_at")) if running else None,
	}
