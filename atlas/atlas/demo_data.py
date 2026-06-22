"""The demo dataset and the per-DocType builders `demo.run()` drives.

Split from `demo.py` so the orchestration stays readable and the (long) data
tables + builders live on their own. Everything here runs against the Fake
provider, so the builders call the real controllers — `vm.provision()`,
`vm.stop()`, `snapshot.clone_to_new_vm`, `Reserved IP.attach` — and let the fake
seam make them succeed without SSH.
"""

from __future__ import annotations

import frappe

from atlas.atlas.sizes import SIZE_PRESETS

# --- Static data ---------------------------------------------------------

# (provider_name, fail_scripts). fake-prod is the active one; fake-lab is a
# second Fake provider so multi-provider UI has something to show.
PROVIDERS = (
	("fake-prod", ""),
	("fake-lab", ""),
)

# The demo also creates two NON-Fake provider rows for variety: a Self-Managed
# host and an archived DigitalOcean row. They are demo artifacts (named here), so
# wipe() removes them by name — it never touches a real operator's rows.
SELF_MANAGED_PROVIDER = "demo-self-managed"
ARCHIVED_PROVIDER = "do-legacy"
DEMO_NON_FAKE_PROVIDERS = (SELF_MANAGED_PROVIDER, ARCHIVED_PROVIDER)

DEFAULT_USER_IMAGE = "ubuntu-24.04-server"

# key -> Virtual Machine Image field dict. The last one is archived (is_active=0)
# so the operator sees a decommissioned image.
IMAGES = {
	"ubuntu-server": {
		"image_name": "ubuntu-24.04-server",
		"title": "Ubuntu 24.04 Server",
		"is_active": 1,
		"default_disk_gigabytes": 4,
	},
	"ubuntu-minimal": {
		"image_name": "ubuntu-24.04-minimal",
		"title": "Ubuntu 24.04 Minimal",
		"is_active": 1,
		"default_disk_gigabytes": 4,
	},
	"alpine": {
		"image_name": "alpine-3.20",
		"title": "Alpine 3.20",
		"is_active": 1,
		"default_disk_gigabytes": 2,
	},
	"ubuntu-eol": {
		"image_name": "ubuntu-22.04-eol",
		"title": "Ubuntu 22.04 (retired)",
		"is_active": 0,
		"default_disk_gigabytes": 4,
	},
}

# key -> (title, final_status). Active servers are provisioned through the real
# worker; the rest are forced to their state afterwards.
SERVERS = {
	"blr1-01": ("blr1-fake-01", "Active"),
	"blr1-02": ("blr1-fake-02", "Active"),
	"nyc3-01": ("nyc3-fake-01", "Active"),
	"ams1-01": ("ams1-fake-01", "Bootstrapping"),
	"fra1-01": ("fra1-fake-01", "Broken"),
	"drain-01": ("drain-fake-01", "Draining"),
}

# Self-Managed server: operator-typed networking, no provider API.
SELF_MANAGED_SERVER = {
	"title": "old-metal-01",
	"ipv4_address": "203.0.113.200",
	"ipv6_address": "2001:db8:5e1f::1",
	"ipv6_prefix": "2001:db8:5e1f::/64",
	"ipv6_virtual_machine_range": "2001:db8:5e1f::/80",
}

# Each VM: a spec dict consumed by `_build_vm`. `server` and `image` are keys
# into the maps above. `preset` draws vcpus/cpu/mem/disk from sizes.SIZE_PRESETS;
# `end` is the lifecycle state to leave it in. `extra` overrides any VM field.
VIRTUAL_MACHINES = (
	{
		"key": "web-01",
		"title": "web-01",
		"server": "blr1-01",
		"image": "ubuntu-server",
		"preset": "Shared 8x",
		"end": "Running",
	},
	{
		"key": "web-02",
		"title": "web-02",
		"server": "blr1-01",
		"image": "ubuntu-server",
		"preset": "Shared 8x",
		"end": "Running",
		"extra": {"cpu_mode": "Relaxed"},
	},  # burst VM
	{
		"key": "api-01",
		"title": "api-01",
		"server": "blr1-02",
		"image": "ubuntu-minimal",
		"preset": "Shared 4x",
		"end": "Stopped",
	},
	{
		"key": "db-01",
		"title": "db-01",
		"server": "blr1-02",
		"image": "ubuntu-server",
		"preset": "Dedicated 1x",
		"end": "Stopped",
		"extra": {
			"data_disk_gigabytes": 50,
			"data_disk_format_and_mount": 1,
			"data_disk_mount_point": "/var/lib/mysql",
		},
	},
	{
		"key": "cache-01",
		"title": "cache-01",
		"server": "blr1-02",
		"image": "ubuntu-minimal",
		"preset": "Shared 2x",
		"end": "Paused",
	},
	{
		"key": "worker-01",
		"title": "worker-01",
		"server": "nyc3-01",
		"image": "ubuntu-server",
		"preset": "Shared 1x",
		"end": "Running",
	},
	{
		"key": "scratch-01",
		"title": "scratch-01",
		"server": "nyc3-01",
		"image": "alpine",
		"preset": "Shared 1x",
		"end": "Terminated",
	},
	{
		"key": "locked-01",
		"title": "locked-01",
		"server": "blr1-01",
		"image": "ubuntu-server",
		"preset": "Shared 4x",
		"end": "Stopped",
		"extra": {"stop_protection": 1, "termination_protection": 1},
	},
	{
		"key": "snap-me",
		"title": "snapshot-source",
		"server": "blr1-01",
		"image": "ubuntu-server",
		"preset": "Shared 4x",
		"end": "Stopped",
	},
	{
		"key": "fastboot",
		"title": "fastboot-01",
		"server": "nyc3-01",
		"image": "ubuntu-server",
		"preset": "Shared 2x",
		"end": "Stopped",
		"extra": {"memory_snapshot_on_stop": 1},
	},  # has_memory_snapshot after stop
	{
		"key": "failed-01",
		"title": "failed-01",
		"server": "blr1-02",
		"image": "ubuntu-server",
		"preset": "Shared 1x",
		"end": "Failed",
	},
	{
		"key": "proxy-01",
		"title": "proxy-blr1",
		"server": "blr1-01",
		"image": "ubuntu-minimal",
		"preset": "Shared 4x",
		"end": "Running",
		"extra": {"is_proxy": 1, "region": "blr1"},
	},  # gets a Reserved IP attached
)


# --- Builders ------------------------------------------------------------


def ensure_images() -> dict[str, str]:
	result = {}
	for key, fields in IMAGES.items():
		name = fields["image_name"]
		if not frappe.db.exists("Virtual Machine Image", name):
			# is_active images fan out a (faked, cheap) sync to Active servers via
			# after_insert. That is harmless and realistic, so let it run.
			frappe.get_doc({"doctype": "Virtual Machine Image", **fields, **_dummy_image_bytes(name)}).insert(
				ignore_permissions=True
			)
		result[key] = name
	# nosemgrep: frappe-manual-commit -- persist the seeded demo images before later phases
	frappe.db.commit()
	return result


def _dummy_image_bytes(name: str) -> dict:
	"""Plausible-but-fake kernel/rootfs locators. Never fetched (no real sync)."""
	return {
		"kernel_url": f"https://images.invalid/{name}/vmlinux",
		"kernel_filename": "vmlinux",
		"kernel_sha256": "0" * 64,
		"rootfs_url": f"https://images.invalid/{name}/rootfs.squashfs",
		"rootfs_filename": f"{name}.ext4",
		"rootfs_sha256": "0" * 64,
	}


def delete_demo_images() -> None:
	for fields in IMAGES.values():
		name = fields["image_name"]
		if frappe.db.exists("Virtual Machine Image", name):
			frappe.delete_doc("Virtual Machine Image", name, force=True, ignore_permissions=True)


def ensure_servers(active_provider: str) -> dict[str, str]:
	from atlas.atlas.providers.worker import finish_provisioning

	provider = frappe.get_doc("Provider", active_provider)
	result: dict[str, str] = {}
	for key, (title, final_status) in SERVERS.items():
		existing = frappe.db.get_value("Server", {"title": title}, "name")
		if existing:
			result[key] = existing
			continue
		name = provider.provision_server(title)
		finish_provisioning(name)  # Pending -> Active (faked bootstrap), inline
		if final_status != "Active":
			frappe.db.set_value("Server", name, "status", final_status)
		result[key] = name
	result["metal-01"] = _ensure_self_managed_server()
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the seeded demo servers before VMs are built on top of them
	frappe.db.commit()
	return result


def _ensure_self_managed_server() -> str:
	existing = frappe.db.get_value("Server", {"title": SELF_MANAGED_SERVER["title"]}, "name")
	if existing:
		return existing
	_ensure_self_managed_provider()
	provider = frappe.get_doc("Provider", SELF_MANAGED_PROVIDER)
	name = provider.provision_server(SELF_MANAGED_SERVER["title"], **_self_managed_dialog())
	frappe.db.set_value("Server", name, "status", "Active")
	return name


def _ensure_self_managed_provider() -> None:
	if not frappe.db.exists("Provider", SELF_MANAGED_PROVIDER):
		frappe.get_doc(
			{
				"doctype": "Provider",
				"provider_name": SELF_MANAGED_PROVIDER,
				"provider_type": "Self-Managed",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)


def _self_managed_dialog() -> dict:
	return {
		"ipv4_address": SELF_MANAGED_SERVER["ipv4_address"],
		"ipv6_address": SELF_MANAGED_SERVER["ipv6_address"],
		"ipv6_prefix": SELF_MANAGED_SERVER["ipv6_prefix"],
		"ipv6_virtual_machine_range": SELF_MANAGED_SERVER["ipv6_virtual_machine_range"],
	}


def ensure_virtual_machines(servers: dict[str, str], images: dict[str, str]) -> dict[str, str]:
	result: dict[str, str] = {}
	for spec in VIRTUAL_MACHINES:
		title = spec["title"]
		existing = frappe.db.get_value("Virtual Machine", {"title": title}, "name")
		if existing:
			result[spec["key"]] = existing
			continue
		result[spec["key"]] = _build_vm(spec, servers, images)
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the seeded demo VMs before snapshots and reserved IPs reference them
	frappe.db.commit()
	return result


# Protection flags can't be set before the lifecycle walk: stop()/terminate()
# refuse while they're on. So insert with them off, walk to the target state,
# then turn them on with db_set (a plain operator toggle, no lifecycle gate).
_PROTECTION_FLAGS = ("stop_protection", "termination_protection")


def _build_vm(spec: dict, servers: dict[str, str], images: dict[str, str]) -> str:
	"""Insert one VM and walk it to its target lifecycle state, all faked."""
	from unittest.mock import patch

	preset = SIZE_PRESETS[spec["preset"]]
	extra = dict(spec.get("extra", {}))
	protections = {flag: extra.pop(flag) for flag in _PROTECTION_FLAGS if flag in extra}
	fields = {
		"doctype": "Virtual Machine",
		"title": spec["title"],
		"server": servers[spec["server"]],
		"image": images[spec["image"]],
		"size_preset": spec["preset"],
		"vcpus": preset["vcpus"],
		"cpu_max_cores": preset["cpu_max_cores"],
		"memory_megabytes": preset["memory_megabytes"],
		"disk_gigabytes": preset["disk_gigabytes"],
		"ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDEMOdemodemodemodemodemodemo",
		**extra,
	}
	# Patch enqueue so after_insert's auto_provision doesn't race the explicit
	# walk below; we drive the lifecycle deterministically.
	with patch("frappe.enqueue"):
		vm = frappe.get_doc(fields).insert(ignore_permissions=True)
	_walk_to_state(vm, spec["end"])
	for flag, value in protections.items():
		vm.db_set(flag, value)
	return vm.name


def _walk_to_state(vm, end: str) -> None:
	"""Drive a freshly-inserted Pending VM to `end` via the real controllers."""
	if end == "Pending":
		return
	if end == "Failed":
		_provision_failing(vm)
		return
	vm.provision()  # -> Running
	if end == "Running":
		return
	if end in ("Stopped", "Paused", "Terminated"):
		if end == "Paused":
			vm.pause()
			return
		vm.stop()  # -> Stopped (captures a memory snapshot if opted in)
		if end == "Terminated":
			vm.terminate()


def _provision_failing(vm) -> None:
	"""Leave the VM Failed by injecting a provision-vm.py failure once."""
	frappe.flags.fake_fail = {"script": "provision-vm.py", "reason": "demo: injected provision failure"}
	try:
		vm.provision()
	except frappe.ValidationError:
		pass
	finally:
		frappe.flags.fake_fail = None
	vm.reload()


def ensure_snapshots(machines: dict[str, str]) -> None:
	"""A Cold (Available), a still-Pending Cold, a Warm golden, and a Failed one."""
	source = machines.get("snap-me")
	if source and not _has_snapshot(source):
		vm = frappe.get_doc("Virtual Machine", source)
		vm.snapshot(title="snapshot-source — nightly")  # Stopped VM -> Available
	# A warm golden captured from a Running VM (the fan-out source).
	warm_source = machines.get("worker-01")
	if warm_source and not _has_warm_snapshot(warm_source):
		warm = frappe.get_doc("Virtual Machine", warm_source).capture_warm_snapshot(title="golden-warm")
		frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", warm, update_modified=False)
	# Two hand-set rows so Pending and Failed states are visible without a Task.
	if source:
		_insert_snapshot_row(source, "pending-export", "Pending")
		_insert_snapshot_row(source, "failed-export", "Failed")
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the seeded demo snapshots and the default_bench_snapshot setting
	frappe.db.commit()


def _has_snapshot(vm_name: str) -> bool:
	return bool(frappe.db.exists("Virtual Machine Snapshot", {"virtual_machine": vm_name, "kind": "Cold"}))


def _has_warm_snapshot(vm_name: str) -> bool:
	return bool(frappe.db.exists("Virtual Machine Snapshot", {"virtual_machine": vm_name, "kind": "Warm"}))


def _insert_snapshot_row(vm_name: str, title: str, status: str) -> None:
	if frappe.db.exists("Virtual Machine Snapshot", {"title": title}):
		return
	server = frappe.db.get_value("Virtual Machine", vm_name, "server")
	frappe.get_doc(
		{
			"doctype": "Virtual Machine Snapshot",
			"title": title,
			"virtual_machine": vm_name,
			"server": server,
			"status": status,
			"disk_gigabytes": 4,
		}
	).insert(ignore_permissions=True)


def ensure_reserved_ips(servers: dict[str, str], machines: dict[str, str]) -> None:
	"""One Attached (to the proxy VM), one free Allocated, one on another server."""
	from atlas.atlas.doctype.reserved_ip import reserved_ip as reserved_ip_module

	proxy = machines.get("proxy-01")
	if proxy:
		server = frappe.db.get_value("Virtual Machine", proxy, "server")
		if not _server_has_attached_ip(server):
			name = reserved_ip_module.allocate(server)
			frappe.get_doc("Reserved IP", name).attach(proxy)
	# A free address on a different Active server.
	other = servers.get("nyc3-01")
	if other and not _server_has_free_ip(other):
		reserved_ip_module.allocate(other)
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the seeded demo reserved IPs and their attachments
	frappe.db.commit()


def _server_has_attached_ip(server: str) -> bool:
	return bool(frappe.db.exists("Reserved IP", {"server": server, "status": "Attached"}))


def _server_has_free_ip(server: str) -> bool:
	return bool(frappe.db.exists("Reserved IP", {"server": server, "status": "Allocated"}))


def backdate_tasks(servers: dict[str, str], machines: dict[str, str]) -> None:
	"""Sprinkle a few historical Tasks so the Task list / Operations panel look
	lived-in. Real lifecycle Tasks already exist from the walk above; these add a
	spread of older Success/Failure/Running rows with varied scripts."""
	rows = [
		("web-01", "sync-image.py", "Success", 240),
		("web-01", "snapshot-vm.py", "Success", 180),
		("api-01", "resize-vm.py", "Success", 90),
		("db-01", "snapshot-vm.py", "Failure", 60),
		("failed-01", "provision-vm.py", "Failure", 30),
		("web-02", "stop-vm.py", "Running", 1),
	]
	for vm_key, script, status, minutes_ago in rows:
		vm_name = machines.get(vm_key)
		if not vm_name:
			continue
		server = frappe.db.get_value("Virtual Machine", vm_name, "server")
		_insert_backdated_task(server, vm_name, script, status, minutes_ago)
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the backdated demo Tasks so the Task list shows realistic history
	frappe.db.commit()


def _insert_backdated_task(server: str, vm: str, script: str, status: str, minutes_ago: int) -> None:
	subject = f"{script.removesuffix('.py').replace('-', ' ').title()} {frappe.db.get_value('Virtual Machine', vm, 'title')}"
	name = frappe.generate_hash("demo-task", 10)
	frappe.db.sql(
		"""
		INSERT INTO `tabTask`
			(name, owner, creation, modified, modified_by, docstatus, idx,
			 subject, script, status, virtual_machine, server, triggered_by)
		VALUES
			(%(name)s, 'Administrator', DATE_SUB(NOW(), INTERVAL %(min)s MINUTE),
			 DATE_SUB(NOW(), INTERVAL %(min)s MINUTE), 'Administrator', 0, 0,
			 %(subject)s, %(script)s, %(status)s, %(vm)s, %(server)s, 'Administrator')
		""",
		{
			"name": name,
			"subject": subject,
			"script": script,
			"status": status,
			"vm": vm,
			"server": server,
			"min": minutes_ago,
		},
	)
