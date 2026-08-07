"""Fleet distribution of a promoted snapshot as a NON-LOCAL base image.

`Virtual Machine Snapshot.promote_to_image` is same-server scope (spec/08): it dds
the snapshot LV into a local `atlas-image-<name>` LV and registers a URL-less
image row, so a baked golden can only be provisioned on the one host it was
promoted on. That leaves a gap — a golden bake (spec/15) is exactly what the fleet
wants everywhere, and no transport exists to get it there. This module fills the
gap by turning a promoted snapshot into the input shape the existing `sync-image`
verb already consumes: an HTTPS squashfs rootfs + an HTTPS kernel, each with a
sha256. It squashes the snapshot's rootfs LV ON the snapshot's host, uploads it to
S3 (public-read, so the plain non-expiring URL fits the image row's 140-char url
field), mints a NON-LOCAL `Virtual Machine Image`, and fans out `sync-image` to an
EXPLICIT list of servers — so each target host curls the squashfs, unsquashes,
builds a pristine ext4 and imports the read-only base LV `atlas-image-<name>`,
exactly as for any from-URL image.

The snapshot's rootfs is itself an LV (`rootfs_path`); we mount it read-only and
mksquashfs it — no dd, no host-side image dir. The kernel is free: the distributed
image REUSES the source image's `kernel_url` + `kernel_sha256` verbatim (the same
public bzImage the snapshot booted, which `sync-image` already knows how to unpack
— exactly as `promote_to_image` reuses the source kernel), so only the rootfs is
new bytes and only it goes to S3. The S3 bucket is the snapshot-backup bucket
(spec/29); only the `fleet-images/` key layout is new. The image row is inserted
INACTIVE so `after_insert` cannot blanket-fan-out to every Active server (the
fleet carries junk Active rows that must not receive the image); the caller names
the targets, and the row is activated only after those sync Tasks are enqueued.
"""

from __future__ import annotations

import shlex

import frappe
from frappe import _


def publish_snapshot_as_fleet_image(
	snapshot_name: str, image_name: str, *, servers=None, title: str | None = None
) -> dict:
	"""Publish `snapshot_name` (an Available cold snapshot) to the fleet as a base
	image named `image_name`: squash its rootfs LV, upload it public-read to S3, mint
	a NON-LOCAL `Virtual Machine Image` (a plain S3 rootfs URL + the source image's
	inherited kernel), and fan out the existing `sync-image` verb to `servers` — the
	EXPLICIT caller list (a JSON string or a Python list), never the implicit
	every-Active-server sweep, because the fleet has junk Active rows that must not
	receive the image.

	The image is inserted INACTIVE (`is_active=0`), so `after_insert` does not
	auto-sync it anywhere; `sync_to_all_servers(servers)` runs explicitly next, and
	only then is the row flipped active. Placement ignores inactive rows, so there
	is never a window where a host that was not asked to sync could provision from
	it. The caller (the chef service) polls `api.service.get_image(...).is_active`
	for readiness.

	Returns {image, rootfs_sha256, kernel_sha256, tasks}."""
	from atlas.atlas import s3

	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)

	if snapshot.status != "Available":
		frappe.throw(f"Snapshot is not Available (status is {snapshot.status})")
	if snapshot.kind == "Warm":
		frappe.throw(
			_(
				"A warm snapshot cannot be published to the fleet — its value is the frozen memory pair clones resume, which a cold-booting base image discards. Publish a cold snapshot, or clone this one with Clone to new VM."
			)
		)
	if snapshot.data_disk_gigabytes:
		frappe.throw(
			_(
				"This snapshot has a data disk; a base image captures only the root disk (the image has no data-disk fields), so publishing would silently drop it. Clone this snapshot with Clone to new VM to keep the data disk, or publish a data-less snapshot."
			)
		)
	if not snapshot.source_image:
		frappe.throw(_("Snapshot has no source image to inherit a kernel from; cannot publish."))

	image_name = (image_name or "").strip().lower()
	from atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot import _IMAGE_NAME_RE

	if not _IMAGE_NAME_RE.match(image_name):
		frappe.throw(
			f"Image name {image_name!r} is invalid — use lowercase letters, digits, "
			"dots and dashes (it becomes both the image record name and the LVM LV name)."
		)
	if frappe.db.exists("Virtual Machine Image", image_name):
		frappe.throw(f"A Virtual Machine Image named {image_name!r} already exists.")
	if not s3.is_configured():
		frappe.throw(_("S3 Settings is not configured — set the bucket and credentials first."))

	# The distributed image reuses the source image's kernel VERBATIM: the same
	# public bzImage the snapshot booted, which sync-image already unpacks. So only
	# the rootfs is produced + uploaded; the kernel row fields are copied over.
	source = frappe.db.get_value(
		"Virtual Machine Image",
		snapshot.source_image,
		["kernel_url", "kernel_filename", "kernel_sha256", "build_mode"],
		as_dict=True,
	)
	if not source or not source.kernel_url or not source.kernel_filename or not source.kernel_sha256:
		frappe.throw(
			f"Source image {snapshot.source_image} has no kernel_url/kernel_filename/kernel_sha256 "
			"to inherit; cannot publish (the distributed image reuses its kernel)."
		)

	backup = s3.S3Backup()
	# `.sqfs`, not `.squashfs`, ON PURPOSE: sync-image's installGuestModules swaps a
	# `.squashfs` rootfs URL's suffix for `.manifest` to fetch the Ubuntu cloud
	# image's package manifest and bake matching guest kernel modules into an
	# otherwise-empty /lib/modules. Our rootfs is squashed from a PROVISIONED VM
	# whose /lib/modules is already populated (baked by the source image's own
	# sync-image, and the fleet image REUSES that same kernel), so that re-bake is
	# redundant — and there is no sibling `.manifest` to fetch. A non-`.squashfs`
	# URL makes manifestURLFor return "" so sync-image skips the module bake; the
	# unsquashfs step reads the squashfs magic, not the extension.
	rootfs_key = f"{backup.key_prefix}/fleet-images/{image_name}/rootfs.sqfs"

	rootfs_sha256 = _produce_and_upload_rootfs(snapshot, image_name, backup, rootfs_key)
	# Flip the uploaded object public-read so its plain, non-expiring URL (short
	# enough for the 140-char url field) is what sync-image curls.
	backup.make_public(rootfs_key)
	rootfs_url = backup.public_url(rootfs_key)

	# Mint the image row INACTIVE (is_active=0): `after_insert` skips the auto
	# fan-out for an inactive row, so the only syncs that happen are the explicit
	# ones below — and placement ignores inactive rows, so there is no window where
	# an un-synced host could provision from it. A rootfs URL + digest + the
	# inherited kernel make it NON-LOCAL, so the ordinary sync-image path (curl,
	# unsquash, build ext4, import the base LV) applies. `rootfs_filename` is the
	# per-server on-disk ext4 name sync-image writes under the image dir.
	image = frappe.get_doc(
		{
			"doctype": "Virtual Machine Image",
			"image_name": image_name,
			"title": title or f"{image_name} (chef fleet image)",
			"kernel_url": source.kernel_url,
			"kernel_filename": source.kernel_filename,
			"kernel_sha256": source.kernel_sha256,
			"rootfs_url": rootfs_url,
			"rootfs_filename": f"{image_name}.ext4",
			"rootfs_sha256": rootfs_sha256,
			"default_disk_gigabytes": snapshot.disk_gigabytes,
			"build_mode": source.build_mode or None,
			"is_active": 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	# Fan out sync-image to the caller's explicit list only. sync_to_all_servers
	# accepts a JSON string or a Python list; with None it would default to every
	# Active server, which is exactly the blanket sweep the explicit list exists to
	# avoid (junk Active rows in the fleet must not receive the image).
	tasks = image.sync_to_all_servers(servers)

	# The sync Tasks are enqueued — now the image is provisionable. Flip active so
	# placement can select it and `api.service.get_image(...).is_active` reads true
	# for the chef service's readiness poll.
	image.db_set("is_active", 1)
	frappe.db.commit()

	return {
		"image": image.name,
		"rootfs_sha256": rootfs_sha256,
		"kernel_sha256": source.kernel_sha256,
		"tasks": tasks,
	}


def _produce_and_upload_rootfs(snapshot, image_name, backup, rootfs_key) -> str:
	"""Do the on-host half: squash the snapshot's rootfs LV into a squashfs, PUT it
	to S3, and return its sha256 digest.

	One root bash script over SSH on the snapshot's server (mirrors the
	`ImageBuild._assert_host_has_capacity` run_ssh wiring):
	  1. `lvchange -ay` the snapshot LV (a cold snapshot's LV may be deactivated
	     once its build VM stopped — already-active is fine), mount it read-only,
	     mksquashfs it to /tmp, umount + rmdir;
	  2. sha256 the /tmp squashfs, PUT it with the presigned URL, rm it;
	  3. echo the digest as an `ATLAS_FLEET_ROOTFS_SHA256=` line.

	Throws if the host run fails (non-zero exit) or the digest line is missing —
	the caller must not mint an image row it cannot vouch for."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_server

	if not snapshot.rootfs_path:
		frappe.throw(_("Snapshot has no rootfs_path to publish; nothing to squash."))

	put_rootfs = backup.presign_put(rootfs_key)
	lv = snapshot.rootfs_path
	rootfs_tmp = f"/tmp/fleet-{image_name}.squashfs"

	script = "\n".join(
		[
			"set -euo pipefail",
			f"lvchange -ay {shlex.quote(lv)} 2>/dev/null || true",
			"mnt=$(mktemp -d)",
			f'mount -o ro {shlex.quote(lv)} "$mnt"',
			f'mksquashfs "$mnt" {shlex.quote(rootfs_tmp)} -noappend -quiet',
			'umount "$mnt"; rmdir "$mnt"',
			f"rootfs_sha=$(sha256sum {shlex.quote(rootfs_tmp)} | awk '{{print $1}}')",
			f"curl -fsS -X PUT --upload-file {shlex.quote(rootfs_tmp)} {shlex.quote(put_rootfs)}",
			f"rm -f {shlex.quote(rootfs_tmp)}",
			'echo "ATLAS_FLEET_ROOTFS_SHA256=$rootfs_sha"',
		]
	)

	connection = connection_for_server(frappe.get_doc("Server", snapshot.server))
	with ssh_key_file(connection.ssh_private_key) as key_path:
		out, err, code = run_ssh(connection, key_path, script, timeout_seconds=1800)
	if code != 0:
		frappe.throw(
			_("Fleet-image production failed on {0} (exit {1}): {2}").format(
				snapshot.server, code, (err or out)[-500:]
			)
		)

	for line in (out or "").splitlines():
		line = line.strip()
		if line.startswith("ATLAS_FLEET_ROOTFS_SHA256="):
			digest = line.split("=", 1)[1].strip()
			if digest:
				return digest
	frappe.throw(
		_("Fleet-image production on {0} did not report the rootfs sha256 digest").format(snapshot.server)
	)
