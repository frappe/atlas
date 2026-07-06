"""Pilot DocType — a bench-backed tenant environment fronted at a subdomain.

A `Pilot` is the bench analogue of `Site`: the tenant-owned aggregate that ties
together one routing identity (Contract A — the subdomain), the backing
`Virtual Machine` it boots from a bench image, and the one-click login handoff.
It exists so the *bench provision* (boot a bench image, wait for SSH, deploy in
admin mode) lives OFF the `Virtual Machine` DocType, which stays a pure microVM
lifecycle. See spec/14-self-serve.md.

Like a `Site`, a Pilot is NOT the `Subdomain` (the proxy map) — it CREATES one
once its backing VM has booted and deployed, so the proxy routes
`<subdomain>.<region domain>` → the backing VM's public /128. That Subdomain row
is linked back as `subdomain_doc` and deleted on teardown, exactly as `Site` does.

Central still talks to Atlas in VM terms — it calls `create_vm` and mirrors a VM
row. That API creates a Pilot under the hood and reads the bench handoff
(gateway_url, login_url, expiry) back THROUGH the Pilot, so the wire shape Central
sees is unchanged (atlas.atlas.api.provision).

The lifecycle mirrors `Site`: a controller-written `status`, `after_insert`
enqueues the provision→deploy background job, and whitelisted methods that
`frappe.throw` early on the wrong state. A Pilot OWNS its VM (creates it, tears it
down) — plain VM facts (ipv6, sizing) are read through the `virtual_machine` link,
never copied onto the Pilot row.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.placement import active_root_domain
from atlas.atlas.subdomain_label import validate_label, validate_reserved

# How long a freshly-minted login URL stays usable, keyed by build_mode — the TTL
# of the token deploy-site.py minted (bench/deploy-site.py). admin:
# `generate-admin-session`'s 5-minute single-use JWT; site: `bench browse`'s 24h
# session. Atlas stamps `login_url_expires_at` = mint time + this, and Central
# compares against it to decide "use it" vs "regenerate". Mirrors Site's
# LOGIN_URL_TTL_MINUTES (which is the "site" value alone — a Site is always site-mode).
LOGIN_URL_TTL_MINUTES = {"admin": 5, "site": 24 * 60}

# The subdomain and the backing VM are the identity; once written they are fixed.
IMMUTABLE_AFTER_INSERT = (
	"subdomain",
	"virtual_machine",
)


class Pilot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attached: DF.Check
		build_mode: DF.Data | None
		login_url: DF.SmallText | None
		login_url_expires_at: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Failed", "Terminated"]
		subdomain: DF.Data
		subdomain_doc: DF.Link | None
		tenant: DF.Link | None
		virtual_machine: DF.Link | None
	# end: auto-generated types

	# ----- derived routing identity (Contract A) -------------------------

	@property
	def bench_fqdn(self) -> str:
		"""The host this pilot is fronted at — `<subdomain>.<region domain>`, derived,
		never stored (Contract-A: region/domain stay in Atlas). This is the bare host
		the in-guest deploy targets; `gateway_url` is the https URL Central shows."""
		return f"{self.subdomain}.{active_root_domain().domain}"

	@property
	def gateway_url(self) -> str:
		"""The URL Central deep-links this pilot at — `https://<bench_fqdn>`. Mirrors
		Site's `url` (also `https://<host>`) so the console + billing views open it the
		same way."""
		return f"https://{self.bench_fqdn}"

	# ----- lifecycle -----------------------------------------------------

	def before_insert(self) -> None:
		"""Gate the routing label (the same Contract-A checks a Site runs on its
		subdomain, so a bad one fails at the boundary) and start Pending. The backing
		VM is created in the background job (after_insert), not here — provisioning
		SSHes and must not block the insert. The owning `tenant` (set by the
		Central-facing create_vm API) carries attribution; Atlas no longer owns
		end-users."""
		validate_label(self.subdomain)
		validate_reserved(self.subdomain)
		if not self.status:
			self.status = "Pending"

	def autoname(self) -> None:
		# The FQDN is the doctype key (Contract A): <subdomain>.<region domain>, built
		# once here from Central's label + the resolved region domain, and never
		# transformed afterward. autoname() is the single place name is set.
		label = (self.subdomain or "").strip()
		if not label:
			frappe.throw(_("A subdomain is required"))
		domain = active_root_domain().domain
		fqdn = f"{label}.{domain}"
		if frappe.db.exists("Pilot", fqdn):
			frappe.throw(f"Subdomain '{label}' is already taken — choose another")
		self.name = fqdn

	def validate(self) -> None:
		self._validate_immutability()

	def after_insert(self) -> None:
		"""Create the backing VM SYNCHRONOUSLY, then enqueue the boot→deploy job.

		The VM is created here, in the insert's transaction, so the Central-facing
		create_vm can read the VM's identity (name, ipv6) back through the pilot and
		return it in the mirror row Central upserts — the same immediate-identity
		contract create_vm had before Pilot existed. The VM's OWN after_insert then
		auto-provisions it (a plain boot to Running, no bench logic).

		The bench work — wait for the VM to boot, deploy in-guest, mint the login URL —
		runs in the background job (queue=long because it SSHes). enqueue_after_commit
		so the worker only starts once this insert's transaction has committed —
		otherwise auto_provision can look up the pilot (or its VM) before the rows
		exist.

		ATTACHED path (a self-serve Site's Pilot, `flags.attach_vm` set): this Pilot
		binds to a VM the Site already owns, so it neither creates a VM nor enqueues its
		own job. It only links the shared VM + marks itself attached; the Site's
		auto_provision drives the admin-console deploy on the already-booted VM
		(`admin`-mode wiring at the pilot FQDN — the Site owns the VM lifecycle)."""
		attach_vm = self.flags.get("attach_vm")
		if attach_vm:
			self.db_set("attached", 1)
			self.db_set("virtual_machine", attach_vm)
			# The attached Pilot serves the bench admin CONSOLE at its FQDN (the VM's own
			# build_mode is `site` — it serves the customer site at a different FQDN); its
			# login mint/TTL follows admin mode. See spec/14-self-serve.md.
			self.db_set("build_mode", "admin")
			return
		_provision_backing_vm(self)
		frappe.enqueue(
			"atlas.atlas.doctype.pilot.pilot.auto_provision",
			queue="long",
			timeout=1800,
			enqueue_after_commit=True,
			pilot_name=self.name,
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

	# ----- lifecycle methods --------------------------------------------

	@frappe.whitelist()
	def terminate(self) -> None:
		"""Take the pilot off the front door, tear down its backing VM, and mark
		Terminated. Delete the Subdomain (the proxy stops routing on the next reconcile),
		terminate the backing VM, then mark Terminated. Mirrors Site.terminate()'s
		cleanup-then-mark shape. Idempotent-ish: a second call on an already-Terminated
		row throws."""
		if self.status == "Terminated":
			frappe.throw(_("Pilot is already terminated"))
		self._delete_subdomain()
		self._terminate_backing_vm()
		self.status = "Terminated"
		self.save(ignore_permissions=True)

	def _delete_subdomain(self) -> None:
		"""Drop the proxy-map row (its on_trash reconciles the fleet). No-op when the
		pilot never began serving (no Subdomain was created).

		Clear `subdomain_doc` first: while the Pilot's Link field still references the
		Subdomain, Frappe's link-integrity guard refuses the delete (LinkExistsError).
		The guard queries the DB, so the null must be persisted (db_set), not just set
		in-memory, before the delete. Same clear-then-remove order Site.terminate() uses,
		and the order terminate() uses for the VM."""
		subdomain = self.subdomain_doc
		if not subdomain:
			return
		self.db_set("subdomain_doc", None)
		if frappe.db.exists("Subdomain", subdomain):
			frappe.delete_doc("Subdomain", subdomain, ignore_permissions=True)

	def _terminate_backing_vm(self) -> None:
		"""Terminate the backing VM if one was created and is not already gone.

		No-op when ATTACHED: an attached Pilot shares a VM the owning Site created and
		tears down — terminating it here would double-terminate (and race the Site's own
		VM teardown). The attached Pilot's teardown is only its Subdomain + its own row."""
		if self.attached:
			return
		if not self.virtual_machine or not frappe.db.exists("Virtual Machine", self.virtual_machine):
			return
		vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		if vm.status != "Terminated":
			vm.terminate()

	@frappe.whitelist()
	def regenerate_login_url(self) -> dict:
		"""Re-mint this pilot's one-click login URL and return the fresh handoff (the
		VM-shaped payload Central re-reads). Central calls this (operator token) when a
		tenant clicks Open after the current URL's short-lived token has expired — the
		admin JWT lasts 5 minutes, so a login is almost always a fresh mint, not the one
		stamped at boot.

		Only a Running pilot has a login URL to regenerate. Re-mint in the guest (admin
		mode → `generate-admin-session`, site mode → `browse`, following build_mode),
		stamp `login_url` + `login_url_expires_at` = now + the mode's TTL, COMMIT so
		Central's poll/reconcile sees it (the mint has no status change, so no event
		fires), and return the mirror shape."""
		if self.status != "Running":
			frappe.throw(f"Cannot regenerate a login URL from {self.status}")
		result = _regenerate_login(self)
		self._stamp_login(result)
		self.save(ignore_permissions=True)
		# nosemgrep: frappe-manual-commit -- persist the fresh URL so Central's poll/reconcile sees it cross-transaction (the mint has no status change, so no event fires)
		frappe.db.commit()
		from atlas.atlas.central_report import _pilot_vm_payload

		return _pilot_vm_payload(self)

	# ----- login-URL stamp (shared by mint + regenerate) -----------------

	def _stamp_login(self, result: dict) -> None:
		"""Stamp a minted login URL + its expiry on the doc (not committed) — the single
		place mint/regenerate share so the expiry is always mint time + the mode's TTL
		(LOGIN_URL_TTL_MINUTES: 5 min for admin's single-use JWT, 24h for a site
		session)."""
		mode = self.build_mode or "site"
		ttl = LOGIN_URL_TTL_MINUTES.get(mode, LOGIN_URL_TTL_MINUTES["site"])
		self.login_url = (result or {}).get("login_url", "")
		self.login_url_expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=ttl)


def auto_provision(pilot_name: str) -> None:
	"""Background-job entrypoint. Wait for the (already-created) backing VM to boot,
	mint the one-click login URL in the booted guest, create the Subdomain that puts
	the pilot on the front door, and THEN mark the pilot Running — the same
	wait→deploy→mint→route ordering Site.auto_provision uses, so the single Running
	event carries the handoff. No-op if the pilot has moved past Pending (operator
	intervened, a manual retry raced us). Fail loud so a pilot whose mint fails is
	Failed, not a silently login-less Running.

	The backing VM was created synchronously in `after_insert` (so create_vm could
	return its identity); it auto-provisions ITSELF (its own after_insert boot job) —
	a plain boot to Running, no bench logic. Here we wait for that Running, then deploy
	in-guest."""
	import time

	def _trace(message: str, since: float | None = None) -> None:
		"""Stage breadcrumb for the (long, opaque) bench provision. Goes to BOTH the
		job's stdout log AND frappe.logger — the logger writes synchronously, so the
		last breadcrumb survives even when the RQ death-penalty kills the worker
		mid-stage (buffered stdout is lost on that SIGALRM)."""
		suffix = f" ({time.monotonic() - since:.1f}s)" if since is not None else ""
		line = f"[pilot auto_provision {pilot_name}] {message}{suffix}"
		print(line, flush=True)
		frappe.logger("atlas").info(line)

	pilot = frappe.get_doc("Pilot", pilot_name)
	if pilot.status != "Pending":
		_trace(f"no-op: status is {pilot.status}, not Pending")
		return

	try:
		_trace(f"waiting for backing VM {pilot.virtual_machine} to boot (Running) …")
		_t = time.monotonic()
		_wait_for_vm_running(pilot.virtual_machine)
		_trace("VM Running; minting login URL (in-guest deploy) …", since=_t)
		_t = time.monotonic()
		result = _deploy(pilot)
		pilot._stamp_login(result)
		pilot.db_set("login_url", pilot.login_url)
		pilot.db_set("login_url_expires_at", pilot.login_url_expires_at)
		_trace("login URL minted; creating Subdomain (proxy route) …", since=_t)
		_t = time.monotonic()
		subdomain_name = _create_subdomain(pilot)
		pilot.db_set("subdomain_doc", subdomain_name)
		_trace("Subdomain created; marking Running …", since=_t)
		pilot.db_set("status", "Running")
		# db_set skips on_update, so the status event that carries the login handoff
		# won't fire on its own — emit it explicitly. Its delivery is enqueued
		# after_commit, so it rides the commit just below. Without this the mirror only
		# learns login_url on the next 10-min reconcile, and Open fails ("VM has no
		# login URL yet") until then.
		from atlas.atlas.central_report import report_pilot_status

		report_pilot_status(pilot)
		# nosemgrep: frappe-manual-commit -- commit the handoff + Running so the status event delivers (enqueue_after_commit) and the poll sees it
		frappe.db.commit()
		_trace("marked Running — pilot provision complete")
	except Exception:
		_trace("FAILED — flipping status to Failed")
		pilot.db_set("status", "Failed")
		# nosemgrep: frappe-manual-commit -- background job: commit Failed so it survives the job's rollback (a stuck Pending is indistinguishable from a mint that never ran)
		frappe.db.commit()
		raise


def deploy_attached(pilot_name: str) -> None:
	"""Wire the admin console for an ATTACHED Pilot on its (already-booted) shared VM.

	Called by `Site.auto_provision` AFTER the site is serving: the backing VM is up and
	the Site has done its own site-mode deploy, so this only does the pilot half —
	the admin-mode wiring at the pilot FQDN (a second nginx vhost on the same guest),
	mint the admin login URL, create the pilot's Subdomain (the second proxy route → the
	SAME VM /128), and mark the Pilot Running. It is the attached twin of `auto_provision`
	minus the VM-boot wait (the Site owns and already waited on the VM).

	Fail loud: a pilot whose admin wiring fails is Failed, not a silently console-less
	Running — the Site's job surfaces it (and the Site itself still serves)."""
	pilot = frappe.get_doc("Pilot", pilot_name)
	if pilot.status != "Pending":
		return
	try:
		result = _deploy(pilot)
		pilot._stamp_login(result)
		pilot.db_set("login_url", pilot.login_url)
		pilot.db_set("login_url_expires_at", pilot.login_url_expires_at)
		subdomain_name = _create_subdomain(pilot)
		pilot.db_set("subdomain_doc", subdomain_name)
		pilot.db_set("status", "Running")
		# db_set skips on_update, so emit the status event (carrying the login handoff)
		# explicitly — the same gap auto_provision closes. Its delivery is
		# enqueue_after_commit, so it rides the Site job's commit.
		from atlas.atlas.central_report import report_pilot_status

		report_pilot_status(pilot)
	except Exception:
		pilot.db_set("status", "Failed")
		# nosemgrep: frappe-manual-commit -- persist Failed so it survives a rollback (a stuck Pending is indistinguishable from a wiring that never ran)
		frappe.db.commit()
		raise


def _provision_backing_vm(pilot) -> str:
	"""Create the backing VM from a bench image and return its name.

	Unlike Site (which CLONES a golden snapshot), a Pilot boots a bench IMAGE
	directly — the same shape create_vm used before Pilot existed. The VM's own
	after_insert auto-provisions it (a plain boot to Running); the guest is reached
	over the fleet SSH key (Atlas Settings) by the control plane. `build_mode` is
	inherited from the image by the VM at insert; the pilot mirrors it onto its own
	row here so its login mint/TTL follows the mode without re-reading the VM."""
	from atlas.atlas.placement import default_image, default_server_for_image

	fleet_public_key = frappe.db.get_single_value("Atlas Settings", "ssh_public_key")
	if not fleet_public_key:
		frappe.throw("Atlas Settings.ssh_public_key is unset; cannot provision a VM the fleet can reach.")

	spec = pilot.flags.get("vm_spec") or {}
	doc = {
		"doctype": "Virtual Machine",
		"title": pilot.subdomain,
		"tenant": pilot.tenant,
		"vcpus": spec.get("vcpus", 1),
		"memory_megabytes": spec.get("memory_megabytes", 512),
		"disk_gigabytes": spec.get("disk_gigabytes", 2),
		"ssh_public_key": fleet_public_key,
	}
	# server/image are Atlas's placement concern. The image is the pinned bench image
	# (caller override, else the Atlas Settings default). The server must be one that
	# HOLDS that image: a baked bench image is often local (an LV on the box it was baked
	# on, plus wherever it was exported), so we pick from the image's home set rather than
	# the whole fleet — placement.default_server_for_image restricts to those hosts and
	# throws loudly if the image lives nowhere yet. A caller-pinned server still wins (the
	# operator vouches the image is there); a pinned-but-missing server is ignored.
	doc["image"] = spec["image"] if spec.get("image") else default_image()
	if spec.get("server") and frappe.db.exists("Server", spec["server"]):
		doc["server"] = spec["server"]
	else:
		doc["server"] = default_server_for_image(
			doc["image"],
			required_vcpus=float(spec.get("cpu_max_cores") or doc["vcpus"]),
			required_memory_mb=float(doc["memory_megabytes"]),
			required_disk_gb=float(doc["disk_gigabytes"]),
		)
	vm = frappe.get_doc(doc)
	if spec.get("cpu_max_cores"):
		vm.cpu_max_cores = float(spec["cpu_max_cores"])
	vm.insert(ignore_permissions=True)
	# Link the VM back onto the pilot and stamp the bench mode (the front door follows
	# the image's mode, so the mint/TTL logic reads it locally). db_set writes both the
	# DB and the in-memory doc, so create_vm — holding this same pilot — reads the VM's
	# identity straight back for its mirror row.
	pilot.db_set("virtual_machine", vm.name)
	pilot.db_set("build_mode", vm.build_mode or "site")
	return vm.name


def _wait_for_vm_running(vm_name: str) -> None:
	"""Block until the backing VM's own boot job flips it to Running. Reuses Site's
	proven wait (poll the committed status with rollback, raise on Failed/deadline)."""
	from atlas.atlas.doctype.site.site import _wait_for_vm_running as _wait

	_wait(vm_name)


def _deploy(pilot) -> dict:
	"""Run the in-guest deploy for the booted backing VM and return the parsed result
	(carries `login_url`). Points the FQDN at the admin console (admin mode) or the
	baked site (site mode) and mints the mode's login URL — the same script and result
	shape Site consumes.

	A Fake-backed VM's documentation IP never answers SSH, so `deploy_site` is a no-op
	there; synthesize a placeholder so desk/e2e stay green without a host."""
	from atlas.atlas.deploy_site import deploy_site
	from atlas.atlas.providers.fake_tasks import is_fake_server

	vm = frappe.get_doc("Virtual Machine", pilot.virtual_machine)
	if is_fake_server(vm.server):
		return {"login_url": f"https://{pilot.bench_fqdn}/app?sid=fake-sid"}
	# An attached Pilot's build_mode is `admin` (it serves the console at its FQDN) while
	# the shared VM's build_mode is `site`; pass mode explicitly so the deploy wires the
	# admin vhost, not another site rename. A stand-alone Pilot passes None → the VM's mode.
	mode = pilot.build_mode if pilot.attached else None
	return deploy_site(pilot.virtual_machine, pilot.bench_fqdn, mode=mode) or {}


def _create_subdomain(pilot) -> str:
	"""Create the proxy-map Subdomain row that puts the pilot on the front door.

	The Subdomain's own after_insert reconciles the proxy fleet, which then routes
	`<subdomain>.<region domain>` → the backing VM's public /128. The subdomain label
	and target VM both flow straight from the Pilot — no transformation (Contract A).
	Identical to Site._create_subdomain, minus the (site-only) build_mode probe path:
	the Subdomain row is the same proxy contract for both aggregates."""
	subdomain = frappe.get_doc(
		{
			"doctype": "Subdomain",
			"subdomain": pilot.subdomain,
			"virtual_machine": pilot.virtual_machine,
			"active": 1,
		}
	).insert(ignore_permissions=True)
	return subdomain.name


def _regenerate_login(pilot) -> dict:
	"""Re-mint an already-deployed pilot's login URL and return the result. The
	regenerate twin of `_deploy`: the VM is already serving its FQDN, so this runs the
	guest deploy with `--regenerate-login` (re-sign only, no rename/setup). A
	Fake-backed VM never answers SSH, so the placeholder is synthesized exactly as the
	mint synthesizes it."""
	from atlas.atlas.deploy_site import regenerate_login
	from atlas.atlas.providers.fake_tasks import is_fake_server

	vm = frappe.get_doc("Virtual Machine", pilot.virtual_machine)
	if is_fake_server(vm.server):
		return {"login_url": f"https://{pilot.bench_fqdn}/app?sid=fake-sid"}
	mode = pilot.build_mode if pilot.attached else None
	return regenerate_login(pilot.virtual_machine, pilot.bench_fqdn, mode=mode) or {}


def pilot_for_vm(vm_name: str):
	"""The Pilot backing a Virtual Machine, or None. The VM→Pilot lookup the
	Central-facing API + the in-guest helper use to resolve a VM's bench front door
	(gateway_url, login_url) without the VM row carrying any bench state. A plain VM
	(proxy, operator machine) has no Pilot → None."""
	name = frappe.db.get_value("Pilot", {"virtual_machine": vm_name}, "name")
	return frappe.get_doc("Pilot", name) if name else None
