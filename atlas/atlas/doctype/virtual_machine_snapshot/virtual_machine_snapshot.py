import json
import re

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result

# An image name becomes both a Frappe doc name (autoname field:image_name) and an
# LVM LV name (atlas-image-<name>). LVM LV names allow [a-zA-Z0-9+_.-]; we are
# stricter — lowercase alnum plus dot/dash — so the name is also a clean docname
# and a clean DNS-ish label. Reject anything else loudly rather than minting an LV
# the host's lvcreate would refuse or a docname Frappe would mangle.
_IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


class VirtualMachineSnapshot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_mode: DF.Literal["", "site", "admin"]
		data_disk_format_and_mount: DF.Check
		data_disk_gigabytes: DF.Int
		data_disk_mount_point: DF.Data | None
		data_rootfs_path: DF.Data | None
		disk_gigabytes: DF.Int
		host_signature: DF.SmallText | None
		kind: DF.Literal["Cold", "Warm"]
		memory_directory: DF.Data | None
		memory_megabytes: DF.Int
		rootfs_path: DF.Data | None
		s3_bucket: DF.Data | None
		s3_key_prefix: DF.Data | None
		s3_objects: DF.SmallText | None
		s3_status: DF.Literal["", "Uploading", "Uploaded", "Restoring", "Failed"]
		s3_uploaded_at: DF.Datetime | None
		server: DF.Link | None
		source_image: DF.Link | None
		status: DF.Literal["Pending", "Available", "Failed"]
		tap_device: DF.Data | None
		tenant: DF.Link | None
		title: DF.Data
		vcpus: DF.Int
		virtual_machine: DF.Link
	# end: auto-generated types

	@frappe.whitelist()
	def clone_to_new_vm(
		self,
		title: str,
		ssh_public_key: str,
		vcpus: int | None = None,
		cpu_max_cores: float | None = None,
		memory_megabytes: int | None = None,
		disk_gigabytes: int | None = None,
		tenant: str | None = None,
	) -> str:
		"""Create a NEW Virtual Machine whose disk is seeded from this snapshot.

		The clone is a fresh VM: new UUID, new IPv6, new MAC, new SSH host keys
		and machine-id (all re-derived at provision from the new UUID). It is a
		disk template, not a live-state resume — the safe path that avoids the
		duplicate-identity hazard Firecracker warns about. Disk defaults to the
		snapshot's size (the rootfs is already grown to it); a smaller value is
		rejected because the filesystem can't shrink to fit.

		The snapshot is a DURABLE artifact that outlives its build VM (self-serve
		sites clone from the golden indefinitely; the bake leaves the build VM as
		scratch and terminates it). So `server` comes from the snapshot's own row,
		not the source VM — and the source VM is consulted only as a fallback for
		the resource sizing a caller didn't pass. If the build VM is gone AND the
		caller passed no sizing, we fail loud with a clear message rather than
		`DoesNotExistError` deep in get_doc. The self-serve caller always passes
		an explicit size, so it never depends on the build VM surviving."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		if self.kind == "Warm":
			return self._clone_warm(
				title, ssh_public_key, vcpus, cpu_max_cores, memory_megabytes, disk_gigabytes, tenant
			)
		disk = int(disk_gigabytes) if disk_gigabytes else self.disk_gigabytes
		if disk < self.disk_gigabytes:
			frappe.throw(
				f"Clone disk ({disk} GB) cannot be smaller than the snapshot ({self.disk_gigabytes} GB)"
			)
		# Source VM is a sizing fallback only — it may have been terminated and its
		# row deleted (bake teardown) long after this durable golden was baked.
		source_vm = (
			frappe.get_doc("Virtual Machine", self.virtual_machine)
			if frappe.db.exists("Virtual Machine", self.virtual_machine)
			else None
		)
		new_vcpus, clone_cpu_max, clone_memory = self._clone_sizing(
			source_vm, vcpus, cpu_max_cores, memory_megabytes
		)
		clone = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": title,
				"server": self.server,
				"image": self.source_image,
				"vcpus": new_vcpus,
				"cpu_max_cores": clone_cpu_max,
				"memory_megabytes": clone_memory,
				"disk_gigabytes": disk,
				"ssh_public_key": ssh_public_key,
				"clone_source_rootfs": self.rootfs_path,
				# The data disk clones too: carry its size + mount config from the
				# snapshot, and seed it from the data-disk snapshot LV (empty when
				# the source had no data disk → a plain image clone with no /vdb).
				"data_disk_gigabytes": self.data_disk_gigabytes,
				"data_disk_format_and_mount": self.data_disk_format_and_mount,
				"data_disk_mount_point": self.data_disk_mount_point,
				"clone_source_data_rootfs": self.data_rootfs_path,
				# Carry the bench bake mode onto the clone, where its first-boot
				# deploy reads it (site → rename the baked site to the FQDN; admin →
				# map the FQDN to the admin console). Empty for a plain snapshot.
				"build_mode": self.build_mode or None,
				# The owning tenant (set by the Site/Pilot aggregate for a tenant-owned
				# clone) carries attribution onto the VM. Empty for an operator clone.
				"tenant": tenant or None,
			}
		).insert(ignore_permissions=True)
		return clone.name

	def _clone_warm(
		self,
		title: str,
		ssh_public_key: str,
		vcpus: int | None,
		cpu_max_cores: float | None,
		memory_megabytes: int | None,
		disk_gigabytes: int | None,
		tenant: str | None = None,
	) -> str:
		"""Clone that RESUMES this warm golden instead of booting it.

		The frozen vmstate pins the machine: a warm clone restores at exactly the
		captured vcpus/memory and on a byte-exact CoW of the captured disk (no
		grow — the frozen RAM's filesystem cache must keep matching it), so any
		mismatched override is rejected rather than silently breaking the
		restore. `cpu_max_cores` is free: it is a host-side cgroup cap, invisible
		to the guest. The clone keeps the golden's tap NAME (the vmstate binds
		the tap by name; names are netns-scoped, so N clones don't collide) and
		carries `warm_snapshot` so provision stages the memory pair + MMDS
		identity."""
		if vcpus and int(vcpus) != self.vcpus:
			frappe.throw(f"A warm clone restores at the captured size: vcpus must be {self.vcpus}")
		if memory_megabytes and int(memory_megabytes) != self.memory_megabytes:
			frappe.throw(
				f"A warm clone restores at the captured size: memory must be {self.memory_megabytes} MB"
			)
		if disk_gigabytes and int(disk_gigabytes) != self.disk_gigabytes:
			frappe.throw(
				f"A warm clone's disk cannot be resized: disk must be {self.disk_gigabytes} GB "
				"(the frozen memory state matches that exact disk)"
			)
		clone = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": title,
				"server": self.server,
				"image": self.source_image,
				"vcpus": self.vcpus,
				"cpu_max_cores": float(cpu_max_cores) if cpu_max_cores else float(self.vcpus),
				"memory_megabytes": self.memory_megabytes,
				"disk_gigabytes": self.disk_gigabytes,
				"ssh_public_key": ssh_public_key,
				"clone_source_rootfs": self.rootfs_path,
				"warm_snapshot": self.name,
				"tap_device": self.tap_device,
				# Carry the bench bake mode onto the warm clone (a warm v16 golden is
				# site mode), so its first-boot deploy maps the FQDN correctly.
				"build_mode": self.build_mode or None,
				# The owning tenant carries attribution onto the VM (see clone_to_new_vm).
				"tenant": tenant or None,
			}
		).insert(ignore_permissions=True)
		return clone.name

	def _clone_sizing(
		self,
		source_vm,
		vcpus: int | None,
		cpu_max_cores: float | None,
		memory_megabytes: int | None,
	) -> tuple[int, float, int]:
		"""Resolve (vcpus, cpu_max_cores, memory_megabytes) for a clone.

		Explicit caller args always win. For anything left unset we fall back to
		the source VM's value — but only if that row still exists. A golden whose
		build VM was terminated has no source to inherit from, so a caller that
		passes nothing gets a clear error here instead of a `DoesNotExistError`
		from get_doc on the dangling `virtual_machine` link."""
		new_vcpus = int(vcpus) if vcpus else (source_vm.vcpus if source_vm else None)
		clone_memory = (
			int(memory_megabytes) if memory_megabytes else (source_vm.memory_megabytes if source_vm else None)
		)
		if cpu_max_cores:
			clone_cpu_max = float(cpu_max_cores)
		elif source_vm:
			# Carry the source's cap so a fractional source clones to the same
			# fraction; when vcpus is overridden but the source was whole-core,
			# track the new vcpus (before_validate would otherwise default a
			# missing cap up to vcpus).
			if source_vm.cpu_max_cores == float(source_vm.vcpus):
				clone_cpu_max = float(new_vcpus)
			else:
				clone_cpu_max = float(source_vm.cpu_max_cores)
		else:
			clone_cpu_max = None
		if new_vcpus is None or clone_memory is None or clone_cpu_max is None:
			frappe.throw(
				f"Snapshot {self.name}'s build VM no longer exists — "
				"pass vcpus, cpu_max_cores and memory_megabytes explicitly to clone it."
			)
		return new_vcpus, clone_cpu_max, clone_memory

	@frappe.whitelist()
	def restore_to_vm(self) -> str:
		"""Restore this snapshot onto its own VM (rollback in place). Thin
		wrapper around Virtual Machine.rebuild so the Stopped-state guard and
		the Task all live in one place. Returns the Task name."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		virtual_machine = frappe.get_doc("Virtual Machine", self.virtual_machine)
		return virtual_machine.rebuild("snapshot", self.name)

	@frappe.whitelist()
	def promote_to_image(self, image_name: str, title: str | None = None) -> str:
		"""Promote this cold snapshot into a first-class same-server base image, so
		new VMs provision from it via the ordinary `image` field instead of locating
		a one-off snapshot to clone (spec/08-images.md, spec/15-image-builder.md).

		On `self.server`, `promote-snapshot-image.py` dd's the snapshot LV into a
		new read-only `atlas-image-<image_name>` LV; then we register a *local*
		(URL-less) `Virtual Machine Image` row pointing at it. The kernel is free —
		the row reuses the snapshot's `source_image` kernel (already on the server),
		so only the rootfs LV is new and nothing leaves the host.

		Warm snapshots are rejected up front: a warm snapshot's value is its frozen
		memory pair (clones RESUME it), and a base image's contract is the opposite —
		clones cold-boot and provision *requires* grow + tune2fs + identity injection,
		none of which the memory pair survives. Promoting a warm snapshot could only
		mean discarding that pair, throwing away the one thing that distinguishes it
		from an ordinary bake. So we throw on `kind == "Warm"`; promote a cold
		snapshot, clone the warm one with `clone_to_new_vm`. (Operator decision,
		2026-06-19.)

		**Promote is root-only.** A snapshot captures both the root and data disks,
		and `clone_to_new_vm` carries the data disk into a clone — but a base image is
		a *root* template (the `Virtual Machine Image` DocType has no data-disk
		fields), so a promoted image would silently drop the snapshot's data disk.
		Rather than lose data quietly, we throw on a data-disk snapshot: promote a
		data-less snapshot, or clone this one to preserve its data disk.

		**Async: insert INACTIVE, then a background job does the dd + activation.** The
		host `dd` of a ~28 GB rootfs takes ~35s — too long to hold a web request open
		(a gunicorn timeout mid-`dd` is what left Tasks stuck Running in production,
		2026-07-09). So this method inserts the image row `is_active=0`, enqueues
		`run_promote` (after_commit), and returns immediately; the job runs the `dd` and
		flips `is_active=1` once the Task confirms the LV exists. Placement ignores
		inactive images (placement.py), so a promote still mid-flight — or a job that
		died — can never be provisioned from. On a host failure the job deletes the
		inactive anchor and re-raises, so promote is all-or-nothing. We do NOT rely on
		the image insert rolling back with a host failure: `run_task` commits its own
		Task row mid-flight, so the two are not one transaction (relying on that rollback
		is what left orphaned `is_active=1` rows).

		Returns the new image's name. The image is INACTIVE until the background job
		finishes — poll `is_active` (or the enqueued Task) to know when it is
		provisionable."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		if self.kind == "Warm":
			frappe.throw(
				_(
					"A warm snapshot cannot be promoted to an image — its value is the frozen memory pair clones resume, which a cold-booting base image discards. Promote a cold snapshot, or clone this one with Clone to new VM."
				)
			)
		if self.data_disk_gigabytes:
			frappe.throw(
				_(
					"This snapshot has a data disk; a base image captures only the root disk (the image has no data-disk fields), so promoting would silently drop it. Clone this snapshot with Clone to new VM to keep the data disk, or promote a data-less snapshot."
				)
			)
		if not self.source_image:
			# The kernel is inherited from source_image; a snapshot with no recorded
			# source image (a malformed row) has no kernel to point the image at.
			frappe.throw(_("Snapshot has no source image to inherit a kernel from; cannot promote."))

		image_name = (image_name or "").strip().lower()
		if not _IMAGE_NAME_RE.match(image_name):
			frappe.throw(
				f"Image name {image_name!r} is invalid — use lowercase letters, digits, "
				"dots and dashes (it becomes both the image record name and the LVM LV name)."
			)
		if frappe.db.exists("Virtual Machine Image", image_name):
			frappe.throw(f"A Virtual Machine Image named {image_name!r} already exists.")

		source_kernel_filename = frappe.db.get_value(
			"Virtual Machine Image", self.source_image, "kernel_filename"
		)
		if not source_kernel_filename:
			frappe.throw(
				f"Source image {self.source_image} has no kernel_filename; cannot promote "
				"(the promoted image reuses its kernel)."
			)

		rootfs_filename = f"atlas-image-{image_name}"
		# Register the local image row as the durable anchor, but INACTIVE
		# (is_active=0) until the host dd confirms the LV exists. Placement only
		# considers is_active=1 images (placement.py), so an inactive row is invisible
		# to provisioning — a promote that dies mid-dd can never leave a provisionable
		# image whose LV is missing or half-written.
		#
		# Ordering matters because run_task is NOT transactional with this insert: it
		# commits its own Task row (Running, then the outcome) mid-flight, so a host
		# failure can NOT be relied on to roll the image row back with it (that was the
		# old assumption, and the source of orphaned is_active=1 rows). We make the
		# lifecycle explicit instead: insert inactive → dd → activate on success, and
		# delete the anchor ourselves on any raise. A worker KILL (no raise) still can't
		# hurt — the row stays inactive, so it is never provisioned from.
		#
		# Empty kernel_url/rootfs_url => a URL-less image (validate permits it;
		# after_insert/sync skip it — its bytes are the promoted LV, already on the
		# server, not a download). rootfs_filename is the LV name; the on-disk file is
		# a presence sentinel the host materializes (provision reads the LV).
		image = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": image_name,
				"title": (title or "").strip() or self.title,
				"kernel_url": "",
				"kernel_filename": source_kernel_filename,
				"kernel_sha256": "",
				"rootfs_url": "",
				"rootfs_filename": rootfs_filename,
				"rootfs_sha256": "",
				"default_disk_gigabytes": self.disk_gigabytes,
				# Carry the bench bake mode onto the base image, so a VM created from it
				# via the ordinary `image` field inherits build_mode and its first-boot
				# deploy maps the FQDN to the admin console (admin) or the baked site
				# (site) — the snapshot→clone path already carried it; this is the
				# promote→image path's equivalent (spec/08). Empty for a non-bench image.
				"build_mode": self.build_mode or None,
				"tenant": self.tenant,
				"is_active": 0,
			}
		).insert(ignore_permissions=True)

		# The host dd of a 28 GB rootfs takes ~35s — too long to hold a web request
		# open (a gunicorn timeout mid-dd is exactly what left Tasks stuck Running,
		# 2026-07-09). So the button returns here with the inactive anchor persisted,
		# and a background job does the dd + activation. enqueue_after_commit so the
		# worker only starts once this insert has committed (mirrors
		# Virtual Machine.after_insert → auto_provision). A killed/lost job leaves the
		# anchor inactive (harmless); the operator's retry re-drives _run_promote,
		# which is idempotent (the host dd is a no-op if the LV already exists).
		frappe.enqueue(
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_promote",
			queue="long",
			enqueue_after_commit=True,
			snapshot_name=self.name,
			image_name=image.name,
		)
		return image.name

	def _run_promote(self, image_name: str) -> None:
		"""Do the host half of a promote for the already-inserted inactive image row:
		dd the snapshot LV into atlas-image-<name>, materialize the image dir, then flip
		the row active. Idempotent — the host dd is a no-op if the LV exists, so a retry
		re-drives it safely. On any host failure we delete the inactive anchor and
		re-raise, so a failed promote leaves no ghost row (never a half-state). A killed
		job (no raise) leaves the row inactive, which placement ignores."""
		image = frappe.get_doc("Virtual Machine Image", image_name)
		if image.is_active:
			return  # already promoted (a raced retry) — nothing to do
		try:
			task = run_task(
				server=self.server,
				script="promote-snapshot-image",
				variables={
					"SNAPSHOT_ROOTFS_PATH": self.rootfs_path,
					"IMAGE_NAME": image_name,
					"DISK_GIGABYTES": str(self.disk_gigabytes),
					"ROOTFS_FILENAME": image.rootfs_filename,
					"SOURCE_IMAGE": self.source_image,
					"KERNEL_FILENAME": image.kernel_filename,
				},
				virtual_machine=self.virtual_machine,
				timeout_seconds=600,
			)
			parse_result(task.stdout)  # fail loud if the script produced no ATLAS_RESULT line
		except Exception:
			# Host dd failed (SSH error, non-zero exit, timeout, or a bad result line).
			# Drop the inactive anchor so a retry starts clean rather than colliding with
			# a ghost row, then re-raise so the failure surfaces in the job log.
			frappe.delete_doc("Virtual Machine Image", image.name, ignore_permissions=True, force=True)
			# nosemgrep: frappe-manual-commit -- persist the anchor deletion before re-raising so the failed promote leaves no ghost row
			frappe.db.commit()
			raise

		# The LV exists and the image dir is materialized — flip the anchor active so
		# placement can provision from it.
		image.db_set("is_active", 1)

	@frappe.whitelist()
	def upload_to_s3(self) -> None:
		"""Push this snapshot's artifacts to S3 for off-host durability — the disk
		LV(s), plus (warm) the frozen memory pair + host signature. The byte movement
		is minutes of zstd/curl, far too long for a web request (the gunicorn-timeout
		lesson promote_to_image learned), so this sets Uploading and enqueues a
		background job that returns immediately. Poll s3_status / the enqueued Task.
		Idempotent — a re-run overwrites the objects. See spec/29-snapshot-backup.md."""
		from atlas.atlas import s3

		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		if not self.server or not frappe.db.exists("Server", self.server):
			frappe.throw(_("Snapshot has no server to upload from."))
		if not s3.is_configured():
			frappe.throw(_("S3 Settings is not configured — set the bucket and credentials first."))
		if self.s3_status in ("Uploading", "Restoring"):
			frappe.throw(f"A backup operation is already running (s3_status is {self.s3_status}).")
		self.db_set("s3_status", "Uploading")
		frappe.enqueue(
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_upload",
			queue="long",
			enqueue_after_commit=True,
			snapshot_name=self.name,
		)

	def _run_upload(self) -> None:
		"""Background half of upload_to_s3: presign a PUT per artifact, run the host
		Task, and record the manifest. On any host failure set Failed and re-raise
		(loud at the boundary — the operator retries the button)."""
		from atlas.atlas import s3

		backup = s3.S3Backup()
		plan = s3.backup_plan(self)
		for obj in plan:
			obj["url"] = backup.presign_put(backup.object_key(self.name, obj["object_name"]))
		try:
			task = run_task(
				server=self.server,
				script="upload-snapshot-s3",
				variables={"SNAPSHOT_NAME": self.name, "OBJECTS_JSON": json.dumps(plan)},
				virtual_machine=self._task_vm(),
				timeout_seconds=3600,
			)
			result = parse_result(task.stdout)
		except Exception:
			self.db_set("s3_status", "Failed")
			# nosemgrep: frappe-manual-commit -- persist Failed before re-raising so the job log and row agree
			frappe.db.commit()
			raise
		self._record_upload(backup, plan, result)

	def _record_upload(self, backup, plan: list[dict], result: dict) -> None:
		"""Merge the plan (which artifact, where, how big) with the host's measured
		digests/sizes into the durable manifest, and flip the row Uploaded. The
		presigned url is deliberately NOT stored — it is a time-limited secret."""
		measured = {item["name"]: item for item in result["objects"]}
		manifest = [
			{
				"name": obj["name"],
				"object_name": obj["object_name"],
				"source": obj["source"],
				"block": obj["block"],
				"compress": obj["compress"],
				"disk_gigabytes": obj["disk_gigabytes"],
				"sha256": measured.get(obj["name"], {}).get("sha256", ""),
				"compressed_bytes": measured.get(obj["name"], {}).get("compressed_bytes", 0),
				"raw_bytes": measured.get(obj["name"], {}).get("raw_bytes", 0),
			}
			for obj in plan
		]
		self.db_set(
			{
				"s3_status": "Uploaded",
				"s3_bucket": backup.bucket,
				"s3_key_prefix": backup.prefix_for(self.name),
				"s3_size_bytes": result["total_compressed_bytes"],
				"s3_uploaded_at": frappe.utils.now_datetime(),
				"s3_objects": json.dumps(manifest),
			}
		)

	@frappe.whitelist()
	def restore_from_s3(self) -> None:
		"""Pull this snapshot's artifacts back from S3 and rehydrate its on-host
		LV(s) (+ warm memory pair) at the exact names the row already records, then —
		for a Cold snapshot whose VM is Stopped — roll the VM back in place. A Warm
		snapshot rehydrates only (consume it with Clone to new VM; warm's value is
		fan-out, not in-place rollback — the same asymmetry promote_to_image draws).
		Background job, like upload. See spec/29-snapshot-backup.md."""
		if self.s3_status != "Uploaded":
			frappe.throw(f"No S3 backup to restore (s3_status is {self.s3_status or 'empty'}).")
		if not self.server or not frappe.db.exists("Server", self.server):
			frappe.throw(_("Snapshot has no server to restore onto."))
		if self.kind == "Cold":
			self._guard_cold_rollback_target()
		self.db_set("s3_status", "Restoring")
		frappe.enqueue(
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_restore",
			queue="long",
			enqueue_after_commit=True,
			snapshot_name=self.name,
		)

	def _guard_cold_rollback_target(self) -> None:
		"""A cold restore rolls the VM back via rebuild(), which needs a Stopped VM.
		Check up front so a running (or vanished) VM fails BEFORE any multi-GB
		download rather than after."""
		if not frappe.db.exists("Virtual Machine", self.virtual_machine):
			frappe.throw(
				_(
					"The snapshot's VM no longer exists; restoring to a new VM on another server is out of scope (spec/29)."
				)
			)
		vm_status = frappe.db.get_value("Virtual Machine", self.virtual_machine, "status")
		if vm_status != "Stopped":
			frappe.throw(f"Stop {self.virtual_machine} before restoring (status is {vm_status}).")

	def _run_restore(self) -> str | None:
		"""Background half of restore_from_s3: presign a GET per manifest object, run
		the host Task to rehydrate the artifacts, then (cold) roll the VM back.
		Returns the rollback Task name for a cold snapshot, else None. On host failure
		set Failed and re-raise."""
		from atlas.atlas import s3

		backup = s3.S3Backup()
		manifest = json.loads(self.s3_objects or "[]")
		if not manifest:
			frappe.throw(_("No upload manifest to restore from."))
		plan = []
		for obj in manifest:
			item = dict(obj)
			item["url"] = backup.presign_get(backup.object_key(self.name, obj["object_name"]))
			plan.append(item)
		try:
			task = run_task(
				server=self.server,
				script="restore-snapshot-s3",
				variables={"SNAPSHOT_NAME": self.name, "OBJECTS_JSON": json.dumps(plan)},
				virtual_machine=self._task_vm(),
				timeout_seconds=3600,
			)
			parse_result(task.stdout)
		except Exception:
			self.db_set("s3_status", "Failed")
			# nosemgrep: frappe-manual-commit -- persist Failed before re-raising so the job log and row agree
			frappe.db.commit()
			raise
		# Rehydrated: the LVs + memory dir the row names exist again. The backup stays
		# in S3 (Uploaded), so a later re-restore still works.
		self.db_set("s3_status", "Uploaded")
		if self.kind == "Cold":
			return self.restore_to_vm()
		return None

	def _task_vm(self) -> str | None:
		"""The VM to stamp on the backup Task as an audit backpointer — dropped if the
		VM row is already gone (a durable snapshot can outlive its build VM)."""
		return self.virtual_machine if frappe.db.exists("Virtual Machine", self.virtual_machine) else None

	def _delete_s3_backup(self) -> None:
		"""Best-effort: drop this snapshot's S3 objects when the row is deleted, so a
		deleted backup leaves no paid orphan. Never blocks the local delete — an S3
		error is logged, not raised (a bucket hiccup must not wedge a teardown)."""
		if self.s3_status != "Uploaded":
			return
		try:
			from atlas.atlas import s3

			if s3.is_configured():
				s3.S3Backup().delete_prefix(self.name)
		except Exception:
			frappe.log_error(f"S3 backup cleanup failed for snapshot {self.name}", "snapshot backup")

	def on_trash(self) -> None:
		"""Remove the on-host snapshot LV when the row is deleted.

		The snapshot LV is the only thing this row points at; once the row is
		gone the LV is dead weight. We remove it in the same gesture so the pool
		doesn't accumulate orphans. Idempotent script — a missing LV is a no-op.

		Unlike the old file-backed snapshots (which lived under the VM directory
		and were swept by terminate-vm.py's `rm -rf`), a snapshot LV lives in the
		thin pool, OUTSIDE the VM directory — so it survives terminate's directory
		removal and MUST be lvremoved here even when terminate() cascades the row
		deletions of a Terminated VM. (No Terminated short-circuit: that would
		leak the snapshot LV.)"""
		# Drop the off-host S3 backup first (independent of the server/LV path below,
		# which the guards can short-circuit — the S3 objects must be swept even if
		# the server row is gone). Best-effort: never blocks the local delete.
		self._delete_s3_backup()
		if not self.server or not self.rootfs_path:
			return
		if not frappe.db.exists("Server", self.server):
			return
		# The VM link is an audit backpointer on the teardown Task, not something the
		# LV removal needs. An orphaned snapshot (its VM already deleted, e.g. by a
		# host reset) must still tear its LV down, so drop a dangling link rather than
		# let Task's link validation throw on a VM that no longer exists.
		virtual_machine = self.virtual_machine
		if virtual_machine and not frappe.db.exists("Virtual Machine", virtual_machine):
			virtual_machine = None
		# Remove both halves of the snapshot: the root snap LV and (when the VM had
		# a data disk) the data snap LV. The empty data path is dropped by the Task
		# runner, so a data-less snapshot's teardown is unchanged. A warm row also
		# owns its durable memory directory (vmstate/mem/host-signature) — same
		# gesture: clone jails only hold hard links, so removing the directory
		# never breaks a clone already provisioned from it.
		run_task(
			server=self.server,
			script="delete-snapshot-vm",
			variables={
				"SNAPSHOT_ROOTFS_PATH": self.rootfs_path,
				"DATA_SNAPSHOT_ROOTFS_PATH": self.data_rootfs_path or "",
				"MEMORY_DIRECTORY": self.memory_directory or "",
			},
			virtual_machine=virtual_machine,
			timeout_seconds=60,
		)


def run_promote(snapshot_name: str, image_name: str) -> None:
	"""Background-job entrypoint (enqueued by promote_to_image). Runs the host dd +
	activation for an already-inserted inactive image row. No-op if the snapshot is
	gone (operator deleted it) or the image already active (a raced retry)."""
	if not frappe.db.exists("Virtual Machine Snapshot", snapshot_name):
		return
	if not frappe.db.exists("Virtual Machine Image", image_name):
		return  # anchor was cleaned up by a prior failed run; nothing to drive
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
	snapshot._run_promote(image_name)


def run_upload(snapshot_name: str) -> None:
	"""Background-job entrypoint (enqueued by upload_to_s3). No-op if the snapshot
	was deleted before the job ran."""
	if not frappe.db.exists("Virtual Machine Snapshot", snapshot_name):
		return
	frappe.get_doc("Virtual Machine Snapshot", snapshot_name)._run_upload()


def run_restore(snapshot_name: str) -> None:
	"""Background-job entrypoint (enqueued by restore_from_s3). No-op if the snapshot
	was deleted before the job ran."""
	if not frappe.db.exists("Virtual Machine Snapshot", snapshot_name):
		return
	frappe.get_doc("Virtual Machine Snapshot", snapshot_name)._run_restore()
