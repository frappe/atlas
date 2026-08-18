"""The `pilot-console` kind's deep lifecycle flow, for a `Site(kind="pilot-console")`.

A pilot-console is the bench analogue of a self-serve site: the tenant-owned
aggregate that fronts a bench (an admin console) at `<subdomain>.<region domain>`
(Contract A), boots its backing VM from a bench IMAGE (not a golden snapshot), and
mints the one-click admin login. It exists so the *bench provision* lives OFF the
`Virtual Machine` (which stays a pure microVM).

This module is the console-kind twin of the self-serve flow inlined in `site.py`:
the thin `Site` controller dispatches its lifecycle hooks here on `kind`, keeping
each flow a focused deep module rather than an `if kind ==` pyramid on every method
(the resumable.py shape — one thin surface, per-kind deep modules). The two flows
diverge in three ways a shared controller can't paper over:

- **VM creation timing.** A console creates its VM SYNCHRONOUSLY in `after_insert`
  (so `create_vm` can return the VM identity immediately), booting a bench image; a
  self-serve site clones a golden snapshot in the background job.
- **Readiness.** A console is Pending→Running with no HTTP gate; a site runs
  Provisioning→Deploying→Running behind an HTTP-200 probe.
- **Central shape.** A console reports AS its backing VM (`vm.*`, the VM-shaped
  mirror keyed by VM uuid); a site reports `site.*`. See spec/14-self-serve.md.

Central mirrors VMs, so a console reports through its VM and never sees a "Pilot" or
"pilot-console" string — the wire shape is the VM mirror (atlas.atlas.api.provision).
"""

from __future__ import annotations

import frappe

from atlas.atlas.core.placement import active_root_domain
from atlas.atlas.services import site_common
from atlas.atlas.services.subdomain_label import PILOT_SUFFIX

# How long a freshly-minted login URL stays usable, keyed by build_mode — the TTL of
# the token deploy-site.py minted. admin: `generate-admin-session`'s 5-minute
# single-use JWT; site: `bench browse`'s 24h session. Atlas stamps
# `login_url_expires_at` = mint time + this, and Central compares against it to decide
# "use it" vs "regenerate".
LOGIN_URL_TTL_MINUTES = {"admin": 5, "site": 24 * 60}


# ----- derived routing identity (Contract A) -----------------------------


def bench_fqdn(doc) -> str:
	"""The host this console is fronted at — `<subdomain>.<region domain>`, derived,
	never stored (Contract A: region/domain stay in Atlas). The bare host the in-guest
	deploy targets; `gateway_url` is the https URL Central shows."""
	return f"{doc.subdomain}.{active_root_domain().domain}"


def gateway_url(doc) -> str:
	"""The URL Central deep-links this console at — `https://<bench_fqdn>`."""
	return f"https://{bench_fqdn(doc)}"


def console_fqdn(doc) -> str:
	"""The host the bench ADMIN console is fronted at — what `[admin].domain` in the
	guest's bench.toml must say, and the label that needs a proxy route.

	- **admin** build_mode — the bench IS the console, so it owns `bench_fqdn` outright.
	  Covers every attached console (the one that comes with a Site) and a stand-alone
	  console baked from an admin image.
	- **site** build_mode — `bench_fqdn` serves the baked SITE, so the console needs a
	  host of its own: `<subdomain>-pilot.<region domain>` (the server case).

	Derived, never stored, and deterministic: a re-driven provision must derive the same
	host both times."""
	if (doc.build_mode or "site") == "admin":
		return bench_fqdn(doc)
	return f"{doc.subdomain}{PILOT_SUFFIX}.{active_root_domain().domain}"


# ----- lifecycle ---------------------------------------------------------


def after_insert(doc) -> None:
	"""Create the backing VM SYNCHRONOUSLY, then enqueue the boot→deploy job.

	The VM is created here, in the insert's transaction, so the Central-facing
	`create_vm` can read the VM's identity (name, ipv6) back through the console and
	return it in the mirror row Central upserts. The VM's OWN after_insert then
	auto-provisions it (a plain boot to Running, no bench logic). The bench work — wait
	for the VM to boot, deploy in-guest, mint the login URL — runs in the background job
	(the shared `site.auto_provision` dispatcher, routed here by kind); queue=long
	because it SSHes, enqueue_after_commit so the worker only starts once this insert has
	committed.

	ATTACHED path (a self-serve Site's console, `flags.attach_vm` set): this console
	binds a VM the Site already owns, so it neither creates a VM nor enqueues its own
	job. It only links the shared VM + marks itself attached; the Site's auto_provision
	drives the admin-console deploy on the already-booted VM."""
	attach_vm = doc.flags.get("attach_vm")
	if attach_vm:
		doc.db_set("attached", 1)
		doc.db_set("virtual_machine", attach_vm)
		# The attached console serves the bench admin CONSOLE at its FQDN (the shared VM's
		# own build_mode is `site` — it serves the customer site at a different FQDN); its
		# login mint/TTL follows admin mode.
		doc.db_set("build_mode", "admin")
		return
	_provision_backing_vm(doc)
	frappe.enqueue(
		"atlas.atlas.doctype.site.site.auto_provision",
		queue="long",
		timeout=1800,
		enqueue_after_commit=True,
		site_name=doc.name,
		# The pilot credential is bench-level and never persisted on the row; it rides the
		# job to the backing VM + the bench's bench.toml. Flags are set by create_vm.
		pilot_credential_id=doc.flags.get("pilot_credential_id"),
		central_endpoint=doc.flags.get("central_endpoint"),
		bootstrap_token=doc.flags.get("bootstrap_token"),
	)


def auto_provision(
	doc,
	pilot_credential_id: str | None = None,
	central_endpoint: str | None = None,
	bootstrap_token: str | None = None,
) -> None:
	"""Wait for the (already-created) backing VM to boot, create the Subdomain that puts
	the console on the front door, mint the one-click login URL in the booted guest, and
	THEN mark Running — the same wait→route→deploy→mint ordering the self-serve flow
	uses, so the single Running event carries the handoff. The Subdomain is created
	BEFORE the deploy (it needs only the FQDN + the VM's /128) so the proxy reconcile
	overlaps the deploy. No-op if the row has moved past Pending. Fail loud so a console
	whose mint fails is Failed, not a silently login-less Running.

	Driven by the `site.auto_provision` dispatcher on a re-fetched Pending doc."""
	import time

	def _trace(message: str, since: float | None = None) -> None:
		suffix = f" ({time.monotonic() - since:.1f}s)" if since is not None else ""
		line = f"[console auto_provision {doc.name}] {message}{suffix}"
		print(line, flush=True)  # noqa: T201 -- follow-along trace to the RQ worker log
		frappe.logger("atlas").info(line)

	try:
		# Stamp the credential id on the backing VM before anything else, so the vm.*
		# events Atlas emits from here on echo it back and Central can bind its reserved
		# Pilot Credential to this VM.
		if pilot_credential_id:
			frappe.db.set_value(
				"Virtual Machine", doc.virtual_machine, "pilot_credential_id", pilot_credential_id
			)
		_trace(f"waiting for backing VM {doc.virtual_machine} to boot (Running) …")
		_t = time.monotonic()
		_wait_for_vm_running(doc.virtual_machine)
		_trace("VM Running; creating Subdomain (proxy route) …", since=_t)
		_t = time.monotonic()
		subdomain_name = _create_subdomain(doc)
		doc.db_set("subdomain_doc", subdomain_name)
		# nosemgrep: frappe-manual-commit -- fire the after_commit proxy reconcile now so it overlaps the deploy
		frappe.db.commit()
		_trace("Subdomain created; minting login URL (in-guest deploy) …", since=_t)
		_t = time.monotonic()
		result = _deploy(doc, central_endpoint=central_endpoint, bootstrap_token=bootstrap_token)
		_stamp_login(doc, result)
		doc.db_set("login_url", doc.login_url)
		doc.db_set("login_url_expires_at", doc.login_url_expires_at)
		_trace("login URL minted; marking Running …", since=_t)
		doc.db_set("status", "Running")
		# db_set skips on_update, so the status event that carries the login handoff won't
		# fire on its own — emit it explicitly. Its delivery is enqueue_after_commit, so it
		# rides the commit just below.
		from atlas.atlas.services.reporting import report_pilot_status

		report_pilot_status(doc)
		# nosemgrep: frappe-manual-commit -- commit the handoff + Running so the status event delivers (enqueue_after_commit) and the poll sees it
		frappe.db.commit()
		_trace("marked Running — console provision complete")
	except Exception:
		_trace("FAILED — flipping status to Failed")
		doc.db_set("status", "Failed")
		# nosemgrep: frappe-manual-commit -- background job: commit Failed so it survives the job's rollback
		frappe.db.commit()
		raise


def deploy_attached(doc) -> None:
	"""Wire the admin console for an ATTACHED console on its (already-booted) shared VM.

	Called by `Site.auto_provision` AFTER the site is serving: the backing VM is up, and
	the Site's own site-mode deploy already wrote the console FQDN into `[admin].domain`
	and emitted the admin vhost. So this only mints the admin login URL, creates the
	console's Subdomain (the second proxy route → the SAME VM /128), and marks Running.
	The attached twin of `auto_provision` minus the VM-boot wait and the front-door
	setup the site deploy already did.

	Fail loud ON THE CONSOLE ROW; the raise does NOT fail the owning Site, though — the
	console is a second, additive front door on a VM whose site already serves, so
	`Site._attach_pilot_console` logs it and lets the Site reach Running."""
	if doc.status != "Pending":
		return
	try:
		subdomain_name = _create_subdomain(doc)
		doc.db_set("subdomain_doc", subdomain_name)
		# nosemgrep: frappe-manual-commit -- fire the after_commit proxy reconcile now, as auto_provision does
		frappe.db.commit()
		result = _regenerate_login(doc)
		_stamp_login(doc, result)
		doc.db_set("login_url", doc.login_url)
		doc.db_set("login_url_expires_at", doc.login_url_expires_at)
		doc.db_set("status", "Running")
		from atlas.atlas.services.reporting import report_pilot_status

		report_pilot_status(doc)
	except Exception:
		doc.db_set("status", "Failed")
		# nosemgrep: frappe-manual-commit -- persist Failed so it survives a rollback
		frappe.db.commit()
		raise


def regenerate_login_url(doc) -> dict:
	"""Re-mint this console's one-click login URL and return the fresh VM-shaped payload
	Central re-reads. Only a Running console has a login URL to regenerate. Re-mint in
	the guest (admin mode → `generate-admin-session`, site mode → `browse`), stamp
	`login_url` + expiry, COMMIT so Central's poll/reconcile sees it, and return the
	mirror. The whitelisted `Site.regenerate_login_url` dispatches here on kind."""
	if doc.status != "Running":
		frappe.throw(f"Cannot regenerate a login URL from {doc.status}")
	result = _regenerate_login(doc)
	_stamp_login(doc, result)
	doc.save(ignore_permissions=True)
	# nosemgrep: frappe-manual-commit -- persist the fresh URL so Central's poll/reconcile sees it cross-transaction
	frappe.db.commit()
	from atlas.atlas.services.reporting import _pilot_vm_payload

	return _pilot_vm_payload(doc)


def _stamp_login(doc, result: dict) -> None:
	"""Stamp a minted login URL + its expiry on the doc (not committed) — the single
	place mint/regenerate share so the expiry is always mint time + the mode's TTL (5 min
	for admin's single-use JWT, 24h for a site session)."""
	mode = doc.build_mode or "site"
	ttl = LOGIN_URL_TTL_MINUTES.get(mode, LOGIN_URL_TTL_MINUTES["site"])
	doc.login_url = (result or {}).get("login_url", "")
	doc.login_url_expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=ttl)


def terminate(doc) -> None:
	"""Take the console off the front door, tear down its backing VM, mark Terminated.
	The console-kind twin of the self-serve terminate: delete the Subdomain, terminate
	the backing VM (a no-op when attached — the owning Site tears the shared VM down),
	then mark Terminated. Idempotent-ish: a second call on an already-Terminated row
	throws. Dispatched from `Site.terminate` on kind."""
	if doc.status == "Terminated":
		frappe.throw(frappe._("Site is already terminated"))
	site_common.delete_subdomain(doc)
	site_common.terminate_backing_vm(doc)
	doc.status = "Terminated"
	doc.save(ignore_permissions=True)


# ----- host seams (mocked in tests) --------------------------------------


def _provision_backing_vm(doc) -> str:
	"""Create the backing VM from a bench IMAGE and return its name.

	Unlike the self-serve flow (which CLONES a golden snapshot), a console boots a bench
	image directly. The VM's own after_insert auto-provisions it (a plain boot to
	Running). `build_mode` is inherited from the image by the VM at insert; the console
	mirrors it onto its own row so its login mint/TTL follows the mode without re-reading
	the VM."""
	from atlas.atlas.core.placement import default_image, default_server_for_image

	fleet_public_key = frappe.db.get_single_value("Atlas Settings", "ssh_public_key")
	if not fleet_public_key:
		frappe.throw("Atlas Settings.ssh_public_key is unset; cannot provision a VM the fleet can reach.")

	spec = doc.flags.get("vm_spec") or {}
	vm_doc = {
		"doctype": "Virtual Machine",
		"title": doc.subdomain,
		"tenant": doc.tenant,
		"vcpus": spec.get("vcpus", 1),
		"memory_megabytes": spec.get("memory_megabytes", 512),
		"disk_gigabytes": spec.get("disk_gigabytes", 2),
		"ssh_public_key": fleet_public_key,
	}
	# server/image are Atlas's placement concern. The image is the pinned bench image
	# (caller override, else the Atlas Settings default). The server must be one that HOLDS
	# that image — a baked bench image is often local — so we pick from the image's home
	# set (default_server_for_image throws loudly if the image lives nowhere yet). A
	# caller-pinned server still wins; a pinned-but-missing server is ignored.
	vm_doc["image"] = spec["image"] if spec.get("image") else default_image()
	if spec.get("server") and frappe.db.exists("Server", spec["server"]):
		vm_doc["server"] = spec["server"]
	else:
		vm_doc["server"] = default_server_for_image(
			vm_doc["image"],
			required_vcpus=float(spec.get("cpu_max_cores") or vm_doc["vcpus"]),
			required_memory_mb=float(vm_doc["memory_megabytes"]),
			required_disk_gb=float(vm_doc["disk_gigabytes"]),
		)
	vm = frappe.get_doc(vm_doc)
	if spec.get("cpu_max_cores"):
		vm.cpu_max_cores = float(spec["cpu_max_cores"])
	vm.insert(ignore_permissions=True)
	# db_set writes both the DB and the in-memory doc, so create_vm — holding this same
	# doc — reads the VM's identity straight back for its mirror row.
	doc.db_set("virtual_machine", vm.name)
	doc.db_set("build_mode", vm.build_mode or "site")
	return vm.name


def _wait_for_vm_running(vm_name: str) -> None:
	"""Block until the backing VM's own boot job flips it to Running. Reuses the
	self-serve flow's proven wait (poll the committed status with rollback, raise on
	Failed/deadline)."""
	from atlas.atlas.doctype.site.site import _wait_for_vm_running as _wait

	_wait(vm_name)


def _deploy(doc, central_endpoint: str | None = None, bootstrap_token: str | None = None) -> dict:
	"""Run the in-guest deploy for the booted backing VM and return the parsed result
	(carries `login_url`). Points the FQDN at the admin console (admin mode) or the baked
	site (site mode) and mints the mode's login URL.

	`central_endpoint`/`bootstrap_token` (when Central supplied them) are what the guest
	runs `bench admin enroll` with — the step that turns Central's RESERVED Pilot
	Credential into an Active one. A Fake-backed VM never answers SSH, so a placeholder is
	synthesized to keep desk/e2e green."""
	from atlas.atlas.core.providers.fake_tasks import is_fake_server
	from atlas.atlas.services.deploy_site import deploy_site

	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if is_fake_server(vm.server):
		return {"login_url": f"https://{bench_fqdn(doc)}/app?sid=fake-sid"}
	# An attached console's build_mode is `admin` (it serves the console at its FQDN) while
	# the shared VM's build_mode is `site`; pass mode explicitly so the deploy wires the
	# admin vhost, not another site rename. A stand-alone console passes None → the VM's mode.
	mode = doc.build_mode if doc.attached else None
	return (
		deploy_site(
			doc.virtual_machine,
			bench_fqdn(doc),
			central_endpoint=central_endpoint,
			bootstrap_token=bootstrap_token,
			mode=mode,
			# Site mode only wires `[admin].domain` when told a host; without this a
			# stand-alone console (a server) keeps the baked `admin.localhost` placeholder.
			# Admin mode already defaults the domain to the FQDN, so passing it is a no-op
			# that keeps the two paths honest.
			admin_domain=console_fqdn(doc),
		)
		or {}
	)


def _regenerate_login(doc) -> dict:
	"""Re-mint an already-deployed console's login URL and return the result. The
	regenerate twin of `_deploy`: the VM is already serving, so this runs the guest
	deploy with `--regenerate-login` (re-sign only). A Fake-backed VM synthesizes the
	placeholder exactly as the mint does."""
	from atlas.atlas.core.providers.fake_tasks import is_fake_server
	from atlas.atlas.services.deploy_site import regenerate_login

	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if is_fake_server(vm.server):
		return {"login_url": f"https://{bench_fqdn(doc)}/app?sid=fake-sid"}
	mode = doc.build_mode if doc.attached else None
	return regenerate_login(doc.virtual_machine, bench_fqdn(doc), mode=mode) or {}


def _create_subdomain(doc) -> str:
	"""Create the proxy-map Subdomain row that puts the console on the front door, routing
	`<subdomain>.<region domain>` → the backing VM's /128, and — in site mode — the
	console's own host too.

	Get-or-create, because retry = re-run and this is the FIRST step of the attached
	deploy: a console that got its route and then failed at the mint is re-driven from the
	top, and a bare insert made that retry die on a duplicate key while the route was in
	fact already live and serving."""
	existing = frappe.db.get_value("Subdomain", doc.subdomain, "virtual_machine")
	if existing is not None:
		if existing != doc.virtual_machine:
			frappe.throw(
				f"Subdomain '{doc.subdomain}' already routes to VM {existing}, "
				f"not this console's {doc.virtual_machine}"
			)
		return doc.subdomain
	subdomain = frappe.get_doc(
		{
			"doctype": "Subdomain",
			"subdomain": doc.subdomain,
			"virtual_machine": doc.virtual_machine,
			"active": 1,
		}
	).insert(ignore_permissions=True)
	_create_console_subdomain(doc)
	return subdomain.name


def _create_console_subdomain(doc) -> str | None:
	"""Route the bench ADMIN console's own host, when it needs one of its own.

	A stand-alone console (a server) serves the baked SITE at `bench_fqdn`, so its console
	lives at `<subdomain>-pilot.<region domain>` (`console_fqdn`) and that label needs its
	own proxy route. No-op for an attached console, whose console IS `bench_fqdn`.

	Get-or-create for the same retry-safety as `_create_subdomain`; a label that resolves
	somewhere else is a genuine conflict, so it fails loud rather than silently repointing
	someone else's host."""
	if console_fqdn(doc) == bench_fqdn(doc):
		return None
	label = console_fqdn(doc).split(".", 1)[0]
	existing = frappe.db.get_value("Subdomain", label, "virtual_machine")
	if existing is not None:
		if existing != doc.virtual_machine:
			frappe.throw(
				frappe._("Subdomain '{0}' already routes to VM {1}, not this console's {2}").format(
					label, existing, doc.virtual_machine
				)
			)
		return label
	console = frappe.get_doc(
		{
			"doctype": "Subdomain",
			"subdomain": label,
			"virtual_machine": doc.virtual_machine,
			"active": 1,
		}
	).insert(ignore_permissions=True)
	return console.name
