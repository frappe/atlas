"""Image Build — the operator-facing object that bakes an image end to end.

One row per bake run. It owns the full lifecycle the two e2e modules used to
hand-roll: provision a scratch build VM → upload the recipe's committed tree and
run build.sh inside it over guest-SSH (the shared `image_builder.run_build` seam)
→ stop + snapshot → optionally register the snapshot into Atlas Settings and
terminate the build VM. The snapshot is the output; the build VM is scratch.

The lifecycle mirrors `Site` (spec/14): an immutable identity tuple guarded in
validate(), a controller-written `status` Select (read-only on the form),
after_insert() enqueues the run on `queue="long"` (it SSHes and waits ~10-20 min),
and each status transition is committed + pushed to the operator over realtime so
the desk form's checklist updates live. See spec/15-image-builder.md.
"""

import time

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import bench_image
from atlas.atlas.image_builder import run_build
from atlas.atlas.image_recipes import get_recipe
from atlas.atlas.placement import default_image
from atlas.atlas.ssh import run_task

# The routing identity of a build: what to bake, where, and on which base. Once
# written they are fixed — re-baking with a different recipe/server/base is a new
# row, not an in-place edit (same shape as Site's IMMUTABLE_AFTER_INSERT).
IMMUTABLE_AFTER_INSERT = (
	"recipe",
	"server",
	"base_image",
)


class ImageBuild(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_register: DF.Check
		base_image: DF.Link | None
		build_inputs: DF.Code | None
		build_task: DF.Link | None
		build_virtual_machine: DF.Link | None
		error: DF.SmallText | None
		recipe: DF.Literal[
			"bench-v16",
			"bench-v15",
			"bench-nightly",
			"bench-v16-admin",
			"bench-v15-admin",
			"bench-nightly-admin",
			"proxy",
		]
		server: DF.Link
		snapshot: DF.Link | None
		status: DF.Literal["Draft", "Provisioning", "Building", "Snapshotting", "Available", "Failed"]
		terminate_build_vm: DF.Check
		title: DF.Data | None
		warm: DF.Check
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Resolve the recipe and fill what the operator didn't pick.

		Copy the recipe's human title for the list view, default the base image
		from Atlas Settings, and start Draft. The build VM is created in the
		background job (after_insert), not here — provisioning SSHes and must not
		block the insert."""
		recipe = get_recipe(self.recipe)
		self.title = recipe.title
		if not self.base_image:
			self.base_image = default_image()
		if self.warm and not recipe.warm_entrypoint:
			frappe.throw(f"The {recipe.title} recipe has no warm entrypoint; it can only bake cold")
		if not self.status:
			self.status = "Draft"

	def validate(self) -> None:
		self._validate_immutability()

	def after_insert(self) -> None:
		"""Enqueue the bake. queue=long because it SSHes and waits on a multi-minute
		in-guest build; mirrors Site.after_insert / VirtualMachine.after_insert.

		enqueue_after_commit so the worker only starts once this insert's
		transaction has committed — otherwise the job can dequeue and look up
		`run(image_build_name)` before the row exists ("Image Build ... not found")."""
		frappe.enqueue(
			"atlas.atlas.doctype.image_build.image_build.run",
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			image_build_name=self.name,
		)

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(original, field) != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def rebake(self) -> None:
		"""Re-run the bake on a row that already ran (Available or Failed).

		Resets status to Draft and re-enqueues. The whole pipeline is idempotent —
		build.sh re-runs cleanly, and a re-bake reuses the existing build VM if it
		survived — so this is the operator's retry button (spec taste #17: the
		operator retries by clicking the button)."""
		if self.status not in ("Available", "Failed"):
			frappe.throw(f"Can only re-bake an Available or Failed build (status is {self.status})")
		self.db_set("status", "Draft")
		self.db_set("error", None)
		# nosemgrep: frappe-manual-commit -- persist the Draft status reset before re-enqueuing the build so the background job sees the cleared state
		frappe.db.commit()
		self.after_insert()

	@frappe.whitelist()
	def promote(self, image_name: str | None = None, title: str | None = None) -> str:
		"""Promote this build's snapshot into a first-class same-server base image,
		so new VMs provision from it via the ordinary `image` field instead of
		cloning the one-off snapshot (spec/08-images.md, spec/15-image-builder.md).

		A thin delegate to `Virtual Machine Snapshot.promote_to_image`: the warm
		reject and every guard live once, in the snapshot method, so a warm bake's
		Image Build surfaces the same clean error.

		A versioned bench recipe defaults its promoted image to the series name it
		pins (`recipe.promote_image_name` = `bench-v15` / `bench-v16` /
		`bench-nightly`), so customers pick the version through the ordinary VM
		`image` field (spec/15). A recipe with no series name (proxy, a one-off
		bench) falls back to the `<recipe>-<build name>` slug — unique because build
		names are. An explicit `image_name` always wins. Returns the new image's
		name."""
		if self.status != "Available":
			frappe.throw(f"Can only promote an Available build (status is {self.status})")
		if not self.snapshot:
			frappe.throw(_("This build has no snapshot to promote."))
		recipe = get_recipe(self.recipe)
		default_name = recipe.promote_image_name or f"{self.recipe}-{self.name}".lower()
		image_name = (image_name or "").strip() or default_name
		snapshot = frappe.get_doc("Virtual Machine Snapshot", self.snapshot)
		return snapshot.promote_to_image(image_name=image_name, title=title or self.title)


def run(image_build_name: str) -> None:
	"""Background-job entrypoint (enqueued by after_insert / rebake). Bakes one
	image end to end:

	  1. provision a scratch build VM at the recipe's size (status → Provisioning),
	  2. wait for it to boot,
	  3. upload the recipe tree + run build.sh inside it (status → Building),
	  4. stop + snapshot it (status → Snapshotting → Available),
	  5. optionally register the snapshot into Atlas Settings + terminate the VM.

	On any failure the row is marked Failed (fail loud) and the exception re-raised
	so the job log carries the traceback. No-op if the build has moved past Draft
	(an operator raced us / a duplicate enqueue)."""
	build = frappe.get_doc("Image Build", image_build_name)
	if build.status != "Draft":
		return
	recipe = get_recipe(build.recipe)
	try:
		_set_status(build, "Provisioning")
		# Fail loud NOW if the host can't hold the (possibly fat) build VM — before we
		# create a VM row and hand it to a host that will OOM-kill it mid-build.
		_assert_host_has_capacity(build.server, recipe.effective_build_memory_megabytes)
		vm_name = _provision_build_vm(build, recipe)
		build.db_set("build_virtual_machine", vm_name)
		# COMMIT before waiting: the build VM's own after_insert enqueued its boot
		# job in a SEPARATE transaction that can't run until this one commits. Same
		# reasoning (and the same hazard) as Site.auto_provision — hold the txn open
		# and the boot never happens, the wait times out, and the rollback deletes
		# the VM row, orphaning its boot job.
		frappe.db.commit()
		_wait_for_vm_running(vm_name)
		_set_status(build, "Building")
		# Link the build Task for the audit trail — set even on a failed build
		# (on_task fires before run_build throws). stream=True (spec/22): the build
		# Task is created Running up front and tails the in-guest build.sh log live, so
		# the operator on this form sees the bake progress instead of an opaque ~10-20
		# min "Building" with no Task. on_task links the streamed row the same way.
		run_build(
			vm_name,
			recipe,
			on_task=lambda task_name: build.db_set("build_task", task_name),
			stream=True,
		)
		# Harvest the resolved input commits build.sh stamped (ATLAS_BUILD_*= lines in
		# the build Task's stdout) into the build's audit — chiefly so a nightly image,
		# whose develop branches float, is traceable to the exact frappe/erpnext SHAs
		# it baked. Best-effort: a parse miss never fails the bake.
		_record_build_inputs(build)
		# Sanity-gate a bench build BEFORE it snapshots: prove the freshly-baked VM
		# actually serves and (site mode) that the baked Administrator password logs
		# in. build.sh's own gate is unauthenticated ping only, so a broken-auth build
		# would otherwise snapshot clean and break a customer at first login. A miss
		# raises → the except below marks the build Failed, no snapshot. Proxy builds
		# bake no Frappe site, so they keep their own in-build health check.
		if not recipe.is_proxy:
			bench_image.sanity_check(vm_name)
		# The build VM may have booted FAT (recipe.build_memory_megabytes, so the
		# Node-asset build had headroom); a clone must restore SMALL. Firecracker
		# can't resize a live VM, so shrink now (stop → resize). After this the VM's
		# memory IS recipe.memory_megabytes, so both snapshot paths capture the small
		# size. The warm path boots the VM back up itself (in _warm_snapshot) at this
		# small size before its capture; the cold path stays Stopped for its snapshot.
		# No-op when the recipe didn't fatten.
		_resize_to_restore_memory(recipe, vm_name)
		_set_status(build, "Snapshotting")
		if build.warm:
			snapshot_name = _warm_snapshot(build, recipe, vm_name)
		else:
			snapshot_name = _stop_and_snapshot(build, recipe, vm_name)
		build.db_set("snapshot", snapshot_name)
		if build.auto_register and recipe.registers_as:
			_register(recipe, snapshot_name)
		if build.warm:
			# AFTER register, so a previously-registered warm golden has already
			# been replaced as the cold fallback and can be superseded cleanly
			# (with auto_register off it stays registered and is kept).
			_supersede_warm_snapshots(frappe.get_doc("Virtual Machine Snapshot", snapshot_name))
		_set_status(build, "Available")
		if build.terminate_build_vm:
			_terminate_build_vm(vm_name)
	except Exception:
		# Fail loud: mark the row (committed in _set_status so it survives the job's
		# rollback) and re-raise so the job log carries the traceback.
		build.db_set("error", frappe.get_traceback()[-500:])
		_set_status(build, "Failed")
		raise


def _set_status(build, status: str) -> None:
	"""Persist a status transition, COMMIT it (so the desk form's polling fallback
	sees it cross-transaction, and the Failed write survives the job's rollback),
	then push the new status to the operators' realtime room.

	Published to the `Image Build` *doc room* (not a user room): the operator is on
	the form, which auto-subscribes to its doc events, so a refresh-free checklist
	update needs no client-side dance. Emitted after the commit so the realtime
	payload never races ahead of the committed row."""
	build.db_set("status", status)
	# nosemgrep: frappe-manual-commit -- background job: commit each status transition so the desk form's polling sees it cross-transaction and the Failed write survives the job's rollback
	frappe.db.commit()
	frappe.publish_realtime(
		event="image_build_progress",
		message={"name": build.name, "status": status},
		doctype="Image Build",
		docname=build.name,
	)


# Host RAM the build VM needs BEYOND its own guest memory: firecracker's VMM
# footprint plus a safety cushion, so a host that only "just fits" on paper doesn't
# OOM-kill the guest at its peak build RSS.
_BUILD_VM_HOST_MARGIN_MB = 512


def _assert_host_has_capacity(server_name: str, needed_megabytes: int) -> None:
	"""Fail loud BEFORE booting the build VM if its host can't hold it.

	A bench bake boots a FAT build VM (recipe.build_memory_megabytes — e.g. 6 GB, for
	the Node asset build's headroom). If the host has less free memory than that, the
	host's OOM-killer reaps the firecracker process the moment the guest's RSS grows;
	systemd then restarts the VM small + empty, wiping /tmp (the detached build.sh and
	its done-marker), and run_detached polls a marker that can never appear until its
	1800s timeout — a 30-minute silent hang for a condition ONE SSH call detects. Probe
	the host's available memory up front and throw an actionable error instead.

	MemAvailable (not MemTotal) so the check accounts for a host that is small OR
	already busy with other VMs. Fail-open on an unreadable probe: a parse miss skips
	the guard rather than blocking an otherwise-fine bake."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_server

	connection = connection_for_server(frappe.get_doc("Server", server_name))
	with ssh_key_file(connection.ssh_private_key) as key_path:
		out, _stderr, _code = run_ssh(
			connection, key_path, "awk '/^MemAvailable:/ {print $2}' /proc/meminfo", timeout_seconds=30
		)
	available_megabytes = int(out.strip() or 0) // 1024
	if available_megabytes and available_megabytes < needed_megabytes + _BUILD_VM_HOST_MARGIN_MB:
		frappe.throw(
			f"Host {server_name} has {available_megabytes} MB available but the build VM needs "
			f"{needed_megabytes} MB (+{_BUILD_VM_HOST_MARGIN_MB} MB firecracker margin). Bake on a "
			f"larger host — a 6 GB fat build VM needs an ~8 GB host."
		)


def _provision_build_vm(build, recipe) -> str:
	"""Insert a scratch Virtual Machine at the recipe's size and return its name.

	Its own after_insert auto-provisions it (boots it) in a separate job — we wait
	on that in _wait_for_vm_running after committing. A proxy build's VM carries
	is_proxy so its build (and the recipe's finalize) takes the proxy path; the
	region it serves is read from Atlas Settings at finalize time. A bench build's
	VM is a plain VM. The fleet SSH key is baked into every VM the standard way, so
	build_proxy/build_bench reach the guest with it."""
	ssh_public_key = frappe.db.get_single_value("Atlas Settings", "ssh_public_key")
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"{recipe.title} — build",
			"server": build.server,
			"image": build.base_image,
			"vcpus": recipe.vcpus,
			# Boot at the (possibly fat) BUILD memory; _resize_for_snapshot shrinks it
			# to recipe.memory_megabytes before the snapshot, so the captured/restore
			# size stays small. effective_* is recipe.memory_megabytes when unset (proxy).
			"memory_megabytes": recipe.effective_build_memory_megabytes,
			"disk_gigabytes": recipe.disk_gigabytes,
			"ssh_public_key": ssh_public_key,
			"is_proxy": 1 if recipe.is_proxy else 0,
			# Stamp the bake mode (bench recipes only; empty for proxy). It rides the
			# build VM → its snapshot → a clone, so a customer VM's first boot maps its
			# FQDN to the baked site (site) or the admin console (admin) — spec/08.
			"build_mode": recipe.build_mode or None,
		}
	).insert(ignore_permissions=True)
	return vm.name


def _wait_for_vm_running(vm_name: str, timeout_seconds: int = 1500, poll_seconds: float = 5.0) -> None:
	"""Block until the build VM's own after_insert provision job flips it to Running.

	Poll its COMMITTED status with rollback() (the boot job commits in its own txn).
	Mirrors Site._wait_for_vm_running / the e2e _tasks.wait_for_vm_running — the
	proven contract for waiting on after_insert auto-provision. Raises on Failed or
	the deadline (the worker never ran the boot job)."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Virtual Machine", vm_name, "status")
		if status == "Running":
			return
		if status == "Failed":
			frappe.throw(f"Build VM {vm_name} reached Failed during provision")
		time.sleep(poll_seconds)
	frappe.throw(f"Build VM {vm_name} did not reach Running within {timeout_seconds}s")


def _wait_for_guest_serving(vm_name: str, timeout_seconds: int = 300, poll_seconds: float = 5.0) -> None:
	"""Block until the (just-restarted) build guest's production stack is SERVING.

	start-vm returns when the firecracker unit is launched, not when the guest has
	finished booting and MariaDB/gunicorn/nginx are answering again. The warm arm
	(`warm.sh`) opens with `bench browse` + real HTTP against that stack, so a
	still-booting guest fails it. Reuse bench_image.sanity_check as the readiness
	probe — the same serve+login proof the post-build gate uses — and retry it until
	it passes or the deadline. Each miss is a not-yet-up guest (SSH refused, ping not
	200, login not minted yet); the last error is surfaced on timeout."""
	deadline = time.monotonic() + timeout_seconds
	last_error = ""
	while time.monotonic() < deadline:
		try:
			bench_image.sanity_check(vm_name)
			return
		except Exception as exc:
			last_error = str(exc)
			time.sleep(poll_seconds)
	frappe.throw(
		f"Build guest {vm_name} did not serve within {timeout_seconds}s after start; "
		f"last probe error: {last_error[-300:]}"
	)


def _sync_guest_before_stop(vm_name: str) -> None:
	"""Flush the guest's page cache to its disk before we terminate it.

	The build path stops the VM with a plain `systemctl stop` of the firecracker
	unit — that KILLS the guest, it does not ACPI-shut-it-down, so the guest never
	runs its own `sync`. Anything still dirty in the guest page cache at that instant
	is lost: ext4 journals the inode + dirent but not the data, so the snapshot
	captures the file as 0 bytes. build.sh's own trailing `sync` covers the normal
	case, but this is the durable seam — flush again right before the terminate so a
	late write from anywhere (build.sh, a warm arm, an operator) is captured whole.
	Best-effort: a guest we cannot reach is about to be stopped anyway."""
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_guest

	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status != "Running":
		return
	try:
		connection = connection_for_guest(vm)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			run_ssh(connection, key_path, "sync", timeout_seconds=60)
	except Exception:
		frappe.log_error(f"guest sync before snapshot stop failed for {vm_name}")


def _resize_to_restore_memory(recipe, vm_name: str) -> None:
	"""Shrink a fat build VM down to its clone-RESTORE memory before the snapshot.

	The build VM booted at recipe.effective_build_memory_megabytes (fat, so the
	Node-asset build had headroom); a clone only needs recipe.memory_megabytes to
	serve, and the snapshot captures whatever the VM's memory is at capture — so we
	must apply the small size on the VM row + host BEFORE either snapshot path runs.

	Firecracker can't resize a live VM (resize() requires Stopped), so: stop → resize.
	Leaves the VM Stopped at the small size. The cold path snapshots from Stopped
	anyway; the WARM path boots it back up itself (_warm_snapshot, now at the small
	size) before its capture. No-op when the recipe didn't fatten (build == restore
	memory)."""
	if recipe.effective_build_memory_megabytes == recipe.memory_megabytes:
		return
	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status != "Stopped":
		_sync_guest_before_stop(vm_name)
		vm.stop()
		vm.reload()
	vm.resize(memory_megabytes=recipe.memory_megabytes)


def _stop_and_snapshot(build, recipe, vm_name: str) -> str:
	"""Stop the build VM and snapshot it. A Stopped VM gives a clean unmount → a
	flush-consistent ext4 (Virtual Machine.snapshot's safe default), and the
	snapshot is the rollable artifact. Returns the snapshot name."""
	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status != "Stopped":
		_sync_guest_before_stop(vm_name)
		vm.stop()
		vm.reload()
	return vm.snapshot(title=recipe.snapshot_title)


def _warm_snapshot(build, recipe, vm_name: str) -> str:
	"""The warm counterpart of _stop_and_snapshot: arm the RUNNING build VM
	(production stack up + pre-warmed + the identity freshen unit live — the
	recipe's warm entrypoint, in-guest), then capture its memory AND disk at one
	paused instant into a `Warm` snapshot row whose durable artifacts live on the
	server. Only then is the build VM stopped — the warmth is in the artifact,
	not the scratch VM. Supersedes the server's previous warm snapshot (one
	current warm golden per server). Returns the snapshot name."""
	from atlas.atlas.networking import derive_uid
	from atlas.atlas.task_results import parse_result

	vm = frappe.get_doc("Virtual Machine", vm_name)
	# A warm capture freezes the RUNNING guest's live RAM, so the VM must be booted
	# and serving before we arm and freeze it. The resize step (_resize_to_restore_
	# memory) leaves the VM Stopped whenever it stopped-to-resize and only reboots on
	# the fattened path — so a warm bake whose recipe did NOT fatten (build == restore
	# memory) reaches here Stopped. Boot it ourselves rather than depending on the
	# resize side effect: the warm path owns getting its own guest warm.
	if vm.status != "Running":
		# start() is SYNCHRONOUS — it runs the start-vm task inline and returns with
		# the VM Running + saved. Do NOT poll with _wait_for_vm_running here: that
		# waiter rolls back on each iteration (it is built for an after_insert boot
		# committed in a SEPARATE txn), which would wipe start()'s not-yet-committed
		# save() in THIS job's txn and hang forever seeing the pre-start Stopped row.
		# Commit the Running write instead so it is durable before we arm + freeze.
		vm.start()
		# nosemgrep: frappe-manual-commit -- background job: persist the build VM's Running status before the warm capture arms and freezes it
		frappe.db.commit()
		# start-vm returns when the unit is LAUNCHED, not when the guest has booted and
		# the production stack is serving again — warm.sh's first act is `bench browse`
		# + real HTTP against that stack, which fails on a still-booting guest. Wait for
		# the guest to actually SERVE (the sanity probe: ping 200/pong + a real login)
		# before arming. A warm bake is site mode only, so this is the site probe.
		_wait_for_guest_serving(vm_name)
	_run_warm_entrypoint(recipe, vm)
	snapshot = frappe.get_doc(
		{
			"doctype": "Virtual Machine Snapshot",
			"title": recipe.snapshot_title,
			"virtual_machine": vm.name,
			"server": vm.server,
			"status": "Pending",
			"kind": "Warm",
			"source_image": vm.image,
			"disk_gigabytes": vm.disk_gigabytes,
			# Carry the bench bake mode (a warm v16 golden is site mode) so a warm
			# clone's first-boot deploy maps the FQDN correctly (spec/08).
			"build_mode": vm.build_mode or None,
			# The frozen vmstate pins the machine and its tap name; a warm clone
			# must reproduce all three exactly (clone_to_new_vm enforces it).
			"vcpus": vm.vcpus,
			"memory_megabytes": vm.memory_megabytes,
			"tap_device": vm.tap_device,
		}
	).insert(ignore_permissions=True)
	rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
	memory_directory = f"/var/lib/atlas/snapshots/{snapshot.name}"
	task = run_task(
		server=vm.server,
		script="warm-snapshot-vm",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"ATLAS_FC_UID": str(derive_uid(vm.name)),
			"SNAPSHOT_ROOTFS_PATH": rootfs_path,
			"MEMORY_DIRECTORY": memory_directory,
		},
		virtual_machine=vm.name,
		timeout_seconds=600,
	)
	result = parse_result(task.stdout)
	snapshot.db_set(
		{
			"rootfs_path": rootfs_path,
			"size_bytes": result["size_bytes"],
			"memory_directory": memory_directory,
			"memory_bytes": result["memory_bytes"],
			"host_signature": result["host_signature"],
			"status": "Available",
		}
	)
	# The capture resumed the golden; stop it now — the artifact is durable and
	# the build VM is scratch from here on (kept Stopped unless terminate_build_vm).
	vm.reload()
	vm.stop()
	return snapshot.name


def _run_warm_entrypoint(recipe, vm) -> None:
	"""Run the recipe's warm entrypoint in the guest, recorded as a Task like every
	guest op. Passes the build VM's uuid: it becomes the in-guest 'identity already
	adopted' marker the freshen unit compares MMDS against.

	run_build staged the tree once, but it lives under /tmp (tmpfs) and the warm bake
	reboots the guest before it gets here (the fat-boot resize, then the warm-snapshot
	boot), which wipes it — so re-stage the tree first. warm.sh reads its siblings
	(bench.toml et al) from its own directory just like build.sh, so the whole tree
	goes back, not just warm.sh."""
	import shlex

	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.image_builder import stage_recipe_tree
	from atlas.atlas.proxy import _record_guest_task
	from atlas.atlas.ssh import connection_for_guest

	connection = connection_for_guest(vm)
	command = (
		f"bash {shlex.quote(f'{recipe.remote_directory}/{recipe.warm_entrypoint}')} {shlex.quote(vm.name)}"
	)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		stage_recipe_tree(recipe, connection, key_path)
		stdout, stderr, code = run_ssh(connection, key_path, command, timeout_seconds=900)
	_record_guest_task(vm.name, "bench-warm", {"recipe": recipe.name}, stdout, stderr, code)
	if code != 0:
		frappe.throw(f"Warm entrypoint on {vm.name} failed (exit {code}): {stderr[-500:]}")


def _supersede_warm_snapshots(snapshot) -> None:
	"""One current warm golden per server: trash older Warm rows on this server
	(their on_trash removes the LV + memory directory). The row Atlas Settings
	currently points at is left alone — never dangle the cold-fallback pointer;
	it is replaced right after by _register (auto_register) or by the operator.

	force=1 is load-bearing: an older warm row is linked from ITS Image Build
	row (`snapshot`), and delete_doc runs on_trash (which destroys the host
	artifacts, non-transactionally) BEFORE the link check — a plain delete would
	abort on the link and roll back to an Available row whose LV and memory pair
	are already gone, the exact stale-golden trap clone provisioning then trips
	over. Skipping the link check leaves the old Image Build an audit row with a
	dangling snapshot link, which Frappe tolerates."""
	registered = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	for name in frappe.get_all(
		"Virtual Machine Snapshot",
		filters={"server": snapshot.server, "kind": "Warm", "name": ("!=", snapshot.name)},
		pluck="name",
	):
		if name == registered:
			continue
		frappe.delete_doc("Virtual Machine Snapshot", name, ignore_permissions=True, force=1)


def _record_build_inputs(build) -> None:
	"""Parse the `ATLAS_BUILD_*=` lines build.sh stamped into the build Task's stdout
	(the resolved frappe / erpnext / bench-cli commit SHAs) and stash them as JSON on
	`build_inputs`. Traceability for the nightly variant above all — its develop
	branches float, so the SHAs are the only record of what a given image really is.

	Best-effort: no build Task, no stamp lines, or a stdout that can't be read just
	leaves build_inputs empty. This is an audit nicety, never a reason to fail a bake
	that otherwise succeeded."""
	if not build.build_task:
		return
	stdout = frappe.db.get_value("Task", build.build_task, "stdout") or ""
	inputs = {}
	for line in stdout.splitlines():
		line = line.strip()
		if line.startswith("ATLAS_BUILD_") and "=" in line:
			key, _, value = line.partition("=")
			inputs[key[len("ATLAS_BUILD_") :].lower()] = value.strip()
	if inputs:
		build.db_set("build_inputs", frappe.as_json(inputs))


def _register(recipe, snapshot_name: str) -> None:
	"""Wire the produced snapshot into the Atlas Settings field the recipe names
	(bench → default_bench_snapshot), so a self-serve site clones from this fresh
	golden without an operator hand-wiring it. Replaces the manual step the e2e
	bake left to the operator."""
	settings = frappe.get_single("Atlas Settings")
	settings.db_set(recipe.registers_as, snapshot_name)
	# nosemgrep: frappe-manual-commit -- background job: persist the newly-registered golden snapshot pointer in Atlas Settings so self-serve clones find it
	frappe.db.commit()


def _terminate_build_vm(vm_name: str) -> None:
	"""Terminate the scratch build VM. The snapshot is durable and outlives it
	(spec/14), so this is a clean teardown, not data loss."""
	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status != "Terminated":
		vm.terminate()
