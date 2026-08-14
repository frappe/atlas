"""The service API — the surface an external image/orchestration service (e.g. chef)
calls to provision, drive, and sync the VMs an Atlas owns (spec/30, the
provisioner/orchestrator split).

Atlas is a *pure provisioner*: it owns "a VM exists" and nothing about services. The
service is a SEPARATE deployment that manages services itself, over its own SSH to the
hosts and guests Atlas hands over. It never reaches into Atlas's DB — it reads these
methods to mirror the VM/Server it needs and learns the connection details (the host's
public IPv4, the guest's public IPv6) so it can SSH in, and calls the write methods
below to lay down / drive / snapshot the bare machines it operates.

One service federates many Atlasses, so it holds a per-Atlas credential and base URL;
each Atlas authenticates the caller with THIS Atlas's admin token (System Manager),
exactly like the Central inbound API (`central_link.py`). Every method here is behind
`frappe.only_for("System Manager")`.
"""

from __future__ import annotations

import frappe
from frappe import _


def _server_ipv4(server: str | None) -> str | None:
	"""The host's public IPv4 — how the service SSHes the HOST (host-plane work:
	the mesh, the gateway). None for a VM not yet placed on a server."""
	if not server:
		return None
	return frappe.db.get_value("Server", server, "ipv4_address")


def _vm_payload(vm) -> dict:
	"""The registration mirror the service keeps for one VM: identity + tenant + the two
	SSH targets (host IPv4, guest IPv6) + base addressing. Deliberately service-free —
	Atlas has no service roles to report.

	`build_mode` (the bake mode of the image, site/admin) and `warm` (was this VM
	warm-restored) are PROVISIONER facts — an image attribute and a boot fact, not
	service state (build_mode stays in Atlas by decision 9). A service that installs a
	site into the guest needs them to pick the deploy mode + gate on the warm identity
	freshen, so they ride the mirror rather than a Central→service call."""
	return {
		"name": vm.name,
		"status": vm.status,
		"server": vm.server,
		"server_ipv4": _server_ipv4(vm.server),
		"tenant": vm.tenant,
		"guest_ipv6": vm.ipv6_address,
		"private_address": vm.private_address,
		"build_mode": vm.build_mode or "",
		"warm": bool(vm.warm_snapshot),
		# The Site/Pilot subdomain label(s) the provisioner recorded as routing intent (the
		# routing dedup): the service creates its own Subdomain rows from these instead of
		# Atlas creating them. A routing fact, but the minimal bridge that lets Atlas stop
		# owning the guest-plane routing while Site/Pilot stay here.
		"routing_subdomains": frappe.parse_json(vm.routing_subdomains or "[]"),
		"modified": str(vm.modified),
	}


@frappe.whitelist()
def get_virtual_machine(name: str) -> dict:
	"""One VM's registration payload (identity, tenant, host IPv4 + guest IPv6). The
	service calls this on a webhook or during a reconcile to (re)populate its mirror."""
	frappe.only_for("System Manager")
	return _vm_payload(frappe.get_doc("Virtual Machine", name))


@frappe.whitelist()
def list_virtual_machines(modified_after: str | None = None) -> list[dict]:
	"""Every VM (optionally only those modified since `modified_after`, an ISO timestamp)
	— the service's reconcile/backfill sweep, so a missed webhook self-heals. Ordered
	oldest-change first so a paging caller can advance its watermark."""
	frappe.only_for("System Manager")
	filters = {"modified": (">", modified_after)} if modified_after else {}
	names = frappe.get_all("Virtual Machine", filters=filters, pluck="name", order_by="modified asc")
	return [_vm_payload(frappe.get_doc("Virtual Machine", n)) for n in names]


@frappe.whitelist()
def get_server(name: str) -> dict:
	"""One host's payload — its public IPv4 (the service's host-plane SSH target), status,
	and the host signature (the architecture + firecracker/jailer/kernel versions the
	service reads to know what a snapshot captured here will restore against). Mirrored so
	the service can address host-plane work without re-reading a VM."""
	frappe.only_for("System Manager")
	server = frappe.get_doc("Server", name)
	return {
		"name": server.name,
		"status": server.status,
		"ipv4": server.ipv4_address,
		"architecture": server.architecture,
		"kernel_version": server.kernel_version,
		"firecracker_version": server.firecracker_version,
		"jailer_version": server.jailer_version,
		"modified": str(server.modified),
	}


@frappe.whitelist()
def create_bare_vm(
	title: str,
	base_image: str,
	vcpus: int,
	memory_megabytes: int,
	disk_gigabytes: int,
	cpu_max_cores: float | None = None,
	server: str | None = None,
	ssh_public_key: str | None = None,
) -> dict:
	"""Lay down a BARE Virtual Machine (no Pilot, no tenant) from `base_image` at the
	given size, and return its identity + the two SSH targets. This is the provisioner
	half of the boundary: Atlas boots a bare box; the service SSHes in and sets up
	whatever it runs. The Pilot/bench path (`api.provision.create_vm`) is the contrast —
	that owns a tenant environment; this owns only "a VM exists".

	`server` (optional) pins placement to that host; omitted, `before_insert` /
	`apply_user_defaults` run the placement gate and pick one. `cpu_max_cores` and
	`ssh_public_key` are passed only when given, so the DocType defaults apply otherwise.
	Committed before returning so the row is durable the moment the service mirrors it."""
	frappe.only_for("System Manager")
	doc = {
		"doctype": "Virtual Machine",
		"title": title,
		"image": base_image,
		"vcpus": int(vcpus),
		"memory_megabytes": int(memory_megabytes),
		"disk_gigabytes": int(disk_gigabytes),
	}
	if cpu_max_cores:
		doc["cpu_max_cores"] = float(cpu_max_cores)
	if server:
		doc["server"] = server
	if ssh_public_key:
		doc["ssh_public_key"] = ssh_public_key
	vm = frappe.get_doc(doc)
	vm.insert(ignore_permissions=True)
	frappe.db.commit()
	return {
		"name": vm.name,
		"status": vm.status,
		"ipv6_address": vm.ipv6_address,
		"server": vm.server,
		"server_ipv4": _server_ipv4(vm.server),
	}


@frappe.whitelist()
def stop_vm(vm: str) -> str | None:
	"""Stop a VM. Returns the stop Task's name (poll target for the service)."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine", vm).stop()


@frappe.whitelist()
def start_vm(vm: str) -> str | None:
	"""Start (or wake) a VM. Returns the start Task's name."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine", vm).start()


@frappe.whitelist()
def terminate_vm(vm: str) -> str | None:
	"""Terminate a VM. Returns the terminate Task's name."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine", vm).terminate()


@frappe.whitelist()
def snapshot_vm(vm: str, title: str | None = None, live: bool = False) -> str | None:
	"""Snapshot a VM's disk(s) into a new Virtual Machine Snapshot. Returns its name."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine", vm).snapshot(title=title, live=live)


@frappe.whitelist()
def capture_warm_snapshot(vm: str, title: str | None = None) -> str | None:
	"""Capture a live VM's memory+disk into a new warm Virtual Machine Snapshot.
	Returns its name."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine", vm).capture_warm_snapshot(title=title)


@frappe.whitelist()
def promote_image(snapshot: str, image_name: str, title: str | None = None) -> str | None:
	"""Promote a cold snapshot into a first-class base image. Returns the new (initially
	inactive) image's name — poll `get_image(...).is_active` for readiness."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine Snapshot", snapshot).promote_to_image(image_name, title=title)


@frappe.whitelist()
def upload_image_to_s3(snapshot: str) -> None:
	"""Push a snapshot's artifacts to S3 for off-host durability. Background job —
	poll `get_snapshot(...).s3_status`."""
	frappe.only_for("System Manager")
	return frappe.get_doc("Virtual Machine Snapshot", snapshot).upload_to_s3()


@frappe.whitelist()
def get_snapshot(name: str) -> dict:
	"""One snapshot's payload — status, the on-host rootfs path + size, the host
	signature it was captured against, and the S3 backup status. The service's poll
	target after `snapshot_vm` / `capture_warm_snapshot` / `upload_image_to_s3`."""
	frappe.only_for("System Manager")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", name)
	return {
		"name": snapshot.name,
		"status": snapshot.status,
		"rootfs_path": snapshot.rootfs_path,
		"size_bytes": snapshot.size_bytes,
		"host_signature": snapshot.host_signature,
		"s3_status": snapshot.s3_status,
	}


@frappe.whitelist()
def get_image(name: str) -> dict:
	"""One image's payload — whether it is active (provisionable) and its bench bake
	mode. The service's poll target after `promote_image` (inactive until the host dd
	finishes)."""
	frappe.only_for("System Manager")
	image = frappe.get_doc("Virtual Machine Image", name)
	return {
		"name": image.name,
		"is_active": bool(image.is_active),
		"build_mode": image.build_mode or "",
	}


@frappe.whitelist()
def publish_snapshot_as_fleet_image(
	snapshot: str, image_name: str, servers: list | str | None = None, title: str | None = None
) -> dict:
	"""Distribute a cold snapshot to the fleet as a base image: squash its rootfs and
	upload it public-read to S3, mint a NON-LOCAL Virtual Machine Image (a plain S3
	rootfs url + the source image's inherited kernel), and fan out sync-image to
	`servers` (a JSON list, or None = every Active server). The service (chef) polls
	get_image(...).is_active for readiness.
	Returns {image, rootfs_sha256, kernel_sha256, tasks}."""
	frappe.only_for("System Manager")
	from atlas.atlas import fleet_image

	return fleet_image.publish_snapshot_as_fleet_image(snapshot, image_name, servers=servers, title=title)


@frappe.whitelist()
def distribute_image(image: str, servers: list | str | None = None) -> dict:
	"""Fan an already-promoted LOCAL base image out to the fleet host-to-host over HTTP —
	no object store, no S3. The no-bucket counterpart to `publish_snapshot_as_fleet_image`.

	Where that squashes a snapshot to S3 and mints a from-URL image, this ships a LOCAL
	image's base LV straight from its home host to every other Active host over the mesh
	(`atlas.atlas.fleet_distribute`), reusing the ordinary `sync-image` verb unchanged. The
	image stays local — no new row, no dangling URL — and placement then treats every host
	with a successful sync as holding its bytes. Only a promoted local image qualifies; a
	from-URL image is rejected (place it with sync-image instead).

	`servers` is a JSON list of Server names (or None = every other Active host). The whole
	fan-out runs on the `long` queue, so this only preflights + enqueues and returns the
	handle `{image, source, servers}` immediately. The service (chef) calls this right after
	a `promote_image` to propagate the golden across the fleet without a bucket."""
	frappe.only_for("System Manager")
	from atlas.atlas import fleet_distribute

	return fleet_distribute.distribute_local_image(image, servers=servers)


@frappe.whitelist()
def register_bench_snapshot(snapshot: str) -> str:
	"""Wire a snapshot in as `Atlas Settings.default_bench_snapshot` — the golden a
	self-serve Site clones from (`placement.default_bench_snapshot`). The signup
	counterpart to `register_user_image`, and the ONLY way the service (chef) can hand
	its freshly-baked golden to the signup path: a Site's backing VM is not laid down
	from a base image, it is CLONED from a `Virtual Machine Snapshot`, so promoting a
	base image is not enough. Mirrors what the in-Atlas `Image Build` auto-register does
	via `registers_as` (`image_build._register`), which chef's bake bypasses.

	The snapshot must be Available — the signup path fails loud on a non-Available
	pointer (`placement.default_bench_snapshot`), so reject it here rather than stranding
	every future signup behind a bad default. A warm accelerator, if the service also
	captured one on the same host, is discovered per-server by
	`placement.warm_bench_snapshot_for_server`; this cold pointer only has to name the
	host + kernel lineage. Returns the snapshot name."""
	frappe.only_for("System Manager")
	status = frappe.db.get_value("Virtual Machine Snapshot", snapshot, "status")
	if status is None:
		frappe.throw(_("Snapshot {0} does not exist").format(snapshot))
	if status != "Available":
		frappe.throw(_("Snapshot {0} is not Available (status is {1})").format(snapshot, status))
	frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", snapshot)
	return snapshot


@frappe.whitelist()
def register_user_image(image: str) -> str:
	"""Wire a base image in as `Atlas Settings.default_user_image` — the image a server
	(`api.provision.create_vm` → `placement.default_image`) boots when no per-version
	`bench-<v>-admin` image matches. The server counterpart to `register_bench_snapshot`:
	after `promote_image` (+ `distribute_image`) the service names the promoted golden
	here so `create_vm` provisions from it without an operator hand-wiring the Single.

	The image must be active (provisionable) — reject an inactive one rather than pointing
	servers at an image whose host `dd` has not finished (poll `get_image(...).is_active`
	first). Returns the image name."""
	frappe.only_for("System Manager")
	is_active = frappe.db.get_value("Virtual Machine Image", image, "is_active")
	if is_active is None:
		frappe.throw(_("Image {0} does not exist").format(image))
	if not is_active:
		frappe.throw(_("Image {0} is not active yet").format(image))
	frappe.db.set_single_value("Atlas Settings", "default_user_image", image)
	return image
