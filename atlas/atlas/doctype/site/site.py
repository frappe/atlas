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

# How long the handed-off login_url stays good: `deploy-site.py` mints it via
# `bench browse --sid`, a real 24h Administrator session, so the URL stops working
# 24h after mint. Atlas stamps `login_url_expires_at` = mint time + this so Central
# can compare against the expiry and regenerate a fresh one for a late click —
# same contract as Virtual Machine's LOGIN_URL_TTL_MINUTES["site"].
LOGIN_URL_TTL_MINUTES = 24 * 60

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

		deploying_started: DF.Datetime | None
		login_url: DF.SmallText | None
		login_url_expires_at: DF.Datetime | None
		pilot: DF.Link | None
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
			# The pilot credential is bench-level; it rides the job (never the Site row) to
			# the backing VM + the bench's bench.toml. Flags are set by create_site.
			pilot_credential_id=self.flags.get("pilot_credential_id"),
			central_endpoint=self.flags.get("central_endpoint"),
			bootstrap_token=self.flags.get("bootstrap_token"),
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
		self._terminate_pilot()
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

	def _terminate_pilot(self) -> None:
		"""Terminate the attached Pilot admin console before the VM (if one was stood up).

		The Pilot is ATTACHED — its own terminate() drops its Subdomain and marks itself
		Terminated but does NOT touch the VM (the Site owns it, torn down next). So this
		is safe to call before `_terminate_backing_vm`: no double-terminate. No-op when
		the site never got a Pilot (failed before the console stage) or it is already gone."""
		if not self.pilot or not frappe.db.exists("Pilot", self.pilot):
			return
		pilot = frappe.get_doc("Pilot", self.pilot)
		if pilot.status != "Terminated":
			pilot.terminate()

	def _terminate_backing_vm(self) -> None:
		"""Terminate the backing VM if one was created and is not already gone."""
		if not self.virtual_machine or not frappe.db.exists("Virtual Machine", self.virtual_machine):
			return
		vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		if vm.status != "Terminated":
			vm.terminate()

	@frappe.whitelist()
	def regenerate_login_url(self) -> dict:
		"""Re-mint the one-click login URL for a serving site and return the fresh
		handoff. Central calls this (operator token) when a tenant clicks their login
		link after the current URL's 24h `bench browse` session has expired — the URL
		is short-lived by design, so it is re-signed on demand rather than kept alive.

		Only a Running site can be regenerated (before that there is no served site to
		sign into, and the field may be unstamped). Re-mint in the guest via the deploy
		seam, stamp `login_url` + `login_url_expires_at` = now + the session TTL (same as
		the original deploy), COMMIT so the `get_site` poll sees it, and return the
		mirror shape Central re-reads. A Fake-backed VM never answers SSH, so its login
		URL is synthesized here exactly as the deploy synthesizes it — desk/e2e stay
		green without a host."""
		if self.status != "Running":
			frappe.throw(_("Only a running site's login URL can be regenerated"))
		result = _regenerate_login(self, self.virtual_machine)
		self.db_set("login_url", (result or {}).get("login_url", ""))
		self.db_set(
			"login_url_expires_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=LOGIN_URL_TTL_MINUTES),
		)
		# nosemgrep: frappe-manual-commit -- persist the fresh URL so Central's get_site poll sees it cross-transaction (the mint has no status change, so no event fires)
		frappe.db.commit()
		from atlas.atlas.api.site import _mirror

		return _mirror(self)


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


def auto_provision(
	site_name: str,
	pilot_credential_id: str | None = None,
	central_endpoint: str | None = None,
	bootstrap_token: str | None = None,
) -> None:
	"""Background-job entrypoint (enqueued by after_insert). Drives the whole
	create_site→live-site flow for one Site:

	  1. clone the backing VM from the golden bench snapshot and provision it,
	  2. wait for the VM to boot (SSH up),
	  3. create the Subdomain row (this is what makes the proxy route it) — done
	     BEFORE the deploy so the fleet reconcile runs while the guest deploys,
	  4. run deploy-site.py in the guest,
	  5. wait for an HTTP 200 from the guest :80 — the readiness gate (Contract B),
	  6. mark Running.

	The Subdomain is created right after the VM is Running (step 3), not after the
	readiness gate: the proxy route needs only the FQDN + the VM's /128, both present
	once the VM boots, and registering it up front lets the proxy catch up in parallel
	with the deploy so a tenant redirected the instant the Site flips Running doesn't
	beat the proxy sync.

	On any failure the Site is marked Failed (fail loud) and the exception is
	re-raised so the Task/job log carries it. No-op if the Site has moved past
	Pending (operator intervened, a manual retry raced us). Steps 4-5 are plan
	03's contract — this owns the orchestration, 03 owns the script + probe.

	Every status transition is committed (`_set_status`) so Central sees progress
	cross-transaction (the `site.status_changed` event + the `get_site` poll)."""
	site = frappe.get_doc("Site", site_name)
	if site.status != "Pending":
		return
	# A Fake-backed site (developer_mode laptop) runs this WHOLE flow for real —
	# clone the backing VM, wait for it to boot, create the Subdomain, mark Running —
	# so every record a production provision creates is created here too (the old
	# short-circuit stamped Running and skipped them, leaving an un-routable Site with
	# no VM and no Subdomain). Only the two stages that physically can't run against a
	# Fake VM's documentation IP — the SSH deploy and the HTTP readiness probe —
	# short-circuit, inside `_deploy_site`/`_wait_for_http`, the same `is_fake_server`
	# gate `run_task` uses. Never fires in production (no Fake servers there).
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
		# The pilot credential is the bench's: stamp its id on the backing VM so vm.* events
		# echo it to Central (central_report), which links/revokes the credential by it.
		if pilot_credential_id:
			frappe.db.set_value("Virtual Machine", vm_name, "pilot_credential_id", pilot_credential_id)
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
		# Register the proxy route AS SOON AS the VM has an address — before the long
		# deploy + HTTP-readiness waits, not after. The Subdomain's after_insert enqueues
		# the fleet reconcile (a separate `long` job), so the proxy learns the FQDN→/128
		# map while the guest deploy is still running. Otherwise the tenant, redirected to
		# the FQDN the moment the Site flips Running, races a proxy that hasn't been synced
		# yet. The route needs only the FQDN + the VM's /128 (`_denormalize_address`), both
		# present once the VM is Running; the deploy result is not a prerequisite.
		clock.stage("create Subdomain (proxy route)")
		subdomain_name = _create_subdomain(site, vm_name)
		site.db_set("subdomain_doc", subdomain_name)
		frappe.db.commit()
		_set_status(site, "Deploying")
		# Resolve the attached Pilot's console FQDN up front (deterministic from the
		# site subdomain, disambiguated on collision) and thread it into BOTH the
		# site-mode deploy — which writes it into `[admin].domain` so the admin vhost
		# is emitted in the rename-site pass — and `_provision_pilot`, which reuses the
		# SAME label so the two agree on the console name. See spec/14-self-serve.md.
		from atlas.atlas.placement import active_root_domain
		from atlas.atlas.subdomain_label import pilot_subdomain_for

		pilot_label = pilot_subdomain_for(site.subdomain)
		admin_domain = f"{pilot_label}.{active_root_domain().domain}"
		clock.stage("deploy site in guest (wait_for_ssh + run deploy-site.py)")
		result = _deploy_site(site, vm_name, central_endpoint, bootstrap_token, admin_domain)
		# The tenant handoff is the one-click login URL `deploy-site.py` minted
		# (`bench browse --sid`, a real 24h session) — NOT a password; the baked
		# Administrator password is a long random secret generated at bake time and
		# never surfaced. Stored BEFORE the readiness wait so the handoff (the
		# site.status_changed event + get_site poll) survives even if the http gate
		# later times out. Stamp `login_url_expires_at` = now + the session's 24h TTL
		# alongside, so Central regenerates a fresh URL for a late click (mirrors
		# Virtual Machine). A Fake-backed VM's `_deploy_site` short-circuits with a
		# synthesized result so this stays stamped in tests/e2e too.
		site.db_set("login_url", (result or {}).get("login_url", ""))
		site.db_set(
			"login_url_expires_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=LOGIN_URL_TTL_MINUTES),
		)
		clock.stage("wait for HTTP 200 from guest :80 (Contract B)")
		_wait_for_http(site, vm_name)
		# Stand up the bench admin console (a Pilot) on this SAME backing VM, fronted at
		# `<subdomain>-pilot.<region>` — the front door Central's Asset resolves for
		# "Open" (front_door_for_vm prefers Pilot). The customer's Frappe site is this
		# Site (get_site); the Pilot is the admin console on the same bench. Done AFTER
		# the site serves (the VM is up + the admin app is installed on every golden) and
		# BEFORE Running so a console-wiring failure fails the whole site loud. See
		# spec/14-self-serve.md.
		clock.stage("attach Pilot admin console (proxy route + admin login)")
		_provision_pilot(site, vm_name, pilot_label)
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
	# db_set runs on_change, NOT on_update, so the on_site_update doc_event never
	# fires for these transitions — emit the status_changed explicitly (same gap the
	# Pilot closes with report_pilot_status). Its delivery is enqueue_after_commit, so
	# it rides the commit just below. Without this the mirror only ever sees the initial
	# Pending (site.created + the insert's on_update) and, with no site reconcile pull,
	# stays stuck Pending forever.
	from atlas.atlas.central_report import report_site_status

	report_site_status(site)
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
			title=site.subdomain,
			ssh_public_key=ssh_public_key,
			cpu_max_cores=size["cpu_max_cores"],
			tenant=site.tenant,
		)
	return snapshot.clone_to_new_vm(
		title=site.subdomain,
		ssh_public_key=ssh_public_key,
		vcpus=size["vcpus"],
		cpu_max_cores=size["cpu_max_cores"],
		memory_megabytes=size["memory_megabytes"],
		tenant=site.tenant,
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


def _deploy_site(
	site,
	vm_name: str,
	central_endpoint: str | None = None,
	bootstrap_token: str | None = None,
	admin_domain: str | None = None,
) -> dict:
	"""Run deploy-site.py in the guest: rename the baked `site.local` dir to the FQDN
	(Contract A), regenerate the bench's nginx vhost (`server_name <fqdn>` + a v6
	listener) and reload — no `set-admin-password`, no `setup production`, no restart
	(the multitenant gunicorn resolves the site by Host header per request, so the
	rename + reload serve it live). Confirms it answers on :80, then mints the
	tenant's one-click login URL, before returning.

	Seam for the in-guest deploy script + its guest-SSH driver. A Fake-backed VM
	carries a documentation IP that never answers SSH, so the deploy is a no-op
	there — the same `is_fake_server` gate `run_task` uses (atlas.atlas._ssh.runner).
	The baked site already serves on the (synthetic) guest in the fiction the Fake
	provider maintains, so there is nothing to rename; a placeholder `login_url` is
	synthesized instead so the mirror shape stays stable for e2e/desk tests that
	run against a Fake server."""
	from atlas.atlas.deploy_site import deploy_site
	from atlas.atlas.providers.fake_tasks import is_fake_server

	vm = frappe.get_doc("Virtual Machine", vm_name)
	if is_fake_server(vm.server):
		return {"site": site.name, "serving": True, "login_url": f"https://{site.name}/app?sid=fake-sid"}
	return deploy_site(vm_name, site.name, central_endpoint, bootstrap_token, admin_domain=admin_domain) or {}


def _regenerate_login(site, vm_name: str) -> dict:
	"""Re-mint the site's one-click login URL in the guest and return the result.

	Seam for the regenerate driver + its Fake short-circuit — the sibling of
	`_deploy_site`, sharing its `is_fake_server` gate. On a real VM it runs
	deploy-site.py with `--regenerate-login` (re-sign only, no rename/setup). A
	Fake-backed VM's documentation /128 never answers SSH, so the same placeholder
	`login_url` is synthesized instead, keeping the mirror shape stable for the
	desk/e2e tests that run against a Fake server."""
	from atlas.atlas.deploy_site import regenerate_login
	from atlas.atlas.providers.fake_tasks import is_fake_server

	vm = frappe.get_doc("Virtual Machine", vm_name)
	if is_fake_server(vm.server):
		return {"site": site.name, "serving": True, "login_url": f"https://{site.name}/app?sid=fake-sid"}
	return regenerate_login(vm_name, site.name) or {}


def _wait_for_http(site, vm_name: str) -> None:
	"""Block until the guest answers HTTP 200 on :80 — the readiness gate
	(Contract B). Frappe is actually serving, not merely the VM booted.

	Seam for the `wait_for_http` probe over the VM's public /128. Passes
	the site FQDN as the Host header (Contract A) so the bench's multitenant nginx
	routes the probe to THIS site, not just any site on the VM. The readiness PATH is
	mode-aware: `/api/method/ping` for a site-mode clone, `/api/status` for an
	admin-mode clone (the admin console is a Flask app with no Frappe ping route) —
	resolved from the clone's `build_mode`.

	A Fake-backed VM's documentation /128 never answers, so the probe is skipped
	there (the same `is_fake_server` gate `_deploy_site` and `run_task` use) — the
	readiness gate is the deploy's twin, both no-ops on a Fake VM."""
	from atlas.atlas.deploy_site import readiness_path_for_mode, wait_for_http
	from atlas.atlas.providers.fake_tasks import is_fake_server

	vm = frappe.get_doc("Virtual Machine", vm_name)
	if is_fake_server(vm.server):
		return
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


def _provision_pilot(site, vm_name: str, pilot_label: str) -> str:
	"""Stand up the attached Pilot admin console on this site's backing VM and link it.

	Creates a `Pilot` at `<subdomain>-pilot.<region>` (the label resolved by the caller
	via `pilot_subdomain_for` and already written into the VM's `[admin].domain` by the
	site-mode deploy), ATTACHED to this site's VM (`flags.attach_vm` → the Pilot binds
	the VM instead of creating one, and won't tear it down). Its `after_insert` only
	links the VM; `deploy_attached` mints the admin login URL, creates the Pilot's own
	Subdomain (a second proxy route → the SAME VM /128), and marks the Pilot Running —
	the admin vhost itself was already emitted in the site deploy's rename-site pass. The
	Pilot is linked on the Site so terminate() cascades. Returns the Pilot name.

	This is the create_site half that makes the Asset's "Open" resolve a bench admin
	console (front_door_for_vm prefers Pilot) rather than the customer site — the bug
	this closes (spec/14-self-serve.md)."""
	from atlas.atlas.doctype.pilot.pilot import deploy_attached

	pilot = frappe.get_doc({"doctype": "Pilot", "subdomain": pilot_label, "tenant": site.tenant})
	pilot.flags.attach_vm = vm_name
	pilot.insert(ignore_permissions=True)
	site.db_set("pilot", pilot.name)
	deploy_attached(pilot.name)
	return pilot.name
