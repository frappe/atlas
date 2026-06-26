"""Site DocType — the user-facing "my Frappe site at acme.blr1.frappe.dev".

A `Site` is the user-owned aggregate that ties together the one routing identity
(Contract A), the backing Virtual Machine it clones from the golden bench
snapshot, and the readiness state (Contract B). It is NOT the `Subdomain` (the
proxy map, which it creates once it is serving) and NOT the `Virtual Machine`
(which it owns/creates). See spec/14-self-serve.md.

The lifecycle mirrors `Virtual Machine`: an `IMMUTABLE_AFTER_INSERT` tuple guarded
in `validate()`, a controller-written `status` Select (read-only on the form),
`after_insert()` enqueues the provision→deploy background job, and whitelisted
methods that `frappe.throw` early on the wrong state.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.placement import (
	active_root_domain,
	default_bench_snapshot,
	warm_bench_snapshot_for_server,
)
from atlas.atlas.sizes import SIZE_PRESETS
from atlas.atlas.subdomain_label import (
	RESERVED_SUBDOMAINS,
	validate_label,
	validate_reserved,
)

# The size every self-serve backing VM is cloned at, from sizes.py (one source of
# truth for the ladder). A Site is provisioned at a fixed tier today; when paid
# plans land this becomes a per-Site field.
#
# "Shared 4x" (2 GB / 0.25 core), NOT the "Shared 1x" entry tier: the golden clone
# auto-starts a FULL bench (MariaDB + Redis + gunicorn + workers, baked to run on
# boot), which thrashes into swap at 512 MB and gets so little CPU under the 1/16
# -core cap that even sshd can't complete a banner exchange — so `deploy-site`'s
# wait_for_ssh times out and the Site never deploys (proven on a real clone,
# 2026-06-11; ~2 GB/site is the working budget, memory: rename-default-site). 2 GB
# matches the bake VM (bench_image GOLDEN_MEMORY_MB) — the size the bench was built
# and proven on.
SITE_VM_SIZE = SIZE_PRESETS["Shared 4x"]

# The Administrator password the golden image bakes into the site — a SHARED
# throwaway, the same on every clone. The deploy path no longer resets it per VM
# (that reset cost a ~28s CPU-throttled `bench frappe` boot that dominated the
# deploy); instead the tenant is handed this and rotates it after first login.
# MUST stay in lockstep with bench/build.sh BAKED_ADMIN_PASSWORD ("$BENCH_NAME
# -baked", bench name "atlas") and warm.sh's pre-warm login — a drift would hand
# the owner a password that does not authenticate.
BAKED_ADMIN_PASSWORD = "atlas-baked"

# The routing key (subdomain) and the backing VM are the identity; once written
# they are fixed. Repointing a live Site at a different VM is a delete-and-recreate,
# not an in-place edit — same shape as Subdomain.
IMMUTABLE_AFTER_INSERT = (
	"subdomain",
	"virtual_machine",
)

# Contract-A label rules (the single dotless DNS label, the reserved denylist)
# live in atlas.atlas.subdomain_label so `Site` and `Site Request` enforce the
# SAME rules — see that module. RESERVED_SUBDOMAINS is re-exported above for the
# callers/spec that reference `site.RESERVED_SUBDOMAINS`.


class Site(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		deploying_started: DF.Datetime | None
		provisioning_started: DF.Datetime | None
		running_started: DF.Datetime | None
		status: DF.Literal["Pending", "Provisioning", "Deploying", "Running", "Failed", "Terminated"]
		subdomain: DF.Data
		subdomain_doc: DF.Link | None
		virtual_machine: DF.Link | None
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Fill what the user didn't pick and gate the routing string.

		Validate the label *here* — before autoname() resolves the domain and
		builds the FQDN — so a bad label fails with a clear "single label"/
		"reserved" message rather than a downstream domain-resolution error.
		Then start Pending. The backing VM is created in the background job
		(after_insert), not here — provisioning SSHes and must not block the insert.
		The owning `tenant` (set by the Central-facing create_site API) carries
		attribution; Atlas no longer owns end-users."""
		self._validate_label()
		self._validate_reserved()
		if not self.status:
			self.status = "Pending"

	def autoname(self) -> None:
		# The FQDN is the doctype key (Contract A): <subdomain>.<region domain>,
		# built once here from the user's label + the resolved region domain, and
		# never transformed afterward. autoname() is the single place name is set.
		self._set_name_from_routing()

	def validate(self) -> None:
		self._validate_immutability()

	def after_insert(self) -> None:
		"""Auto-provision: enqueue the provision→deploy job so the user never has
		to click anything after create_site. queue=long because it SSHes (clone-boot-
		deploy-probe). Mirrors VirtualMachine.after_insert.

		enqueue_after_commit so the worker only starts once this insert's
		transaction has committed — otherwise auto_provision can look up the Site
		before the row exists ("Site ... not found")."""
		frappe.enqueue(
			"atlas.atlas.doctype.site.site.auto_provision",
			queue="long",
			timeout=1800,
			enqueue_after_commit=True,
			site_name=self.name,
		)

	# ----- validation -----------------------------------------------------

	def _validate_label(self) -> None:
		"""Single dotless DNS label — the shared Contract-A rule (subdomain_label),
		so `Site` and `Site Request` reject the exact same labels."""
		validate_label(self.subdomain)

	def _validate_reserved(self) -> None:
		validate_reserved(self.subdomain)

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(original, field) != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	# ----- routing identity (Contract A) ---------------------------------

	def _set_name_from_routing(self) -> None:
		"""Build the one routing string: `<subdomain>.<region domain>`, the FQDN
		that is simultaneously the proxy Host header and this row's key. (It is NOT
		the on-disk site name — a single-tenant VM keeps the baked `site.local` on
		disk and serves it for the FQDN Host; the FQDN identity lives at the proxy/Host
		layer.) Never transformed afterward. Uniqueness throws a clean "taken" message
		(this is the create_site race), not a raw duplicate-key error."""
		label = (self.subdomain or "").strip()
		if not label:
			frappe.throw(_("A subdomain is required"))
		domain = active_root_domain().domain
		fqdn = f"{label}.{domain}"
		if frappe.db.exists("Site", fqdn):
			frappe.throw(f"Subdomain '{label}' is already taken — choose another")
		self.name = fqdn

	# ----- lifecycle methods (Contract B state machine) ------------------

	@frappe.whitelist()
	def terminate(self) -> None:
		"""Take the site off the front door and tear down its backing VM.

		Delete the Subdomain (the proxy stops routing on the next reconcile),
		terminate the backing VM, then mark Terminated. Mirrors
		VirtualMachine.terminate()'s cleanup-then-mark shape. Idempotent-ish: a
		second call on an already-Terminated row throws."""
		if self.status == "Terminated":
			frappe.throw(_("Site is already terminated"))
		self._delete_subdomain()
		self._terminate_backing_vm()
		self.status = "Terminated"
		self.save(ignore_permissions=True)

	def _delete_subdomain(self) -> None:
		"""Drop the proxy map row (its on_trash reconciles the fleet). No-op when
		the site never began serving (no Subdomain was created).

		Clear `subdomain_doc` first: while the Site's Link field still references
		the Subdomain, Frappe's link-integrity guard refuses the delete
		(LinkExistsError). The guard queries the DB, so the null must be persisted
		(db_set), not just set in-memory, before the delete. Same clear-then-remove
		order terminate() uses for the VM."""
		subdomain = self.subdomain_doc
		if not subdomain:
			return
		self.db_set("subdomain_doc", None)
		if frappe.db.exists("Subdomain", subdomain):
			frappe.delete_doc("Subdomain", subdomain, ignore_permissions=True)

	def _terminate_backing_vm(self) -> None:
		"""Terminate the backing VM if one was created and is not already gone."""
		if not self.virtual_machine or not frappe.db.exists("Virtual Machine", self.virtual_machine):
			return
		vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		if vm.status != "Terminated":
			vm.terminate()


class _ProvisionClock:
	"""Live, per-stage wall-clock trace for `auto_provision`, printed to stdout
	(the RQ job log + the `worker` Procfile pane). Each `stage()` call closes out
	the previous stage — printing how long it took — and announces the next, so the
	operator watching the log sees both "what's happening now" and "that step took
	N s". `done()`/`failed()` close the final stage and print the grand total.

	Print, not `frappe.logger`: this is a follow-along trace meant to land in the
	same stream as the worker's other job lines, with `flush=True` so a long stage
	(the deploy) shows its start line immediately, not buffered until it returns."""

	def __init__(self, site_name: str) -> None:
		import time

		self._now = time.monotonic
		self._site = site_name
		self._t0 = self._now()
		self._stage_started = self._t0
		self._current = None

	def _emit(self, message: str) -> None:
		elapsed = self._now() - self._t0
		# nosemgrep: frappe-print-function-in-doctypes -- intentional: _ProvisionClock prints its follow-along trace to stdout so it lands in the RQ worker job log alongside the other job lines
		print(f"[auto_provision {self._site}] +{elapsed:6.1f}s {message}", flush=True)

	def stage(self, label: str) -> None:
		if self._current is not None:
			took = self._now() - self._stage_started
			self._emit(f"✓ {self._current} ({took:.1f}s)")
		self._current = label
		self._stage_started = self._now()
		self._emit(f"→ {label} …")

	def done(self) -> None:
		if self._current is not None:
			took = self._now() - self._stage_started
			self._emit(f"✓ {self._current} ({took:.1f}s)")
		self._emit(f"DONE — site Running in {self._now() - self._t0:.1f}s total")

	def failed(self) -> None:
		if self._current is not None:
			took = self._now() - self._stage_started
			self._emit(f"✗ FAILED during '{self._current}' after {took:.1f}s")
		self._emit(f"FAILED after {self._now() - self._t0:.1f}s total")


def auto_provision(site_name: str) -> None:
	"""Background-job entrypoint (enqueued by after_insert). Drives the whole
	create_site→live-site flow for one Site:

	  1. clone the backing VM from the golden bench snapshot and provision it,
	  2. wait for the VM to boot (SSH up),
	  3. run deploy-site.py in the guest,
	  4. wait for an HTTP 200 from the guest :80 — the readiness gate (Contract B),
	  5. create the Subdomain row (this is what makes the proxy route it),
	  6. mark Running.

	On any failure the Site is marked Failed (fail loud) and the exception is
	re-raised so the Task/job log carries it. No-op if the Site has moved past
	Pending (operator intervened, a manual retry raced us). Steps 3-4 are plan
	03's contract — this owns the orchestration, 03 owns the script + probe.

	Every status transition is committed (`_set_status`) so Central sees progress
	cross-transaction (the `site.status_changed` event + the `get_site` poll)."""
	site = frappe.get_doc("Site", site_name)
	if site.status != "Pending":
		return
	# Developer-mode short-circuit: a laptop has no Firecracker/KVM host, so the
	# real clone→boot→deploy→HTTP chain can't complete (the Fake provider's VMs
	# carry documentation IPs that never serve). In developer_mode we skip straight
	# to the tenant handoff — stamp the shared baked Administrator password and mark
	# the site Running — so the Central self-serve flow is testable end-to-end
	# locally. The same `_set_status` path the real flow uses, so Central's
	# `site.status_changed` event + `get_site` poll see Running identically. Never
	# fires in production (developer_mode off).
	if frappe.conf.developer_mode:
		site.db_set("admin_password", BAKED_ADMIN_PASSWORD)
		_set_status(site, "Running")
		return
	# Per-stage wall-clock trace, printed to the job log (and the bench `worker`
	# console) so the whole provision can be followed live and the slow stage
	# pinpointed — `_stage` logs the prior stage's duration as the next begins.
	# tail -f sites/<bench>/logs/worker.log (or the `worker` Procfile pane).
	clock = _ProvisionClock(site_name)
	try:
		_set_status(site, "Provisioning")
		clock.stage("clone backing VM")
		vm_name = _provision_backing_vm(site)
		site.db_set("virtual_machine", vm_name)
		# COMMIT before waiting. The clone's own after_insert enqueued its
		# provision (boot) job; that job is a SEPARATE transaction and cannot run
		# until this one commits. If we held the transaction open and blocked here,
		# the boot would never happen, the wait would time out, and the rollback
		# would delete the clone row — leaving its orphaned boot job to die
		# "Virtual Machine <uuid> not found". So: persist the clone + the status,
		# release the transaction, then wait on the boot job's COMMITTED progress.
		frappe.db.commit()
		clock.stage(f"wait for VM {vm_name} to boot (Running)")
		_wait_for_vm_running(vm_name)
		_set_status(site, "Deploying")
		clock.stage("deploy site in guest (wait_for_ssh + run deploy-site.py)")
		_deploy_site(site, vm_name)
		# The Administrator password handed to the tenant is the SHARED baked
		# throwaway (BAKED_ADMIN_PASSWORD) — the deploy no longer resets it per VM
		# (that cost a full CPU-throttled `bench frappe` boot, ~28s under the
		# 0.25-core cap, which dominated the deploy). The tenant rotates it after
		# first login. Stored encrypted on the Site BEFORE the readiness wait so the
		# handoff (the site.status_changed event + get_site poll) survives even if
		# the http gate later times out. db_set on a Password field round-trips
		# through Frappe's field encryption.
		site.db_set("admin_password", BAKED_ADMIN_PASSWORD)
		clock.stage("wait for HTTP 200 from guest :80 (Contract B)")
		_wait_for_http(site, vm_name)
		clock.stage("create Subdomain (proxy route)")
		subdomain_name = _create_subdomain(site, vm_name)
		site.db_set("subdomain_doc", subdomain_name)
		_set_status(site, "Running")
		clock.done()
	except Exception:
		# Fail loud: mark the row so the operator (and Central, via the event) sees
		# the failure, and re-raise so the job log carries the traceback. COMMIT the Failed status
		# before re-raising — otherwise the job's rollback reverts it back to
		# Pending (and a stuck Pending is indistinguishable from "never ran").
		clock.failed()
		_set_status(site, "Failed")
		raise


# The Site field that records when each real phase began. Drives per-phase timing
# Central can surface. Pending's start is the row's creation; the terminal
# Running/Failed states reuse the last stamp.
_TIMING_FIELD = {
	"Provisioning": "provisioning_started",
	"Deploying": "deploying_started",
	"Running": "running_started",
}


def _set_status(site, status: str) -> None:
	"""Persist a Site status transition and report it to Central.

	Two things, in order: db_set the status (stamping the phase entry time), then
	COMMIT it. The commit on every transition makes the state visible
	cross-transaction so Central's `get_site` poll sees the new value, and — on
	Failed — survives the job's rollback (a stuck Pending is indistinguishable
	from 'never ran'). The push half is the `site.status_changed` event the Site's
	on_update doc_event emits (atlas/atlas/central_report.py); committing here
	(enqueue_after_commit) is what lets that delivery fire."""
	# Stamp the entry time of this phase (drives the per-phase timing Central can
	# surface). Only the three real phases carry a field; Pending's start is the
	# row's creation, and Failed keeps the last good stamp (the in-flight phase
	# shows elapsed-until-failure). _TIMING_FIELD maps a status to its field.
	stamp_field = _TIMING_FIELD.get(status)
	if stamp_field:
		site.db_set(stamp_field, frappe.utils.now_datetime())
	site.db_set("status", status)
	# nosemgrep: frappe-manual-commit -- background job: commit each status transition so Central's poll sees it cross-transaction, the status_changed event delivers, and progress survives a crash mid-provision
	frappe.db.commit()


def _provision_backing_vm(site) -> str:
	"""Clone a fresh VM from the golden bench snapshot and return its name.

	The snapshot already carries the preinstalled bench + the grown disk;
	clone_to_new_vm re-derives a fresh identity (UUID, IPv6, MAC, host keys) and
	its own after_insert auto-provisions the VM. The guest is reached over the
	fleet SSH key (Atlas Settings) by the control plane, not the tenant — the
	tenant reaches the site over HTTPS through the proxy.

	WARM-FIRST: the server choice still follows the cold golden's row (today's
	placement), but when that server has an Available WARM golden the clone
	RESUMES it — a pre-warmed, already-serving guest in low seconds instead of a
	~17s+ cold boot. Warm is strictly an accelerator: no warm row (or a host that
	drifted — vm-restore.py's signature guard) degrades to exactly today's cold
	path. A warm clone restores at the CAPTURED vcpus/memory (the frozen vmstate
	pins them — clone_to_new_vm rejects overrides), so only the cgroup CPU cap
	comes from the tier; the cold path passes the explicit tier size as before.

	We pass an EXPLICIT size (`SITE_VM_SIZE`, see its note) rather than letting the
	clone inherit the build VM's resources: a self-serve site wants the standard
	tier, not whatever the bake VM happened to be — and, decisively, the build VM
	is scratch that gets terminated and its row deleted, so inheriting from it
	would fail once the golden has outlived its source (its whole point)."""
	snapshot = frappe.get_doc("Virtual Machine Snapshot", default_bench_snapshot())
	ssh_public_key = frappe.db.get_single_value("Atlas Settings", "ssh_public_key")
	size = SITE_VM_SIZE
	warm_name = warm_bench_snapshot_for_server(snapshot.server)
	if warm_name and warm_name != snapshot.name:
		snapshot = frappe.get_doc("Virtual Machine Snapshot", warm_name)
	if snapshot.kind == "Warm":
		return snapshot.clone_to_new_vm(
			title=site.name,
			ssh_public_key=ssh_public_key,
			cpu_max_cores=size["cpu_max_cores"],
		)
	return snapshot.clone_to_new_vm(
		title=site.name,
		ssh_public_key=ssh_public_key,
		vcpus=size["vcpus"],
		cpu_max_cores=size["cpu_max_cores"],
		memory_megabytes=size["memory_megabytes"],
		# Disk is not passed: the golden's rootfs is already grown to its own
		# disk_gigabytes and the clone can't shrink below it. The snapshot's size
		# is the floor; the entry tier's nominal disk would only ever be smaller.
	)


def _wait_for_vm_running(
	vm_name: str,
	timeout_seconds: int = 1500,
	initial_poll_seconds: float = 0.25,
	max_poll_seconds: float = 2.0,
) -> None:
	"""Block until the clone's own after_insert provision job flips it to Running.

	The clone boots in a SEPARATE background job (its after_insert enqueue); we
	committed before calling this so that job can run. Poll its COMMITTED status
	with rollback() (the boot job commits the Running flip in its own txn). On
	Running the guest's SSH + microVM are up, so the deploy step that follows can
	reach it. Raises on Failed (the boot job marked it) or on the deadline (the
	worker never ran the boot job). Mirrors the e2e `_tasks.wait_for_vm_running`
	shape — the proven contract for waiting on after_insert auto-provision.

	The poll starts tight (0.25s) and backs off geometrically to a 2s ceiling. A
	warm clone reaches Running in low seconds, so the old flat 5s poll added up to
	~5s of dead time waiting on a VM that was already up — pure granularity, on a
	path the user is actively watching. The tight initial poll shaves that off
	without busy-waiting on the rare slow cold boot (it settles to 2s)."""
	import time

	deadline = time.monotonic() + timeout_seconds
	poll = initial_poll_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Virtual Machine", vm_name, "status")
		if status == "Running":
			return
		if status == "Failed":
			frappe.throw(f"Backing VM {vm_name} reached Failed during provision")
		time.sleep(poll)
		poll = min(poll * 1.5, max_poll_seconds)
	frappe.throw(f"Backing VM {vm_name} did not reach Running within {timeout_seconds}s")


def _deploy_site(site, vm_name: str) -> None:
	"""Run deploy-site.py in the guest: rename the baked `site.local` dir to the FQDN
	(Contract A), regenerate the bench's nginx vhost (`server_name <fqdn>` + a v6
	listener) and reload — no `set-admin-password`, no `setup production`, no restart
	(the multitenant gunicorn resolves the site by Host header per request, so the
	rename + reload serve it live). Confirms it answers on :80 before returning.

	Seam for the in-guest deploy script + its guest-SSH driver."""
	from atlas.atlas.deploy_site import deploy_site

	deploy_site(vm_name, site.name)


def _wait_for_http(site, vm_name: str) -> None:
	"""Block until the guest answers HTTP 200 on :80 — the readiness gate
	(Contract B). Frappe is actually serving, not merely the VM booted.

	Seam for the `wait_for_http` probe over the VM's public /128. Passes
	the site FQDN as the Host header (Contract A) so the bench's multitenant nginx
	routes the probe to THIS site, not just any site on the VM. The readiness PATH is
	mode-aware: `/api/method/ping` for a site-mode clone, `/api/status` for an
	admin-mode clone (the admin console is a Flask app with no Frappe ping route) —
	resolved from the clone's `build_mode`."""
	from atlas.atlas.deploy_site import readiness_path_for_mode, wait_for_http

	vm = frappe.get_doc("Virtual Machine", vm_name)
	wait_for_http(vm.ipv6_address, site.name, path=readiness_path_for_mode(vm.build_mode))


def _create_subdomain(site, vm_name: str) -> str:
	"""Create the proxy-map Subdomain row that puts the site on the front door.

	The Subdomain's own after_insert reconciles the proxy fleet. The subdomain label
	and target VM both flow straight from the Site — no transformation (Contract A)."""
	subdomain = frappe.get_doc(
		{
			"doctype": "Subdomain",
			"subdomain": site.subdomain,
			"virtual_machine": vm_name,
			"active": 1,
		}
	).insert(ignore_permissions=True)
	return subdomain.name
