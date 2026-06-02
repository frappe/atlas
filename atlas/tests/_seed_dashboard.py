import frappe

# Throwaway visual-seed for the dashboard SPA. Inserts a base image, a handful
# of Virtual Machines across the status spectrum (owned by the dashboard
# tester), their Snapshots, and a few Tasks — all via direct DB writes so the
# Virtual Machine controller's provisioning hooks never fire. This exists only
# to populate the UI for screenshot/QA work; it is NOT part of the test suite.
# Idempotent: re-running clears the seeded rows first.

OWNER = "dashboard.tester@atlas.local"
IMAGE = "ubuntu-24.04-server"


def _pick_server():
	"""A real Server name so the VM.server link resolves and the detail page
	reads cleanly. Prefer an Active server; fall back to any server, then to a
	placeholder. These are display fixtures — do NOT click lifecycle buttons on
	a seeded VM expecting a real host op; the script does not provision a guest
	on the server, so a Stop/Start would target a VM that isn't there.
	"""
	active = frappe.get_all("Server", filters={"status": "Active"}, pluck="name", limit=1)
	if active:
		return active[0]
	any_server = frappe.get_all("Server", pluck="name", limit=1)
	return any_server[0] if any_server else "seed-server-1"

VMS = [
	# title, status, ipv6, vcpus, mem, disk, preset
	("web-01", "Running", "2606:4700:4700::a1f3", 2, 2048, 10, "Medium (2 vCPU / 2048 MB / 10 GB)"),
	("db-staging", "Stopped", "2606:4700:4700::77c2", 4, 8192, 40, "Large (4 vCPU / 8192 MB / 40 GB)"),
	("build-box", "Pending", "", 1, 512, 4, "Small (1 vCPU / 512 MB / 4 GB)"),
	("cache-02", "Paused", "2606:4700:4700::9b10", 1, 512, 4, "Small (1 vCPU / 512 MB / 4 GB)"),
	("old-worker", "Terminated", "", 2, 2048, 10, "Medium (2 vCPU / 2048 MB / 10 GB)"),
]


def _wipe():
	for dt in ("Task", "Virtual Machine Snapshot", "Virtual Machine"):
		for name in frappe.get_all(dt, filters={"owner": OWNER}, pluck="name"):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)


def _ensure_image():
	if not frappe.db.exists("Virtual Machine Image", IMAGE):
		doc = frappe.new_doc("Virtual Machine Image")
		doc.image_name = IMAGE
		doc.title = "Ubuntu 24.04 Server"
		doc.is_active = 1
		doc.default_disk_gigabytes = 4
		doc.kernel_url = "https://example.invalid/vmlinux"
		doc.kernel_filename = "vmlinux"
		doc.kernel_sha256 = "0" * 64
		doc.rootfs_url = "https://example.invalid/rootfs.img"
		doc.rootfs_filename = "rootfs.img"
		doc.rootfs_sha256 = "0" * 64
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	# A second, also-active image so the New Machine picker shows a choice.
	if not frappe.db.exists("Virtual Machine Image", "ubuntu-24.04-minimal"):
		doc = frappe.new_doc("Virtual Machine Image")
		doc.image_name = "ubuntu-24.04-minimal"
		doc.title = "Ubuntu 24.04 Minimal"
		doc.is_active = 1
		doc.default_disk_gigabytes = 4
		doc.kernel_url = "https://example.invalid/vmlinux"
		doc.kernel_filename = "vmlinux"
		doc.kernel_sha256 = "0" * 64
		doc.rootfs_url = "https://example.invalid/rootfs.img"
		doc.rootfs_filename = "rootfs.img"
		doc.rootfs_sha256 = "0" * 64
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)


def _insert_vm(server, title, status, ipv6, vcpus, mem, disk, preset):
	name = frappe.generate_hash("vm", 10)
	# ssh_command is a virtual field (computed from ipv6_address on read) — no
	# column, so it is not part of the INSERT.
	frappe.db.sql(
		"""
		INSERT INTO `tabVirtual Machine`
			(name, owner, creation, modified, modified_by, docstatus, idx,
			 title, status, image, server, ipv6_address,
			 size_preset, vcpus, memory_megabytes, disk_gigabytes, ssh_public_key)
		VALUES
			(%(name)s, %(owner)s, NOW(), NOW(), %(owner)s, 0, 0,
			 %(title)s, %(status)s, %(image)s, %(server)s, %(ipv6)s,
			 %(preset)s, %(vcpus)s, %(mem)s, %(disk)s, %(key)s)
		""",
		{
			"name": name,
			"owner": OWNER,
			"title": title,
			"status": status,
			"image": IMAGE,
			"server": server,
			"ipv6": ipv6,
			"preset": preset,
			"vcpus": vcpus,
			"mem": mem,
			"disk": disk,
			"key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISEEDdashboardtester",
		},
	)
	return name, disk


def _insert_task(server, vm, subject, script, status, mins_ago):
	name = frappe.generate_hash("task", 10)
	frappe.db.sql(
		"""
		INSERT INTO `tabTask`
			(name, owner, creation, modified, modified_by, docstatus, idx,
			 subject, script, status, virtual_machine, server)
		VALUES
			(%(name)s, %(owner)s, DATE_SUB(NOW(), INTERVAL %(mins)s MINUTE),
			 DATE_SUB(NOW(), INTERVAL %(mins)s MINUTE), %(owner)s, 0, 0,
			 %(subject)s, %(script)s, %(status)s, %(vm)s, %(server)s)
		""",
		{
			"name": name,
			"owner": OWNER,
			"subject": subject,
			"script": script,
			"status": status,
			"vm": vm,
			"server": server,
			"mins": mins_ago,
		},
	)


def _insert_snapshot(server, title, vm, status, disk, size_bytes):
	name = frappe.generate_hash("snap", 10)
	frappe.db.sql(
		"""
		INSERT INTO `tabVirtual Machine Snapshot`
			(name, owner, creation, modified, modified_by, docstatus, idx,
			 title, virtual_machine, server, status, source_image,
			 disk_gigabytes, size_bytes)
		VALUES
			(%(name)s, %(owner)s, NOW(), NOW(), %(owner)s, 0, 0,
			 %(title)s, %(vm)s, %(server)s, %(status)s, %(image)s,
			 %(disk)s, %(size)s)
		""",
		{
			"name": name,
			"owner": OWNER,
			"title": title,
			"vm": vm,
			"server": server,
			"status": status,
			"image": IMAGE,
			"disk": disk,
			"size": size_bytes,
		},
	)


def run():
	_wipe()
	_ensure_image()
	server = _pick_server()

	by_title = {}
	for row in VMS:
		name, disk = _insert_vm(server, *row)
		by_title[row[0]] = (name, disk)

	# Tasks for web-01 — the Activity timeline on the detail page.
	web01 = by_title["web-01"][0]
	_insert_task(server, web01, "Provision web-01", "provision-vm.py", "Success", 130)
	_insert_task(server, web01, "Start web-01", "start-vm.py", "Success", 125)
	_insert_task(server, web01, "Stop web-01", "stop-vm.py", "Running", 1)

	dbstaging = by_title["db-staging"][0]
	_insert_task(server, dbstaging, "Provision db-staging", "provision-vm.py", "Success", 1500)
	_insert_task(server, dbstaging, "Stop db-staging", "stop-vm.py", "Success", 1440)

	# Snapshots — secondary list.
	_insert_snapshot(server, "web-01-may30", web01, "Available", 10, 9_800_000_000)
	_insert_snapshot(server, "db-pre-upgrade", dbstaging, "Pending", 40, 9_800_000_000)

	frappe.db.commit()
	print(f"Seeded {len(VMS)} VMs, 2 snapshots, 5 tasks for {OWNER} (server={server})")
