import json
import shlex
import uuid
from contextlib import contextmanager
from typing import ClassVar

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import scripts_catalog
from atlas.atlas.providers.fake_tasks import is_fake_server
from atlas.atlas.ssh import connection_for_server, run_ssh, run_task, ssh_key_file, upload_files
from atlas.atlas.task_results import parse_result

IMMUTABLE_AFTER_INSERT = (
	"title",
	"provider_type",
	"provider_resource_id",
	"size",
	"image",
	"ipv4_address",
	"ipv6_address",
	"ipv6_prefix",
	"ipv6_virtual_machine_range",
)


class Server(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		architecture: DF.Data | None
		cli_ready: DF.Check
		firecracker_version: DF.Data | None
		image: DF.Link | None
		ipv4_address: DF.Data | None
		ipv6_address: DF.Data | None
		ipv6_prefix: DF.Data | None
		ipv6_virtual_machine_range: DF.Data | None
		jailer_version: DF.Data | None
		kernel_version: DF.Data | None
		provider_metadata: DF.Code | None
		provider_resource_id: DF.Data | None
		provider_type: DF.Literal["", "DigitalOcean", "Scaleway", "Self-Managed", "Fake"]
		size: DF.Link | None
		status: DF.Literal["Pending", "Bootstrapping", "Active", "Draining", "Broken", "Archived"]
		title: DF.Data
	# end: auto-generated types

	BOOTSTRAP_ALLOWED_STATUS: ClassVar[set[str]] = {"Pending", "Bootstrapping", "Active", "Broken"}
	# Durable uploads beyond the atlas package (which _bootstrap_uploads()
	# computes from disk). The systemd-invoked hooks are .py now (positional
	# uuid); they and atlas-pool.service import the durable package under
	# /var/lib/atlas/bin (their sys.path shim adds that dir). The package itself
	# replaces the old durable lvm.sh — there is no shell helper library anymore.
	BOOTSTRAP_UPLOAD_SOURCES: ClassVar[list[tuple[str, str]]] = [
		# The pip-install manifest: bootstrap-server.py runs `uv pip install
		# /var/lib/atlas/bin` into the Atlas venv, which needs a pyproject.toml at
		# that root. host-pyproject.toml's wheel package root is `atlas` (the flat
		# durable layout), distinct from the dev scripts/pyproject.toml.
		("host-pyproject.toml", "/var/lib/atlas/bin/pyproject.toml"),
		# install.sh creates the uv venv + `atlas` console script over SSH right
		# after this upload, BEFORE the bootstrap Task (which then runs as a normal
		# `atlas bootstrap-server` verb). Shipped durably so the controller has a
		# local copy to pipe over SSH — no public URL needed.
		("install.sh", "/var/lib/atlas/bin/install.sh"),
		("vm-network-up.py", "/var/lib/atlas/bin/vm-network-up.py"),
		("vm-network-down.py", "/var/lib/atlas/bin/vm-network-down.py"),
		# vm-disk-up.py re-activates the VM's thin-snapshot disk LV and refreshes
		# its in-jail block node at every unit start — the disk analogue of
		# vm-network-up.py, so an enabled VM self-heals its disk after a reboot.
		("vm-disk-up.py", "/var/lib/atlas/bin/vm-disk-up.py"),
		# vm-restore.py resumes a pending memory snapshot at every unit start —
		# the ExecStartPost counterpart of the two ExecStartPre hooks above.
		("vm-restore.py", "/var/lib/atlas/bin/vm-restore.py"),
		("systemd/firecracker-vm@.service", "/etc/systemd/system/firecracker-vm@.service"),
		("systemd/atlas-pool.service", "/etc/systemd/system/atlas-pool.service"),
		# host-mesh.service brings up the wg-mesh private-plane device in the host
		# root netns at boot (design §3), the host-fabric analog of atlas-pool.service:
		# bootstrap creates the mesh but is not re-run on boot, so this oneshot
		# re-asserts the device from /etc/atlas-host-mesh.{env,key} + wg-mesh.conf.
		("systemd/host-mesh.service", "/etc/systemd/system/host-mesh.service"),
	]

	def autoname(self) -> None:
		# UUID identity: title is the human label, name is opaque.
		self.name = str(uuid.uuid4())

	def validate(self) -> None:
		self._validate_immutability()
		self._denormalize_mesh_identity()

	def _denormalize_mesh_identity(self) -> None:
		"""Fill the derived WireGuard host-mesh denorm fields (design §8). Both are pure
		functions of the Server UUID — recomputed by the controller wherever they are
		needed (host_mesh.reconcile), so these fields are a legible read-through, not a
		source of truth. Set once; a re-derive yields the same value, so an existing row
		is unchanged on save."""
		if not self.wireguard_public_key:
			from atlas.atlas.networking import derive_host_wireguard_keypair

			_private_key, self.wireguard_public_key = derive_host_wireguard_keypair(self.name)
		if not self.mesh_address:
			from atlas.atlas.networking import derive_host_mesh_address

			self.mesh_address = derive_host_mesh_address(self.name)

	def _validate_immutability(self) -> None:
		"""Lock fields once they carry a value. Allow None → value transitions
		so the DigitalOcean provision flow (`finish_provisioning`) can write
		IPv4/6 onto a freshly-inserted Pending row whose addresses weren't
		known at insert time."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			new_value = getattr(self, field)
			if not old_value:
				continue  # initial population is allowed
			if old_value != new_value:
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def archive(self) -> None:
		"""Destroy the vendor resource (idempotent), then mark Archived.

		Resolve the vendor by the Server's OWN frozen `provider_type`, not the active
		one (`atlas.get_provider()`) — a host outlives a vendor switch, so destroy()
		must hit the client that owns the resource. Mirrors `reserved_ip.py`'s
		`_provider_for_server`."""
		from atlas.atlas.providers import for_provider_type

		if self.status == "Archived":
			frappe.throw(_("Server is already archived"))
		if self.provider_resource_id:
			for_provider_type(self.provider_type).destroy(self.provider_resource_id)
		frappe.db.set_value(self.doctype, self.name, "status", "Archived")

	@frappe.whitelist()
	def recover(self) -> bool:
		"""Operator escape hatch: re-drive a Server stranded pre-Active.

		`provision()` creates the billing vendor box synchronously, then a single
		fire-and-forget `finish_provisioning` job adopts it (describe → IPs →
		Bootstrapping → bootstrap → Active). When that job is lost the row sits in
		Pending / Bootstrapping forever with a paid-for box behind it. This re-enqueues
		finish_provisioning — the same path the scheduled reconciler uses, deduplicated
		so it never stacks a second job atop one still in flight.

		Distinct from `bootstrap()`: that runs the host bootstrap straight away and
		needs the IPs already populated, whereas a lost-job row has NULL addresses —
		recover() runs the full describe()-poll first to fill them. Returns True if a
		job was enqueued, False if one was already queued/running.
		"""
		from atlas.atlas.providers.worker import enqueue_finish_provisioning

		if self.status not in ("Pending", "Bootstrapping", "Broken"):
			frappe.throw(f"Cannot recover from status {self.status}; nothing is stuck")
		if not self.provider_resource_id:
			frappe.throw(
				"Server has no provider_resource_id — provision() never recorded a vendor "
				"resource, so there is nothing to recover. Re-provision instead."
			)
		return enqueue_finish_provisioning(self.name)

	@frappe.whitelist()
	def sync_image(self, image: str) -> str:
		"""Single-server convenience wrapper around `Virtual Machine Image.sync_to_server`."""
		image_doc = frappe.get_doc("Virtual Machine Image", image)
		return image_doc.sync_to_server(self.name)

	@frappe.whitelist()
	def bootstrap(self) -> str:
		"""Upload helpers + units, create the Atlas venv (install.sh), then run the
		bootstrap-server verb. Returns Task name.

		Ordering is load-bearing: install.sh's `uv pip install` needs the uploaded
		/var/lib/atlas/bin, and the bootstrap Task now runs as `atlas bootstrap-server`
		on the venv install.sh creates — so it's upload → install.sh → bootstrap Task.
		"""
		if self.status not in self.BOOTSTRAP_ALLOWED_STATUS:
			frappe.throw(f"Cannot bootstrap from status {self.status}")

		# A Fake server has no host to scp the durable package onto or SSH install.sh
		# into; the bootstrap-server Task below is faked too and still records the
		# host versions, so the row ends up Active exactly as a real bootstrap leaves
		# it. Skip both the upload AND install.sh for it, in lockstep.
		if not is_fake_server(self.name):
			connection = connection_for_server(self)
			upload_files(connection, self._bootstrap_uploads())
			self._run_install_sh(connection)
			self._authorize_satellite_keys(connection)
			self._ship_dashboard(connection)

		task = run_task(
			server=self.name,
			script="bootstrap-server",
			variables={
				"FIRECRACKER_VERSION": "v1.16.0",
				"ARCHITECTURE": "x86_64",
			},
		)
		self._absorb_bootstrap_output(task.stdout)
		self.save(ignore_permissions=True)
		return task.name

	def _run_install_sh(self, connection) -> None:
		"""Run scripts/install.sh on the host over SSH, AFTER the upload — it creates
		the uv venv + `atlas` console script and runs the deep sanity gate. This is
		what removes the bootstrap carve-out: once it returns, `bootstrap-server` runs
		as a normal `atlas <verb>` on the venv. Not recorded as a Task (it's bootstrap
		plumbing, like upload_files); raises on a non-zero exit so a broken venv fails
		the bootstrap HERE, before the bootstrap Task or any unit points at it."""
		command = "bash /var/lib/atlas/bin/install.sh"
		with ssh_key_file(connection.ssh_private_key) as key_path:
			stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=600)
		if exit_code != 0:
			frappe.throw(
				f"install.sh failed on {self.name} (exit {exit_code}): {stderr[-500:] or stdout[-500:]}"
			)

	def _authorize_satellite_keys(self, connection) -> None:
		"""Append the Satellite orchestrator's public key(s) to the host's root
		authorized_keys so a Satellite can SSH the HOST for host-plane services (the
		mesh, the gateway — spec/28). Idempotent: a re-bootstrap never duplicates a line.
		No-op on an Atlas with no Satellite configured."""
		from atlas.atlas.atlas_settings import satellite_public_keys

		keys = satellite_public_keys()
		if not keys:
			return
		appends = " && ".join(
			f"grep -qxF {shlex.quote(key)} $AUTH || echo {shlex.quote(key)} >> $AUTH" for key in keys
		)
		command = (
			"AUTH=/root/.ssh/authorized_keys; mkdir -p /root/.ssh && chmod 700 /root/.ssh "
			f"&& touch $AUTH && chmod 600 $AUTH && {appends}"
		)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			_stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=60)
		if exit_code != 0:
			frappe.throw(
				f"authorizing Satellite keys on {self.name} failed (exit {exit_code}): {stderr[-300:]}"
			)

	def _ship_dashboard(self, connection) -> None:
		"""Build the read-only host dashboard on the controller and ship it to the
		host, then enable its socket unit. WHOLLY best-effort: the dashboard is a
		convenience, not part of the host's function, so nothing here may fail a
		bootstrap. A build that can't run (no npm/node_modules) ships nothing; an
		SSH error shipping or enabling it is logged and swallowed. Runs AFTER
		install.sh so a broken venv still surfaces as a hard bootstrap failure —
		the dashboard ships onto an already-good host or not at all.

		Freshness: dashboard.dashboard_uploads() ships assets ONLY from a build it
		just ran (dist/ is a gitignored artifact), so a re-bootstrap always lands
		current assets alongside a matching server.py, never a stale dist."""
		from atlas.atlas import dashboard

		try:
			uploads = dashboard.dashboard_uploads()
			if not uploads:
				return  # build could not be produced — skip silently, no unit enabled
			upload_files(connection, uploads)
			with ssh_key_file(connection.ssh_private_key) as key_path:
				_stdout, stderr, exit_code = run_ssh(
					connection, key_path, dashboard.enable_command(), timeout_seconds=60
				)
			if exit_code != 0:
				frappe.logger("atlas").warning(
					f"dashboard socket enable failed on {self.name} (exit {exit_code}): {stderr[-300:]}"
				)
		except Exception as exception:
			# Never let a dashboard hiccup fail a real bootstrap.
			frappe.logger("atlas").warning(f"dashboard ship skipped on {self.name}: {exception}")

	@frappe.whitelist()
	def sync_scripts(self) -> int:
		"""Re-upload the durable scripts (atlas package + systemd-invoked .py
		hooks) to /var/lib/atlas/bin without re-running bootstrap, then reinstall
		the atlas package into the venv so the new code is what imports resolve.

		The development fast path: after editing anything under scripts/lib/atlas/
		(or vm-network-up.py et al.) push the change to a live host in one scp
		sweep, instead of a full `bootstrap` (which also runs bootstrap-server.py
		and mutates status). Bootstrap remains the single refresh point for unit
		files; this is the subset that's pure code. Idempotent — a plain overwrite.

		The scp lands the package at /var/lib/atlas/bin/atlas, but every entry
		script and systemd hook imports `atlas` from the venv's site-packages,
		where install.sh COPY-installed it at bootstrap (`uv pip install`, not
		editable). Overwriting bin/atlas alone leaves that copy frozen — the edit
		never takes effect. So we `uv pip install --reinstall` the just-uploaded
		tree into the venv, exactly as install.sh's step 3 does; that is what makes
		sync a true code refresh rather than a dead-drop into bin/atlas.

		Returns the number of files uploaded.
		"""
		if not self.ipv4_address:
			frappe.throw(f"Server {self.name} has no ipv4_address; cannot sync scripts")
		connection = connection_for_server(self)
		uploads = self._script_uploads()
		upload_files(connection, uploads)
		self._reinstall_atlas_venv_package(connection)
		return len(uploads)

	def _reinstall_atlas_venv_package(self, connection) -> None:
		reinstall_atlas_venv_package(connection, self.name)

	@frappe.whitelist()
	def reboot(self) -> str:
		"""Run reboot-server.sh as a Task. SSH drops mid-Task — Task ends in
		Failure; the operator confirms reboot by waiting and reconnecting."""
		return self.run_task_dialog(script="reboot-server", variables={})

	@frappe.whitelist()
	def run_task_dialog(self, script: str, variables: dict | str | None = None) -> str:
		"""Operator escape hatch. Same code path as bootstrap/provision.

		`variables` is a dict (JS form post) or JSON string. Returns Task name.
		"""
		if isinstance(variables, str):
			try:
				variables = json.loads(variables or "{}")
			except json.JSONDecodeError as exception:
				frappe.throw(f"variables must be valid JSON: {exception}")
		if variables is None:
			variables = {}
		if not isinstance(variables, dict):
			frappe.throw(_("variables must be a JSON object"))
		if script not in scripts_catalog.allowed_scripts():
			frappe.throw(f"Unknown script: {script}")
		task = run_task(
			server=self.name,
			script=script,
			variables=variables,
			timeout_seconds=1800,
		)
		return task.name

	@frappe.whitelist()
	def get_scripts(self) -> list[dict]:
		"""Whitelisted: operator-visible scripts + Run Task dialog metadata.

		Each entry is `{name, intro, fields}`. The client renders the dialog
		straight from this shape — fields are Frappe Dialog field dicts.

		The picker is intentionally shorter than `allowed_scripts()`.
		Lifecycle scripts (provision-vm, terminate-vm, vm-network-up, ...) are
		invoked from VM/Image controllers, not by hand from this dialog.
		"""
		return [
			{"name": name, **scripts_catalog.script_form(name)}
			for name in scripts_catalog.operator_visible_scripts()
		]

	def _bootstrap_uploads(self) -> list[tuple[str, str]]:
		return self._script_uploads() + self._unit_uploads()

	def _script_uploads(self) -> list[tuple[str, str]]:
		"""The durable scripts that live under /var/lib/atlas/bin: the importable
		atlas package, the systemd-invoked .py hooks, and the Task entry scripts.
		These are pure code — an scp overwrite is all it takes for an edit to land,
		no daemon-reload. This is exactly the set `sync_scripts()` refreshes during
		development; bootstrap ships it alongside `_unit_uploads()`."""
		directory = scripts_catalog.scripts_directory()
		uploads = [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
			if destination.startswith("/var/lib/atlas/bin/")
		]
		# The durable atlas package: every lib module lands under
		# /var/lib/atlas/bin/atlas/ so the .py hooks and atlas-pool.service can
		# `import atlas`. Computed from disk (test_*.py skipped) so a new module
		# is shipped with no edit here — mirrors script_uploads.package staging.
		package_dir = directory / "lib" / "atlas"
		for entry in sorted(package_dir.glob("*.py")):
			if entry.name.startswith("test_"):
				continue
			uploads.append((str(entry), f"/var/lib/atlas/bin/atlas/{entry.name}"))
		# The durable Task entry scripts: every host SSH Task (provision-vm.py,
		# start/stop/snapshot-stop, …). `host_task_scripts()` yields VERBS; the FILE
		# (verb→file_for, e.g. provision-vm.py) is what ships — the file keeps its
		# suffix on the host disk, where `uv pip install` registers the console
		# entry and the runner reaches it as `atlas <verb>`. Shipping them here lets
		# the runner invoke each in place instead of scp'ing it per Task — the scp
		# was the dominant latency of an otherwise-instant start/stop. Computed from
		# disk (scripts_catalog) so a new Task script ships with no edit here.
		for verb in scripts_catalog.host_task_scripts():
			file_name = scripts_catalog.file_for(verb)
			uploads.append((str(directory / file_name), f"/var/lib/atlas/bin/{file_name}"))
		return uploads

	def _unit_uploads(self) -> list[tuple[str, str]]:
		"""The bootstrap-only uploads that are NOT plain /var/lib/atlas/bin code —
		systemd unit files under /etc/systemd/system. Editing one needs a
		daemon-reload (a bootstrap concern), so `sync_scripts()` deliberately omits
		these."""
		directory = scripts_catalog.scripts_directory()
		return [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
			if not destination.startswith("/var/lib/atlas/bin/")
		]

	def _absorb_bootstrap_output(self, stdout: str) -> None:
		# bootstrap-server.py emits a typed BootstrapResult as one
		# `ATLAS_RESULT=<json>` line; parse_result pulls it out (the host still
		# also writes /var/lib/atlas/bootstrap.json as the on-disk source of
		# truth). Replaces the old "last non-empty stdout line is the JSON" scrape.
		#
		# The result also carries `python_version` (the resolved Atlas venv python).
		# It is deliberately NOT absorbed onto a Server field: it is derived state —
		# `/var/lib/atlas/venv/bin/python --version` on the host and the bootstrap
		# script's PY_VERSION constant are both live truth, so persisting a copy
		# would only drift. It rides the bootstrap log (this Task's stdout) for
		# visibility; nothing reads it back.
		parsed = parse_result(stdout)
		self.firecracker_version = parsed["firecracker_version"]
		self.jailer_version = parsed["jailer_version"]
		self.kernel_version = parsed["kernel_version"]
		self.architecture = parsed["architecture"]
		# The host's capacity totals ride the same BootstrapResult line (see
		# atlas.hostfacts). `.get()` because a Fake host's synthesized bootstrap
		# result omits them — its capacity comes from `fake_host_totals` in
		# `capacity_for_server`, so the row's totals stay unset and it reads as a
		# measured Fake host regardless. A real bootstrap always carries all three.
		self._stamp_capacity_facts(
			parsed.get("vcpus_total"),
			parsed.get("memory_megabytes_total"),
			parsed.get("pool_disk_gigabytes_total"),
		)
		# Reaching here means the bootstrap Task succeeded — and run_task raises on
		# any failure, so bootstrap-server.py's deep sanity gate (which runs
		# `atlas --help` to prove the console script dispatches) passed. Persist
		# CLI-readiness once, here, instead of paying a per-Task `test -e` round
		# trip: a legacy/unbootstrapped host has cli_ready=0 and the operator sees
		# the re-bootstrap signal. Fail-fast moved from per-Task to once-at-bootstrap.
		self.cli_ready = 1

	def _stamp_capacity_facts(
		self,
		vcpus_total: int | None,
		memory_megabytes_total: int | None,
		pool_disk_gigabytes_total: int | None,
		pool_data_percent: float | None = None,
	) -> None:
		"""Persist the host's measured capacity totals and the stamp time. Shared by
		bootstrap (three totals; pool fullness starts ~0, so it is left out) and
		Refresh Capacity (all four). `capacity_reported_at` records when the host was
		last measured, so a host silent past a staleness threshold can be treated as
		uncatalogued later rather than trusting stale totals (a future guard)."""
		self.vcpus_total = vcpus_total
		self.memory_megabytes_total = memory_megabytes_total
		self.pool_disk_gigabytes_total = pool_disk_gigabytes_total
		if pool_data_percent is not None:
			self.pool_data_percent = pool_data_percent
		self.capacity_reported_at = frappe.utils.now_datetime()

	@frappe.whitelist()
	def refresh_capacity_facts(self) -> str:
		"""Re-measure the host's capacity facts and stamp them — the Refresh Capacity
		button. For an already-Active host whose shape changed (a resized droplet, a
		grown pool) or that was bootstrapped before the totals were reported. Runs the
		read-only `server-facts` Task and persists the four numbers; returns the Task
		name. Bootstrap already stamps the three totals, so this is the no-re-bootstrap
		refresh — and the one path that also captures live `pool_data_percent`."""
		if self.status != "Active":
			frappe.throw(f"Refresh capacity on an Active host (status is {self.status})")
		task = run_task(server=self.name, script="server-facts", variables={}, timeout_seconds=120)
		parsed = parse_result(task.stdout)
		self._stamp_capacity_facts(
			parsed["vcpus_total"],
			parsed["memory_megabytes_total"],
			parsed["pool_disk_gigabytes_total"],
			parsed["pool_data_percent"],
		)
		self.save(ignore_permissions=True)
		return task.name


def reinstall_atlas_venv_package(connection, server_name: str) -> None:
	"""Reinstall the durable /var/lib/atlas/bin tree into the Atlas venv so the
	just-synced code is what `import atlas` resolves to. Mirrors install.sh's
	step 3 (`uv pip install --reinstall`) verbatim — the venv holds a COPY, not an
	editable link, so a plain scp overwrite of bin/atlas would not reach it. The
	uv/venv literals match install.sh (UV_DIR / ATLAS_VENV / BIN_DIRECTORY); the
	two trees don't share imports, so the paths are repeated here. Pure SSH — safe
	to call from a sync_scripts_to_all worker thread."""
	command = (
		"sudo env VIRTUAL_ENV=/var/lib/atlas/venv "
		"/var/lib/atlas/uv/uv pip install --reinstall /var/lib/atlas/bin"
	)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=300)
	if exit_code != 0:
		frappe.throw(
			f"atlas venv reinstall failed on {server_name} (exit {exit_code}): "
			f"{stderr[-500:] or stdout[-500:]}"
		)


def sync_scripts_to_all() -> dict[str, int]:
	"""Push the durable scripts to every Active server in one sweep.

	The development convenience: edit a script under scripts/lib/atlas/ once, then
	`bench --site <site> execute atlas.sync_scripts_to_all` (or `atlas.sync_scripts_to_all()`
	in a console) to refresh every live host. Active-only because a Pending/Broken
	server has no working SSH endpoint. Returns {server_name: files_uploaded}.

	Hosts are synced CONCURRENTLY: each host's cost is now dominated by its cold SSH
	handshake (a few seconds to a remote region), and those handshakes are
	independent I/O — a serial sweep pays them back-to-back (N x handshake), a
	parallel one overlaps them (~1 x handshake).

	All Frappe/DB work (the doc load, the connection, the upload list) is resolved
	HERE on the main thread first; the pool threads only do the pure-SSH push. That
	push still reaches Frappe for cosmetics (`frappe.utils.nowtime()` in the upload
	log line reads `frappe.local`, which is thread-local and empty in a fresh
	worker), so each worker binds its own Frappe context to the SAME site for the
	duration of its upload via `frappe_thread_context`."""
	names = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")

	# Resolve everything that touches the DB on the main thread: the doc, its SSH
	# connection, and the file list. The thread only does the SSH upload.
	jobs = []
	for name in names:
		server = frappe.get_doc("Server", name)
		if not server.ipv4_address:
			frappe.throw(f"Server {name} has no ipv4_address; cannot sync scripts")
		jobs.append((name, connection_for_server(server), server._script_uploads()))

	if not jobs:
		return {}

	site = frappe.local.site

	def _push(job) -> tuple[str, int]:
		name, connection, uploads = job
		with frappe_thread_context(site):
			print(f"Syncing durable scripts to {name} ({connection.host})")
			upload_files(connection, uploads)
			reinstall_atlas_venv_package(connection, name)
			print(f"Done syncing durable scripts to {name} ({connection.host})")
		return name, len(uploads)

	from concurrent.futures import ThreadPoolExecutor

	with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
		return dict(pool.map(_push, jobs))


@contextmanager
def frappe_thread_context(site: str):
	"""Bind a Frappe context to `site` for the current thread, then tear it down.

	`frappe.local` is thread-local, so a worker thread spawned off the request/CLI
	main thread starts with no site bound — any `frappe.*` that reads `local` (e.g.
	`frappe.utils.nowtime()` reaching for the site timezone) raises `AttributeError:
	conf`. Init + connect gives the worker its own bound context and DB connection
	(NOT shared with the main thread's, which would be unsafe); `destroy()` closes
	it so the thread leaves nothing behind. Read-mostly here — the upload does no
	writes — but each worker owning its connection keeps it correct if that changes."""
	frappe.init(site=site)
	frappe.connect()
	try:
		yield
	finally:
		frappe.destroy()
