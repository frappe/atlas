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
from frappe.model.document import Document

from atlas.atlas.image_builder import run_build
from atlas.atlas.image_recipes import get_recipe
from atlas.atlas.placement import default_image

# The routing identity of a build: what to bake, where, and on which base. Once
# written they are fixed — re-baking with a different recipe/server/base is a new
# row, not an in-place edit (same shape as Site's IMMUTABLE_AFTER_INSERT).
IMMUTABLE_AFTER_INSERT = (
	"recipe",
	"server",
	"region",
	"base_image",
)


class ImageBuild(Document):
	def before_insert(self) -> None:
		"""Resolve the recipe and fill what the operator didn't pick.

		Copy the recipe's human title for the list view, default the base image
		from Atlas Settings, require a region for a proxy recipe (and default the
		auto-register check for a recipe that supports it), and start Draft. The
		build VM is created in the background job (after_insert), not here —
		provisioning SSHes and must not block the insert."""
		recipe = get_recipe(self.recipe)
		self.title = recipe.title
		if not self.base_image:
			self.base_image = default_image()
		if recipe.is_proxy and not self.region:
			frappe.throw(f"A region is required to build the {recipe.title}")
		if not self.status:
			self.status = "Draft"

	def validate(self) -> None:
		self._validate_immutability()

	def after_insert(self) -> None:
		"""Enqueue the bake. queue=long because it SSHes and waits on a multi-minute
		in-guest build; mirrors Site.after_insert / VirtualMachine.after_insert."""
		frappe.enqueue(
			"atlas.atlas.doctype.image_build.image_build.run",
			queue="long",
			timeout=3600,
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
		frappe.db.commit()
		self.after_insert()


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
		# (on_task fires before run_build throws).
		run_build(vm_name, recipe, on_task=lambda task_name: build.db_set("build_task", task_name))
		_set_status(build, "Snapshotting")
		snapshot_name = _stop_and_snapshot(build, recipe, vm_name)
		build.db_set("snapshot", snapshot_name)
		if build.auto_register and recipe.registers_as:
			_register(recipe, snapshot_name)
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
	frappe.db.commit()
	frappe.publish_realtime(
		event="image_build_progress",
		message={"name": build.name, "status": status},
		doctype="Image Build",
		docname=build.name,
	)


def _provision_build_vm(build, recipe) -> str:
	"""Insert a scratch Virtual Machine at the recipe's size and return its name.

	Its own after_insert auto-provisions it (boots it) in a separate job — we wait
	on that in _wait_for_vm_running after committing. A proxy build's VM carries
	is_proxy + region so its build (and the recipe's finalize) can read them; a
	bench build's VM is a plain VM. The fleet SSH key is baked into every VM the
	standard way, so build_proxy/build_bench reach the guest with it."""
	ssh_public_key = frappe.db.get_single_value("Atlas Settings", "ssh_public_key")
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"{recipe.title} — build",
			"server": build.server,
			"image": build.base_image,
			"vcpus": recipe.vcpus,
			"memory_megabytes": recipe.memory_megabytes,
			"disk_gigabytes": recipe.disk_gigabytes,
			"ssh_public_key": ssh_public_key,
			"is_proxy": 1 if recipe.is_proxy else 0,
			"region": build.region if recipe.is_proxy else None,
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


def _stop_and_snapshot(build, recipe, vm_name: str) -> str:
	"""Stop the build VM and snapshot it. A Stopped VM gives a clean unmount → a
	flush-consistent ext4 (Virtual Machine.snapshot's safe default), and the
	snapshot is the rollable artifact. Returns the snapshot name."""
	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status != "Stopped":
		vm.stop()
		vm.reload()
	return vm.snapshot(title=recipe.snapshot_title)


def _register(recipe, snapshot_name: str) -> None:
	"""Wire the produced snapshot into the Atlas Settings field the recipe names
	(bench → default_bench_snapshot), so a self-serve site clones from this fresh
	golden without an operator hand-wiring it. Replaces the manual step the e2e
	bake left to the operator."""
	settings = frappe.get_single("Atlas Settings")
	settings.db_set(recipe.registers_as, snapshot_name)
	frappe.db.commit()


def _terminate_build_vm(vm_name: str) -> None:
	"""Terminate the scratch build VM. The snapshot is durable and outlives it
	(spec/14), so this is a clean teardown, not data loss."""
	vm = frappe.get_doc("Virtual Machine", vm_name)
	if vm.status != "Terminated":
		vm.terminate()
