"""Fan a LOCAL base image out to the fleet over HTTP — no object store, no S3.

A local (snapshot-promoted) `Virtual Machine Image` has an empty `rootfs_url`: its
bytes are the read-only base LV `atlas-image-<name>` on exactly the host it was
promoted on, so `sync-image` has nothing to fetch and the fleet can't provision from
it anywhere else. Two transports close that gap without a bucket:

- `atlas.atlas.export` ships the base LV point-to-point to ONE target over plain-TCP
  NBD (spec/24 §5.1) — the migration's proven copy.
- THIS module fans the SAME bytes out to MANY targets by reusing the ordinary
  `sync-image` verb UNCHANGED, over an on-host HTTP export on the host↔host mesh.

The shape mirrors `atlas.atlas.fleet_image.publish_snapshot_as_fleet_image` — squash
the rootfs on its home host, then feed the existing `sync-image` verb — but swaps the
S3 upload for a short-lived `python3 -m http.server` bound to the source's mesh
address (`networking.derive_host_mesh_address`, the documented image-fan-out bus).
Each target curls the squashfs, verifies its digest, unsquashes, builds a pristine
ext4 and imports the base LV `atlas-image-<name>` — byte-identical to a promote on
that host. The kernel is FREE: the promoted image reuses its source image's public
`kernel_url` + digest (spec/08 "the kernel is free"), so only the rootfs is new bytes
and only it rides the mesh; the kernel is fetched from its public URL as usual.

The served URL is EPHEMERAL — the HTTP unit is torn down when the fan-out finishes
(a `RuntimeMaxSec` cap is the backstop), and it is NEVER written to the image row. So
the image stays local: no new row (no name collision with the local one), no dangling
URL, and placement keeps treating every host with a successful `sync-image` Task for
it as holding its bytes (`placement._local_image_home_servers`). The whole thing is
idempotent — the home host re-serves and `sync-image` short-circuits where the base LV
already exists.

The squash + serve runs as ROOT over SSH on the home host (mirroring
`fleet_image._produce_and_upload_rootfs`): the Boat sudoers grants neither
`mount atlas-image-*` nor `mksquashfs`, so this is a root one-shot, not a Boat verb —
no new grant, no Boat change.
"""

from __future__ import annotations

import hashlib
import shlex
import time

import frappe
from frappe import _

# The per-image HTTP port lives ABOVE the migration/export NBD range (20000-25000, see
# export.export_port) so a distribute and an export of the same image never collide on a
# port. A stable per-image hash makes a re-run reuse the same port (idempotent).
_HTTP_PORT_BASE = 26000
_HTTP_PORT_SPAN = 2000

# Where the squashed rootfs is staged and served from on the home host.
_SERVE_ROOT = "/var/lib/atlas/fleet-serve"

# The whole fan-out (serve → sync → teardown) runs in one background job; it blocks on
# the sync Tasks so it can tear the HTTP server down only once every target has pulled
# the bytes. Bounded so a wedged target can't hold the worker forever — the HTTP unit's
# own RuntimeMaxSec is the ultimate backstop.
_POLL_INTERVAL_SECONDS = 10
_DISTRIBUTION_TIMEOUT_SECONDS = 1800


def image_http_port(image: str) -> int:
	"""The stable HTTP port this image is served on from its home host. A pure function
	of the name (a salted `hash()` would move the port every process), landing in
	[26000, 28000) — disjoint from the NBD range export/migration use, so a distribute
	and an export of the same image can run at once."""
	digest = hashlib.sha256(image.encode()).digest()
	return _HTTP_PORT_BASE + int.from_bytes(digest[:4], "big") % _HTTP_PORT_SPAN


@frappe.whitelist()
def distribute_local_image(image: str, servers: list[str] | str | None = None) -> dict:
	"""Fan a local base image out to the fleet over HTTP. Called from the Image form's
	`Sync Across Hosts` action; the operator picks the targets (or leaves it to the
	default: every other Active host).

	Only a LOCAL image needs this — a from-URL image is placed anywhere by
	`sync-image` (`Sync to All Servers`), so we reject a syncable one up front. The
	home host (where the base LV lives) is resolved from the promote Task trail and is
	always dropped from the target list — it already holds the bytes.

	Synchronous work would blow a web request's timeout (mksquashfs of a multi-GB rootfs
	takes minutes, then we wait on every target's sync), so this only PREFLIGHTS and
	enqueues `_run_distribution` on the long queue, returning a handle. Returns
	`{image, source, servers}`."""
	from atlas.atlas.doctype.virtual_machine_image_export.virtual_machine_image_export import (
		_image_home_server,
	)

	row = frappe.db.get_value(
		"Virtual Machine Image", image, ["is_active", "rootfs_url"], as_dict=True
	)
	if not row:
		frappe.throw(_("Virtual Machine Image {0} does not exist").format(image))
	if not row.is_active:
		frappe.throw(_("Image {0} is archived — activate it before distributing.").format(image))
	if (row.rootfs_url or "").strip():
		frappe.throw(
			_(
				"{0} is a from-URL image — place it with Sync to All Servers, not Sync Across "
				"Hosts (which ships a local, un-syncable image's bytes)."
			).format(image)
		)

	source = _image_home_server(image)
	if not source:
		frappe.throw(
			_(
				"Cannot resolve which host holds {0}'s base LV (no successful promote Task "
				"found) — nothing to distribute."
			).format(image)
		)

	if isinstance(servers, str):
		servers = frappe.parse_json(servers) or None
	if not servers:
		servers = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")
	targets = [server for server in servers if server != source]
	if not targets:
		frappe.throw(
			_("No other Active host to distribute {0} to (it already lives on {1}).").format(
				image, source
			)
		)

	frappe.enqueue(
		"atlas.atlas.fleet_distribute._run_distribution",
		queue="long",
		timeout=_DISTRIBUTION_TIMEOUT_SECONDS + 600,
		image=image,
		servers=targets,
	)
	return {"image": image, "source": source, "servers": targets}


def _run_distribution(image: str, servers: list[str]) -> None:
	"""The background driver: serve the rootfs on its home host, fan `sync-image` out to
	`servers`, wait for every sync to finish, then tear the HTTP server down.

	Teardown is in a `finally` so a mid-fan-out failure never leaves the HTTP unit and
	its staged squashfs behind on the home host. The image row is not touched — the
	served URL lives only in the sync Tasks' variables, so it can't dangle."""
	from atlas.atlas.doctype.virtual_machine_image.virtual_machine_image import (
		_enqueue_sync_image_task,
	)
	from atlas.atlas.doctype.virtual_machine_image_export.virtual_machine_image_export import (
		_image_home_server,
	)
	from atlas.atlas.networking import derive_host_mesh_address

	source = _image_home_server(image)
	if not source:
		frappe.throw(_("Cannot resolve which host holds {0}'s base LV.").format(image))
	targets = [server for server in servers if server != source]
	if not targets:
		return

	kernel = _source_image_kernel(image)
	mesh_address = derive_host_mesh_address(source)
	port = image_http_port(image)
	image_doc = frappe.get_doc("Virtual Machine Image", image)

	try:
		rootfs_sha256 = _serve_rootfs_over_http(source, image, mesh_address, port)
		variables = {
			"IMAGE_NAME": image_doc.image_name,
			"KERNEL_URL": kernel["kernel_url"],
			"KERNEL_FILENAME": kernel["kernel_filename"],
			"KERNEL_SHA256": kernel["kernel_sha256"],
			# Plain HTTP on the mesh bus (WG-encrypted, host-only); the `.sqfs` name
			# makes sync-image skip the guest-module re-bake (the promoted rootfs
			# already carries its modules).
			"ROOTFS_URL": f"http://[{mesh_address}]:{port}/rootfs.sqfs",
			# The LV-named presence sentinel the promote wrote on the home host; passing
			# it verbatim makes every target's on-disk name match the home host's, so a
			# distributed host looks identical to the promoted one to provision-vm.
			"ROOTFS_FILENAME": image_doc.rootfs_filename,
			"ROOTFS_SHA256": rootfs_sha256,
			"DEFAULT_DISK_GB": str(image_doc.default_disk_gigabytes),
			"GUEST_NETWORK_UNIT": "/tmp/atlas/atlas-network.service",
		}
		task_names = [_enqueue_sync_image_task(server, variables) for server in targets]
		outcomes = _poll_tasks_to_terminal(task_names, _DISTRIBUTION_TIMEOUT_SECONDS)
		unfinished = {name: (status or "Timed out") for name, status in outcomes.items() if status != "Success"}
		if unfinished:
			frappe.logger("atlas").error(
				f"fleet distribution of {image}: sync tasks not successful: {unfinished}"
			)
	finally:
		_teardown_http_server(source, image)


def _source_image_kernel(image: str) -> dict:
	"""The kernel a distributed image inherits: the source image's public `kernel_url`,
	`kernel_filename` and `kernel_sha256`. A promoted image reuses its snapshot's source
	image kernel byte-for-byte (spec/08 "the kernel is free"), so `sync-image` fetches
	the SAME public bzImage the promoted rootfs booted — the kernel never rides the mesh.

	The source image is read off the promote Task's `SOURCE_IMAGE` variable (the same
	Task trail `_image_home_server` resolves the home host from). Throws if the promote
	Task, its source image, or any kernel field is missing — we must not enqueue a sync
	we can't hand a kernel."""
	source_image = _promote_source_image(image)
	kernel = frappe.db.get_value(
		"Virtual Machine Image",
		source_image,
		["kernel_url", "kernel_filename", "kernel_sha256"],
		as_dict=True,
	)
	if not kernel or not kernel.kernel_url or not kernel.kernel_filename or not kernel.kernel_sha256:
		frappe.throw(
			_(
				"Source image {0} of {1} has no kernel_url/kernel_filename/kernel_sha256 to "
				"inherit; cannot distribute (the distributed image reuses its kernel)."
			).format(source_image, image)
		)
	return kernel


def _promote_source_image(image: str) -> str:
	"""The `SOURCE_IMAGE` the promote recorded for this local image, off the latest
	non-failed `promote-snapshot-image` Task — the same immutable trail
	`_image_home_server` reads the home host from (so the two never disagree about which
	promote produced the LV). Throws if there is no promote Task or it named no source."""
	rows = frappe.db.sql(
		"""
		SELECT variables FROM `tabTask`
		WHERE script IN ('promote-snapshot-image', 'promote-snapshot-image.py')
		  AND status != 'Failure'
		  AND variables LIKE %(pattern)s
		ORDER BY modified DESC
		LIMIT 1
		""",
		{"pattern": f'%"IMAGE_NAME": "{image}"%'},
		as_dict=True,
	)
	if not rows:
		frappe.throw(
			_("No promote Task found for {0} — cannot resolve the kernel to inherit.").format(image)
		)
	variables = frappe.parse_json(rows[0]["variables"]) or {}
	source_image = variables.get("SOURCE_IMAGE")
	if not source_image:
		frappe.throw(
			_("Promote Task for {0} recorded no SOURCE_IMAGE — cannot resolve the kernel.").format(image)
		)
	return source_image


def _serve_rootfs_over_http(source_server: str, image: str, mesh_address: str, port: int) -> str:
	"""Squash the base LV on its home host and start serving it over HTTP; return the
	squashfs sha256.

	One root bash script over SSH (mirrors `fleet_image._produce_and_upload_rootfs`):
	  1. stop any prior serve unit (so a re-run's `systemd-run --unit` and mksquashfs
	     don't collide), activate the read-only base LV, mount it read-only;
	  2. `mksquashfs` it to `<serve_dir>/rootfs.sqfs` (`.sqfs`, not `.squashfs`, so
	     sync-image skips the guest-module re-bake), umount, sha256 it;
	  3. `systemd-run` a transient `python3 -m http.server` bound to the host's mesh
	     address on `port`, capped by RuntimeMaxSec so a missed teardown self-heals;
	  4. echo the digest as an `ATLAS_FLEET_ROOTFS_SHA256=` line.

	Throws if the host run fails or the digest line is missing — the caller must not fan
	out a sync it can't hand a verified digest."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_server

	lv = f"/dev/atlas/atlas-image-{image}"
	serve_dir = f"{_SERVE_ROOT}/{image}"
	sqfs = f"{serve_dir}/rootfs.sqfs"
	unit = f"atlas-image-serve-{image}"

	script = "\n".join(
		[
			"set -euo pipefail",
			f"systemctl stop {shlex.quote(unit)} 2>/dev/null || true",
			f"lvchange -ay {shlex.quote(lv)} 2>/dev/null || true",
			f"rm -rf {shlex.quote(serve_dir)}",
			f"mkdir -p {shlex.quote(serve_dir)}",
			"mnt=$(mktemp -d)",
			f'mount -o ro {shlex.quote(lv)} "$mnt"',
			f'mksquashfs "$mnt" {shlex.quote(sqfs)} -noappend -quiet',
			'umount "$mnt"; rmdir "$mnt"',
			f"rootfs_sha=$(sha256sum {shlex.quote(sqfs)} | awk '{{print $1}}')",
			" ".join(
				[
					"systemd-run",
					f"--unit={shlex.quote(unit)}",
					"--property=RuntimeMaxSec=3600",
					"python3 -m http.server",
					str(port),
					f"--bind {shlex.quote(mesh_address)}",
					f"--directory {shlex.quote(serve_dir)}",
				]
			),
			'echo "ATLAS_FLEET_ROOTFS_SHA256=$rootfs_sha"',
		]
	)

	connection = connection_for_server(frappe.get_doc("Server", source_server))
	with ssh_key_file(connection.ssh_private_key) as key_path:
		out, err, code = run_ssh(connection, key_path, script, timeout_seconds=1800)
	if code != 0:
		frappe.throw(
			_("Serving image {0} on {1} failed (exit {2}): {3}").format(
				image, source_server, code, (err or out)[-500:]
			)
		)

	for line in (out or "").splitlines():
		line = line.strip()
		if line.startswith("ATLAS_FLEET_ROOTFS_SHA256="):
			digest = line.split("=", 1)[1].strip()
			if digest:
				return digest
	frappe.throw(_("Serving image {0} on {1} reported no rootfs sha256 digest").format(image, source_server))


def _teardown_http_server(source_server: str, image: str) -> None:
	"""Stop the serve unit and drop its staged squashfs on the home host. Best-effort:
	runs in a `finally`, so a failure here is logged, never raised (it must not mask the
	real distribution error, and the unit's RuntimeMaxSec caps it regardless)."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_server

	unit = f"atlas-image-serve-{image}"
	serve_dir = f"{_SERVE_ROOT}/{image}"
	script = "\n".join(
		[
			"set -uo pipefail",
			f"systemctl stop {shlex.quote(unit)} 2>/dev/null || true",
			f"rm -rf {shlex.quote(serve_dir)}",
		]
	)
	connection = connection_for_server(frappe.get_doc("Server", source_server))
	with ssh_key_file(connection.ssh_private_key) as key_path:
		out, err, code = run_ssh(connection, key_path, script, timeout_seconds=120)
	if code != 0:
		frappe.logger("atlas").error(
			f"fleet image serve teardown on {source_server} failed (exit {code}): {(err or out)[-300:]}"
		)


def _poll_tasks_to_terminal(task_names: list[str], timeout_seconds: int) -> dict:
	"""Block until every sync Task is terminal (Success/Failure) or the timeout hits;
	return `{task: final_status_or_None}` (None == still unfinished at timeout).

	`rollback` before each read starts a fresh transaction so the sync workers'
	committed status flips are visible (a long-lived read snapshot would never see
	them). Nothing here writes, so the rollback only refreshes."""
	deadline = time.monotonic() + timeout_seconds
	outcomes: dict = {name: None for name in task_names}
	while any(status is None for status in outcomes.values()) and time.monotonic() < deadline:
		time.sleep(_POLL_INTERVAL_SECONDS)
		frappe.db.rollback()
		for name, status in outcomes.items():
			if status is not None:
				continue
			current = frappe.db.get_value("Task", name, "status")
			if current in ("Success", "Failure"):
				outcomes[name] = current
	return outcomes
