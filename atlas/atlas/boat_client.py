"""Client for Boat — the per-host Go daemon that owns VM mechanics (spec/33, WO-0).

Boat is the successor to running a verb over SSH: same verb grammar, same Task
row, different transport. This module is the Atlas half of that seam and nothing
more — it speaks the HTTP/JSON contract in `api/openapi.yaml` (frappe/boat),
which is the source of truth for every shape here.

Three public entry points, and the split between them is deliberate:

  - `BoatClient` is the typed transport, one method per endpoint, shaped
    like `DigitalOceanClient`: `requests`, no retries, one shot, fail loud. The
    operator retries by clicking the button.
  - `put_desired_state` states what a VM should be — the desired spec and the
    fence epoch — and is the push half of the pair `boat_mirror.sync_mirror`
    completes (spec/33 §2.5). From WO-2 a lifecycle verb mutates desired state
    and the host's reconciler drives observed toward it (§11.3), so this is the
    mutation and the verb that follows only says "now".
  - `run_boat_task` is `run_task`'s twin. It takes the same keyword arguments,
    creates and drives the same `Task` row through the same `Pending → Running →
    Success/Failure` lifecycle with the same `stdout`/`stderr`/`exit_code`, and
    raises the same `frappe.ValidationError` on failure. Nothing downstream —
    the Task list, the retry button, `_propagate_status_to_virtual_machine` —
    can tell which transport ran the verb. That is what lets a call site become
    `run = run_boat_task if boat_enabled(server) else run_task`.

`op_id` is the Frappe Task name. That single identity is what makes a retry a
*replay* rather than a second boot: Boat keys its append-only op journal on it,
so re-posting an identifier it has already seen returns the recorded result
without touching the host (spec/33 §2.7).

There is no fallback to SSH anywhere below. A fallback would hide exactly the
failure this seam exists to expose, and would risk running a verb twice on a
host whose Boat acted but whose answer was lost. The rollback is the per-host
`Server.boat_enabled` flag, not a silent retry on another transport.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas._ssh.runner import _elapsed_ms, _finalize, _mark_running
from atlas.atlas.providers.fake_tasks import is_fake_server

if TYPE_CHECKING:
	from atlas.atlas.doctype.task.task import Task
	from atlas.atlas.doctype.virtual_machine.virtual_machine import VirtualMachine

# Boat binds the management-tunnel address and /run/boat.sock, never a public
# interface. The port is not yet pinned by the contract, so it is configurable
# and this is only the default.
DEFAULT_BOAT_PORT = 8080

# The API call's own deadline when the caller states none. A Task's
# `timeout_seconds` overrides it, exactly as it bounds an SSH run.
DEFAULT_TIMEOUT_SECONDS = 30

# The two Task verbs whose request carries more than an identifier.
START_VERB = "start-vm"
STOP_VERB = "stop-vm"

# The first fence epoch Atlas issues. Epochs start at 1 (`api/openapi.yaml`,
# `DesiredVirtualMachine.boot_epoch`): Boat gates on whether it holds a fence at
# all, so 0 would read as a fence it holds rather than one it lacks. Atlas is the
# sole issuer, and the epoch bumps at exactly one point — a migration's repoint
# (spec/33 §11.1) — which is not this module's to do.
FIRST_BOOT_EPOCH = 1

# The desired spec, as `DesiredVirtualMachine` names it. Every one of these is a
# `Virtual Machine` fieldname too, because both sides took their names from the
# same chapter, so the mapping is the identity and stays legible as one list.
#
# What is deliberately absent is the point: `status`, `has_memory_snapshot`,
# `last_started` and the rest are OBSERVED (spec/33 §1). Boat reports those, and
# stating them back would make Atlas an authority on facts it does not hold.
DESIRED_SPEC_FIELDS = (
	"vcpus",
	# Sent as the row's float. The contract types `cpu_max_cores` an integer,
	# which cannot express the fractional share a Shared 1x VM is sold (a quarter
	# of a core); sending the true number states the mismatch where it can be
	# fixed, while rounding it would quietly sell a machine nobody bought.
	"cpu_max_cores",
	"cpu_mode",
	"memory_megabytes",
	"disk_gigabytes",
	"data_disk_gigabytes",
	"idle_timeout_seconds",
	"ipv6_address",
	"private_address",
	"mac_address",
	"tap_device",
)


class BoatError(Exception):
	pass


def boat_enabled(server_name: str | None) -> bool:
	"""True iff this host is switched over to Boat. One cheap read, mirroring
	`is_fake_server`.

	Clearing the flag is WO-0's entire rollback: the next lifecycle call takes
	the SSH path with nothing else changed."""
	if not server_name:
		return False
	return bool(frappe.db.get_value("Server", server_name, "boat_enabled"))


class BoatClient:
	"""One host's Boat daemon over HTTP/JSON.

	One method per endpoint of `api/openapi.yaml`. Every non-2xx and every
	transport error raises `BoatError` carrying the daemon's own error sentence,
	so a caller cannot mistake a failure for a result."""

	def __init__(self, base_url: str, token: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
		self.base_url = base_url.rstrip("/")
		# Underscored and never rendered: the token goes into the Authorization
		# header and nowhere else — not into a log line, not into an exception.
		self._token = token
		self.timeout_seconds = timeout_seconds

	@classmethod
	def for_server(cls, server_name: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> "BoatClient":
		"""The client for one Server row, credentials resolved the way every other
		Atlas client resolves them."""
		return cls(
			base_url=base_url_for_server(server_name),
			token=token_for_server(server_name),
			timeout_seconds=timeout_seconds,
		)

	def start_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/start — returns the operation record.

		`operation_id` is the Frappe Task name. Re-posting one Boat has already
		recorded returns that result and boots nothing: retry is replay."""
		return self._request("POST", f"/vms/{uuid}/start", json={"operation_id": operation_id})

	def stop_virtual_machine(
		self,
		uuid: str,
		*,
		operation_id: str,
		graceful: bool = True,
		stop_timeout_seconds: int = 0,
	) -> dict:
		"""POST /vms/{uuid}/stop — returns the operation record.

		`graceful` asks the guest to power itself off first so its kernel syncs;
		`stop_timeout_seconds` of 0 leaves systemd's default drain in place. Same
		replay-by-`operation_id` rule as start."""
		body = {"operation_id": operation_id, "graceful": graceful}
		if stop_timeout_seconds:
			body["stop_timeout_seconds"] = stop_timeout_seconds
		return self._request("POST", f"/vms/{uuid}/stop", json=body)

	def pause_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/pause — freeze the guest's vCPUs. The unit stays
		active, so this frees CPU, not RAM."""
		return self._operation(uuid, "pause", operation_id)

	def resume_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/resume — the inverse of pause."""
		return self._operation(uuid, "resume", operation_id)

	def sleep_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/sleep — park the VM to free its RAM, so the first
		inbound SYN wakes it. Refused when the wake trap is not running: a VM that
		sleeps with nothing watching its counter never wakes (spec/32)."""
		return self._operation(uuid, "sleep", operation_id)

	def wake_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/wake — the operator's equivalent of the traffic that
		would have woken it."""
		return self._operation(uuid, "wake", operation_id)

	def rebuild_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/rebuild — lay the root disk down again, keeping the
		VM's identity, addresses and data disk."""
		return self._operation(uuid, "rebuild", operation_id)

	def terminate_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/terminate — destroy the VM and everything it holds on
		this host. Idempotent: terminating what is already gone succeeds, so a
		retry after a partial failure can finish the job."""
		return self._operation(uuid, "terminate", operation_id)

	def resize_virtual_machine(self, uuid: str, *, operation_id: str) -> dict:
		"""POST /vms/{uuid}/resize — apply the desired vCPU, memory and disk
		numbers. It carries none of them: they are desired state, so they arrive
		by `put_desired` and this only asks for them to be applied now."""
		return self._operation(uuid, "resize", operation_id)

	def _operation(self, uuid: str, verb: str, operation_id: str) -> dict:
		"""POST /vms/{uuid}/<verb> for every verb whose whole request is the
		operation identifier. Same replay-by-`operation_id` rule as start."""
		return self._request("POST", f"/vms/{uuid}/{verb}", json={"operation_id": operation_id})

	def put_desired(self, uuid: str, desired: dict) -> dict:
		"""PUT /vms/{uuid} — state this VM's whole desired spec, fence epoch
		included.

		The durable primitive (spec/33 §2.4): Boat diffs it against its store and
		runs forward to converge, so re-stating an unchanged spec changes nothing
		and re-stating it after a partition is how intent is re-asserted. It is
		also the only channel by which a Boat learns it may boot a UUID at all.

		The document names the VM as well as the path does, so the `uuid` is
		filled from the path here — a request that named two VMs would have two
		answers."""
		return self._request("PUT", f"/vms/{uuid}", json={**desired, "uuid": uuid})

	def get_virtual_machine(self, uuid: str) -> dict:
		"""GET /vms/{uuid} — Boat's observed document for one VM."""
		return self._request("GET", f"/vms/{uuid}")

	def list_virtual_machines(self) -> list[dict]:
		"""GET /vms — every VM this host observes."""
		return self._request("GET", "/vms")

	def get_host(self) -> dict:
		"""GET /host — host facts and the running `boat_version`."""
		return self._request("GET", "/host")

	def get_export(self) -> dict:
		"""GET /export — this host's entire observed state in one document.

		The mirror image of `PUT` desired: those two calls, back to back, fully
		resynchronize a host from any state (spec/33 §2.5). Boat materializes it
		inside one short read transaction and releases before writing it out, so
		this is a plain bounded request and not a stream — a busy host must never
		be mis-declared partitioned because Atlas read it slowly."""
		return self._request("GET", "/export")

	def get_operation(self, operation_id: str) -> dict:
		"""GET /ops/{operation_id} — the journal record for one Task name."""
		return self._request("GET", f"/ops/{operation_id}")

	def _request(self, method: str, path: str, json: dict | None = None):
		url = f"{self.base_url}{path}"
		headers = {
			"Authorization": f"Bearer {self._token}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		try:
			response = requests.request(method, url, json=json, headers=headers, timeout=self.timeout_seconds)
		except requests.RequestException as exception:
			# A refused connection, a DNS failure, a timeout. Raise it; never try
			# the host another way.
			raise BoatError(f"{method} {path} to {self.base_url} failed: {exception}") from exception
		if response.status_code >= 400:
			raise BoatError(f"{method} {path} -> {response.status_code}: {_error_sentence(response)}")
		if not response.content:
			return {}
		return response.json()


# Task verb -> the `BoatClient` method that serves it, for every lifecycle verb
# whose whole request is the operation identifier. They carry no arguments
# because every argument they would have carried is desired state, stated by the
# `put_desired` that precedes them (spec/33 §11.3).
#
# There is deliberately no default: a verb Boat does not serve must raise rather
# than appear to have run — see `_run_verb`.
OPERATION_VERBS = {
	"pause-vm": BoatClient.pause_virtual_machine,
	"resume-vm": BoatClient.resume_virtual_machine,
	"sleep-vm": BoatClient.sleep_virtual_machine,
	"wake-vm": BoatClient.wake_virtual_machine,
	"rebuild-vm": BoatClient.rebuild_virtual_machine,
	"terminate-vm": BoatClient.terminate_virtual_machine,
	"resize-vm": BoatClient.resize_virtual_machine,
}


def desired_state(virtual_machine: "VirtualMachine", **spec) -> dict:
	"""The `DesiredVirtualMachine` document for one VM row.

	`spec` overrides a field the row does not carry yet: a resize's new numbers
	are desired the moment the operator asks for them, and only reach the row once
	the host has applied them.

	A row with no `desired_power` raises rather than defaulting. Guessing a VM's
	power is precisely the mistake this seam exists to prevent — a wrong guess
	stops a live machine — so the caller states it."""
	if not virtual_machine.desired_power:
		raise BoatError(f"Virtual Machine {virtual_machine.name} has no desired_power to state")
	desired = {
		"uuid": virtual_machine.name,
		"boot_epoch": virtual_machine.boot_epoch or FIRST_BOOT_EPOCH,
		"desired_power": virtual_machine.desired_power,
		**{field: getattr(virtual_machine, field) for field in DESIRED_SPEC_FIELDS},
		**spec,
	}
	# Enrolment in the sleep reflex — and the one place Atlas declines to hand
	# Boat a contradiction. §11.3 makes `desired_power = Stopped` outrank the wake
	# trap, so a stopped VM would not be woken by traffic either way; this is
	# Atlas not asking the daemon to resolve a conflict it never needed to send.
	# The enrolment stays on the row, so the next start states it again.
	desired["sleep_on_idle"] = bool(virtual_machine.sleep_on_idle) and desired["desired_power"] == "Running"
	return desired


def put_desired_state(virtual_machine: "VirtualMachine", **spec) -> dict:
	"""State one VM's desired spec, fence epoch included, on its host's Boat.

	The push half of the pair `boat_mirror.sync_mirror` completes (spec/33 §2.5):
	`PUT` desired is how Atlas re-asserts intent, `GET /export` is how Boat
	re-asserts fact, and those two calls back to back resynchronize a host from
	any state.

	The caller gates on `boat_enabled` — a host without the flag is never called
	at all. A Fake-backed host is never called either, exactly as `run_boat_task`
	gives it no request: there is no daemon there to hold a fence."""
	if is_fake_server(virtual_machine.server):
		return {}
	client = BoatClient.for_server(virtual_machine.server)
	return client.put_desired(virtual_machine.name, desired_state(virtual_machine, **spec))


def base_url_for_server(server_name: str) -> str:
	"""Where this host's Boat answers.

	Boat listens on the management-tunnel address only, so the default is the
	same private address Atlas already reaches the host at, plus the daemon's
	port. Site config `atlas_boat_base_urls` maps a Server name to an explicit
	URL for a host whose daemon sits elsewhere (a dev bench, a forwarded port),
	and `atlas_boat_port` overrides the port fleet-wide. WO-1b's registration
	handshake replaces this with an address Boat reports, the way
	`central_link.provision_tunnel` learns the hub's endpoint today."""
	configured = (frappe.conf.get("atlas_boat_base_urls") or {}).get(server_name)
	if configured:
		return configured
	# The MESH address, deliberately, and never `ipv4_address`. Boat binds the
	# management tunnel and the local socket, so the mesh endpoint is the only
	# address it actually answers on — and it is the only one that keeps the
	# bearer token inside the tunnel. `ipv4_address` is the host's PUBLIC
	# address, the one `connection_for_server` SSHes to; defaulting to it would
	# put a long-lived token on the public internet in cleartext on every verb
	# and every mirror poll, which is worse than not reaching the host at all.
	address = frappe.db.get_value("Server", server_name, "mesh_address")
	if not address:
		frappe.throw(
			_("Server {0} has no mesh_address; Boat is only reachable over the management tunnel").format(
				server_name
			)
		)
	port = frappe.conf.get("atlas_boat_port") or DEFAULT_BOAT_PORT
	# Bracketed: the mesh address is IPv6, and an unbracketed v6 literal in a URL
	# parses its last colon as the port separator.
	return f"http://[{address}]:{port}/v1"


def token_for_server(server_name: str) -> str:
	"""This host's bearer token.

	Per-host and short-lived by design (spec/33 §12): Atlas mints it, Boat serves
	the last valid one under partition. WO-0 has no minting yet, so it is
	configured like every other Atlas credential and read through this one
	chokepoint — `atlas_boat_tokens` maps a Server name to its token, and
	`atlas_boat_token` is the single-host dev fallback. The value is never
	logged and never appears in an error message."""
	tokens = frappe.conf.get("atlas_boat_tokens") or {}
	token = tokens.get(server_name) or frappe.conf.get("atlas_boat_token")
	if not token:
		frappe.throw(
			_("Server {0} has no Boat token; set atlas_boat_tokens in site config").format(server_name)
		)
	return token


def run_boat_task(
	*,
	script: str,
	variables: dict,
	server: str,
	virtual_machine: str | None = None,
	timeout_seconds: int = 1800,
) -> "Task":
	"""Run a verb through the host's Boat daemon, recording the Task row
	`run_task` would have recorded.

	Keyword-for-keyword `run_task`'s signature, minus its `connection=` bootstrap
	escape hatch (there is no pre-row Boat, and bootstrap keeps its SSH path
	until WO-1b). Raises `frappe.ValidationError` on any failure — transport
	error, non-2xx, or a failed operation — with the Task row saved first, so the
	outcome survives the raise exactly as it does on the SSH path."""
	# Fake provider (developer_mode): a Task on a Fake-backed Server succeeds (or
	# fails on demand) with no Boat call, exactly as run_task gives it no SSH
	# connection. The dev/test fleet is Fake hosts; a real socket from a test
	# would go nowhere. So flipping boat_enabled on a Fake host is a no-op and
	# the synthesized Task row is identical either way.
	if is_fake_server(server):
		from atlas.atlas.providers.fake_tasks import run_fake_task

		return run_fake_task(server, script, variables, virtual_machine)

	if not virtual_machine:
		frappe.throw(
			_("run_boat_task: {0} needs a virtual_machine — Boat addresses VMs by UUID").format(script)
		)

	task = frappe.get_doc(
		{
			"doctype": "Task",
			"server": server,
			"virtual_machine": virtual_machine,
			"script": script,
			"status": "Pending",
			"triggered_by": frappe.session.user if frappe.session else "Administrator",
		}
	)
	task.variables_dict = variables
	task.insert(ignore_permissions=True)

	_execute_on_boat(task, server, script, variables, timeout_seconds)
	return task


def _execute_on_boat(
	task: "Task",
	server: str,
	script: str,
	variables: dict,
	timeout_seconds: int,
) -> None:
	"""Drive an inserted Task to its outcome over Boat. Mirrors
	`_ssh.runner._execute_into` step for step, and shares its `_mark_running` /
	`_finalize` so the row's shape cannot drift between the two transports."""
	_mark_running(task)
	start = time.monotonic()
	try:
		client = BoatClient.for_server(server, timeout_seconds=timeout_seconds)
		operation = _run_verb(client, script, task.virtual_machine, task.name, variables)
		status, exit_code = _outcome(operation, task.name)
	except Exception as exception:
		_finalize(task, "", str(exception), None, "Failure", _elapsed_ms(start))
		if isinstance(exception, frappe.ValidationError):
			raise
		raise frappe.ValidationError(str(exception)) from exception

	# Fold the daemon's own record onto the row: its trace becomes the Task's
	# stdout, its one-sentence error the stderr. The operator surface is the
	# same text it has always been.
	output = operation.get("output") or ""
	error = operation.get("error") or ""
	_finalize(task, output, error, exit_code, status, _elapsed_ms(start))
	if status == "Failure":
		frappe.throw(f"Task {task.name} ({script}) exited {exit_code}: {error[-500:]}")


def _run_verb(client: BoatClient, script: str, uuid: str, operation_id: str, variables: dict) -> dict:
	"""Map a Task verb onto its Boat endpoint, passing the Task name as `op_id`.

	The variables dict is the same one the SSH path renders to `--kebab-flags`,
	so a verb's inputs are stated once and mean the same thing on either
	transport. Only start and stop read it: every other verb's inputs are desired
	state, which reached the host by `put_desired` before this ran.

	An unmapped verb raises. A verb Boat cannot run must fail loud rather than
	appear to have run."""
	if script == START_VERB:
		return client.start_virtual_machine(uuid, operation_id=operation_id)
	if script == STOP_VERB:
		return client.stop_virtual_machine(
			uuid,
			operation_id=operation_id,
			graceful=variables.get("GRACEFUL", "1") != "0",
			stop_timeout_seconds=int(variables.get("STOP_TIMEOUT_SECONDS") or 0),
		)
	call = OPERATION_VERBS.get(script)
	if call:
		return call(client, uuid, operation_id=operation_id)
	served = ", ".join(sorted((START_VERB, STOP_VERB, *OPERATION_VERBS)))
	raise BoatError(f"Boat serves no endpoint for verb {script!r} (it serves {served})")


def _outcome(operation: dict, operation_id: str) -> tuple[str, int]:
	"""The Task status and exit code an operation record implies.

	WO-0's daemon records an operation terminal before it answers, so a
	non-terminal status here means the operation outlived its request — a
	protocol surprise, not an outcome. Raise instead of guessing; the operator
	reads `GET /ops/{id}` for the real state."""
	status = operation.get("status")
	if status not in ("Success", "Failure"):
		raise BoatError(f"Boat operation {operation_id} came back {status!r}, not a terminal result")
	exit_code = operation.get("exit_code")
	if exit_code is None:
		exit_code = 0 if status == "Success" else 1
	return status, int(exit_code)


def _error_sentence(response: "requests.Response") -> str:
	"""Boat's own explanation, per the Error schema — one sentence, stated at the
	boundary where the failure was detected. Falls back to the raw body when the
	response is not a typed error (a proxy's 502 page, say)."""
	try:
		return response.json()["error"]
	except (ValueError, KeyError, TypeError):
		return response.text
