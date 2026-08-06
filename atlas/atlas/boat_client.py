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
    can tell which transport ran the verb. That twinning is what let each call
    site move across one at a time while both transports still existed.

`op_id` is the Frappe Task name. That single identity is what makes a retry a
*replay* rather than a second boot: Boat keys its append-only op journal on it,
so re-posting an identifier it has already seen returns the recorded result
without touching the host (spec/33 §2.7).

There is no fallback to SSH anywhere below, and since the cutover there is no
SSH verb left to fall back to. A fallback would hide exactly the failure this
seam exists to expose, and would risk running a verb twice on a host whose Boat
acted but whose answer was lost. A host whose daemon cannot be reached fails its
verb and says so; the repair is `sync_mirror` + `assert_desired_state`
(spec/33 §2.5), not a second transport.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas._ssh.runner import _elapsed_ms, _finalize, _mark_running
from atlas.atlas.providers.fake_tasks import is_fake_server
from atlas.atlas.task_results import result_line

if TYPE_CHECKING:
	from collections.abc import Callable

	from atlas.atlas.doctype.task.task import Task
	from atlas.atlas.doctype.virtual_machine.virtual_machine import VirtualMachine

# Boat binds the management-tunnel address and /run/boat.sock, never a public
# interface. The port is not yet pinned by the contract, so it is configurable
# and this is only the default.
DEFAULT_BOAT_PORT = 8080

# The API call's own deadline when the caller states none. A Task's
# `timeout_seconds` overrides it, exactly as it bounds an SSH run.
DEFAULT_TIMEOUT_SECONDS = 30

# The Task verbs `_run_verb` routes by hand. `stop` and `rebuild` are the two
# whose request carries more than the operation identifier; `start` keeps its own
# method for the replay contract its docstring states.
START_VERB = "start-vm"
STOP_VERB = "stop-vm"
REBUILD_VERB = "rebuild-vm"
# reserved-ip keeps the Python script's own name (vm-reserved-ip.py), and carries
# its two variables (the action and, on attach, the reserved IP) the way rebuild
# carries its source — so it is routed by hand alongside them, not through
# OPERATION_VERBS whose members carry only the operation identifier.
RESERVED_IP_VERB = "vm-reserved-ip"

# The guest file the in-guest routing client reads (spec/18), and the one place
# Atlas states its content. It travels as one anonymous `extra_env` entry, which
# is the whole of the guest-service seam (spec/33 §7.2): naming a field for what
# a file MEANS would put a service semantic into Boat's vocabulary, so Boat gets
# a path and its bytes and cannot tell this file from any other. The content is
# byte-for-byte what `rootfs.py` writes on the SSH path — the two transports must
# lay down the same guest.
ROUTING_ENVIRONMENT_PATH = "/etc/atlas-routing.env"

# The first fence epoch Atlas issues. Epochs start at 1 (`api/openapi.yaml`,
# `DesiredVirtualMachine.boot_epoch`): Boat gates on whether it holds a fence at
# all, so 0 would read as a fence it holds rather than one it lacks. Atlas is the
# sole issuer, and the epoch bumps at exactly one point — a migration's repoint
# (spec/33 §11.1) — which is not this module's to do.
FIRST_BOOT_EPOCH = 1

# The `Operation` field carrying a verb's TYPED RESULT — the structured answer a
# verb produces beyond its trace, which on the SSH path travels as the script's one
# `ATLAS_RESULT=` JSON line (spec/04, `task_results.parse_result`).
#
# **Not in the contract yet.** `api/openapi.yaml` gives `Operation` only `output`,
# `error` and `exit_code`, so today a Boat verb that computes a result — `sleep`
# computes exactly the boolean `VirtualMachine.sleep` wants — discards it on the way
# out. What Boat needs to add is one optional free-form object on `Operation`:
#
#     result:
#       type: object
#       additionalProperties: true
#       description: The verb's typed result. Same payload the SSH script's one
#                    ATLAS_RESULT= line carries, so a Task reads the same either way.
#
# populated by every verb that has one — today `sleep-vm` with
# `{"memory_snapshot": <SleepResult.MemorySnapshot>}`, and the same field for
# `snapshot-vm` / `warm-snapshot-vm` (`size_bytes`, `memory_bytes`,
# `host_signature`) whenever those verbs move onto Boat.
#
# Atlas is written against the field NOW, so the day it lands, every call site that
# parses a verb's structured output reads it with no second mechanism — see
# `_task_stdout`. Until then a Boat Task simply carries no result line, and the one
# caller that reads one survives its absence.
OPERATION_RESULT_FIELD = "result"

# The statuses an operation stops at. Anything else means it is still running.
TERMINAL_OPERATION_STATUSES = ("Success", "Failure")

# The poll's shape. The per-request timeout is short because each request is a
# small read — the long wait is the loop, bounded by the verb's own timeout —
# and the interval backs off so a fast verb is answered fast without a slow one
# costing a poll a second for its whole length.
POLL_REQUEST_TIMEOUT_SECONDS = 30
POLL_FIRST_INTERVAL_SECONDS = 0.1
POLL_MAX_INTERVAL_SECONDS = 1.0

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


class BoatClient:
	"""One host's Boat daemon over HTTP/JSON.

	One method per endpoint of `api/openapi.yaml`. Every non-2xx and every
	transport error raises `BoatError` carrying the daemon's own error sentence,
	so a caller cannot mistake a failure for a result."""

	def __init__(
		self,
		base_url: str,
		token: str,
		timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
		poll: bool = False,
	):
		self.base_url = base_url.rstrip("/")
		# Underscored and never rendered: the token goes into the Authorization
		# header and nowhere else — not into a log line, not into an exception.
		self._token = token
		self.timeout_seconds = timeout_seconds
		# Ask the daemon to answer a verb with its claim rather than its outcome,
		# and read the outcome from `/ops/{id}`. Off by default because a caller
		# that has nowhere to poll from — a Desk action reading one field back —
		# wants the answer in the response.
		self._poll = poll

	@classmethod
	def for_server(
		cls,
		server_name: str,
		timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
		poll: bool = False,
	) -> "BoatClient":
		"""The client for one Server row, credentials resolved the way every other
		Atlas client resolves them."""
		return cls(
			base_url=base_url_for_server(server_name),
			token=token_for_server(server_name),
			timeout_seconds=timeout_seconds,
			poll=poll,
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

	def rebuild_virtual_machine(
		self,
		uuid: str,
		*,
		operation_id: str,
		image: str | None = None,
		snapshot_device: str | None = None,
		data_snapshot_device: str | None = None,
		identity: dict | None = None,
	) -> dict:
		"""POST /vms/{uuid}/rebuild — lay the root disk down again, keeping the
		VM's identity, addresses and data disk.

		The one verb whose request carries more than an identifier (spec/33 §2.4).
		The source is a choice made at the moment of asking and `identity` is what
		the fresh filesystem must be told about itself, so neither has an answer in
		desired state; the sizes it grows to do, and are read from the store rather
		than sent. The per-VM uid is neither, and Boat reads it off the host.

		`identity` is opaque to Boat and stays opaque here — it is passed through
		as `rebuild_request` built it, and nothing on this path parses a key or
		validates an address (§7.2)."""
		optional = {
			"image": image,
			"snapshot_device": snapshot_device,
			"data_snapshot_device": data_snapshot_device,
			"identity": identity,
		}
		body = {"operation_id": operation_id}
		# An absent field is left out entirely, so the daemon distinguishes "no
		# image named" from "an image named empty".
		body.update({field: value for field, value in optional.items() if value})
		return self._request("POST", f"/vms/{uuid}/rebuild", json=body)

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

	def reserved_ip_virtual_machine(
		self, uuid: str, *, operation_id: str, action: str, reserved_ipv4: str | None = None
	) -> dict:
		"""POST /vms/{uuid}/reserved-ip — attach or detach a Reserved IP's host
		1:1-NAT, live and recorded in the VM's network.env.

		The reserved IP is the one input a caller states — the public identity Atlas
		allocated, neither host state nor desired power — so it rides in the body;
		the guest address and veth the NAT is built around are read off the host, not
		sent. `reserved_ipv4` is omitted for detach, which keys on the guest, so the
		daemon distinguishes a detach from an attach that forgot its address."""
		body = {"operation_id": operation_id, "action": action}
		if reserved_ipv4:
			body["reserved_ipv4"] = reserved_ipv4
		return self._request("POST", f"/vms/{uuid}/reserved-ip", json=body)

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

	def migrate(self, uuid: str, phase: str, *, operation_id: str, params: dict) -> dict:
		"""POST /vms/{uuid}/migrate/{phase} — run one MUTATING phase of the
		cross-host migration saga, returning the operation record (spec/33 §8).

		`operation_id` is the Frappe Task name — the same replay key every verb
		shares, so a retried migration Task returns the phase's first result rather
		than re-running it. `params` are the phase-specific `MigrateRequest` fields,
		the UUID-derived ones (nbd port/slots, tunnel device/port, route table)
		already dropped by the caller because Boat re-derives them from the UUID.
		The poll-only Hydrating phase is `get_migration_hydration`, not this."""
		return self._request(
			"POST", f"/vms/{uuid}/migrate/{phase}", json={"operation_id": operation_id, **params}
		)

	def get_migration_hydration(self, uuid: str, *, clone_device: str | None = None) -> dict:
		"""GET /vms/{uuid}/migrate/hydration — the Hydrating phase's per-tick poll.

		A plain read, deliberately carrying no operation identifier and writing no
		journal record (spec/33 §8): a poll runs every controller tick for the life
		of the copy, so it must not bury the op journal. Returns `hydration_percent`
		(0..100, the MIN across the VM's disk clones) and `source_healthy`.

		`clone_device` polls one named dm device instead of the VM's own clones —
		the base-image ship's `atlas-base-<image>-clone` (spec/24 §5.1) — and is
		omitted for the ordinary disk poll."""
		path = f"/vms/{uuid}/migrate/hydration"
		if clone_device:
			path += f"?clone_device={clone_device}"
		return self._request("GET", path)

	def _request(self, method: str, path: str, json: dict | None = None):
		url = f"{self.base_url}{path}"
		headers = {
			"Authorization": f"Bearer {self._token}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		if self._poll and method == "POST":
			# RFC 7240. The daemon answers with the claim and runs the verb after,
			# and this side reads the outcome from `GET /ops/{operation_id}`. A
			# thirty-minute sync therefore holds no connection for thirty minutes,
			# and a connection dropped mid-verb loses no outcome — the record is
			# on the host either way, which is the whole reason the identifier is
			# the Task name.
			headers["Prefer"] = "respond-async"
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
		try:
			return response.json()
		except ValueError as exception:
			# A 2xx whose body is not JSON is this boundary failing, not the caller's
			# data: a proxy's HTML interception page, a truncated response, a daemon
			# answering a route it does not serve. Decoded outside this try it escaped
			# as a `JSONDecodeError` past every caller that handles `BoatError` — the
			# mirror's freeze among them, which is how a host answering nonsense
			# stayed `Fresh` while a worker logged a traceback nobody reads.
			raise BoatError(
				f"{method} {path} answered {response.status_code} with a non-JSON body"
			) from exception


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
	"terminate-vm": BoatClient.terminate_virtual_machine,
	"resize-vm": BoatClient.resize_virtual_machine,
}


# A rebuild Task's variables, mapped onto the `RebuildRequest` they state. The
# four collections below are the whole translation, and every variable the verb
# sends belongs to exactly one of them. A variable in none of them raises,
# because a silently dropped one is a rootfs laid down without the thing it was
# supposed to carry, and nothing would say so.
#
# The source to lay down. Boat takes the snapshot when both are given.
REBUILD_SOURCES = {
	"SNAPSHOT_ROOTFS_PATH": "snapshot_device",
	"DATA_SNAPSHOT_ROOTFS_PATH": "data_snapshot_device",
	"IMAGE_NAME": "image",
}

# The guest identity, field for field. Every one is written into the fresh
# filesystem verbatim; neither side parses any of it.
REBUILD_IDENTITY = {
	"VIRTUAL_MACHINE_IPV6": "ipv6_address",
	"IPV4_GUEST_CIDR": "ipv4_guest_cidr",
	"IPV4_GATEWAY": "ipv4_gateway",
	"PRIVATE_ADDRESS": "private_address",
	"SSH_PUBLIC_KEY": "authorized_keys_blob",
	"DATA_DISK_MOUNT_AT": "data_disk_mount_at",
}

# Variables the request deliberately does not carry, each because something that
# is not this request already answers it:
#
#   VIRTUAL_MACHINE_NAME              the path names the VM
#   DISK_GB, DATA_DISK_GB             desired state; a rebuild is not a resize
#   DATA_DISK_FORMAT                  a restore never formats — it snapshots a
#                                     filesystem that already exists
#   ATLAS_FC_UID                      the host reads the uid off its own network.env
#   IPV4_HOST_CIDR                    host-side networking, which a rebuild does
#                                     not touch (the SSH script declares it only
#                                     to accept-and-ignore it)
#   ROOTFS_FILENAME                   read by nothing on either transport
REBUILD_ANSWERED_ELSEWHERE = frozenset(
	{
		"VIRTUAL_MACHINE_NAME",
		"DISK_GB",
		"DATA_DISK_GB",
		"DATA_DISK_FORMAT",
		"ATLAS_FC_UID",
		"IPV4_HOST_CIDR",
		"ROOTFS_FILENAME",
	}
)

# Variables that become one guest file each, written verbatim and anonymously.
REBUILD_GUEST_FILES = frozenset({"ROUTING_BASE_URL"})


def rebuild_request(variables: dict) -> dict:
	"""The `RebuildRequest` fields one rebuild Task's variables state.

	Refuses the two ways a rebuild can be meaningless, exactly as Boat's own CLI
	does and for the same reason: with no source there is nothing to lay down, and
	with no authorized keys the VM boots a rootfs carrying the SOURCE's identity —
	it comes up, reports success, and nothing can ever log in to it again.

	A variable this does not name also raises. The failure this seam invites is a
	value that exists on one side and is dropped on the other, and a rebuild is the
	one verb where that drop is invisible until someone tries to reach the VM."""
	unknown = set(variables) - set(REBUILD_SOURCES) - set(REBUILD_IDENTITY)
	unknown -= REBUILD_ANSWERED_ELSEWHERE | REBUILD_GUEST_FILES
	if unknown:
		raise BoatError(
			f"rebuild-vm states {', '.join(sorted(unknown))}, which the rebuild request has no field for"
		)
	request = {field: variables[key] for key, field in REBUILD_SOURCES.items() if variables.get(key)}
	if not request.get("image") and not request.get("snapshot_device"):
		raise BoatError("rebuild-vm states no source: Boat needs an image name or a snapshot device")
	request["identity"] = _guest_identity(variables)
	return request


def _guest_identity(variables: dict) -> dict:
	"""The `GuestIdentity` blob — what makes the fresh rootfs this VM's rather
	than the image's. Absent fields are left out rather than sent empty; Boat
	writes a defined value for each either way."""
	identity = {field: variables[key] for key, field in REBUILD_IDENTITY.items() if variables.get(key)}
	if not identity.get("authorized_keys_blob"):
		raise BoatError("rebuild-vm states no SSH key: the rebuilt VM would have no way back in")
	files = _guest_files(variables)
	if files:
		identity["extra_env"] = files
	return identity


def _guest_files(variables: dict) -> list[dict]:
	"""Every guest file the rebuild lays down, as `{path, content}` pairs.

	One entry today — the routing client's base URL (spec/18) — and it is here
	rather than in a named field precisely so Boat cannot tell it from the next
	one (spec/33 §7.2)."""
	routing_base_url = variables.get("ROUTING_BASE_URL")
	if not routing_base_url:
		return []
	return [{"path": ROUTING_ENVIRONMENT_PATH, "content": f"ATLAS_BASE_URL={routing_base_url}\n"}]


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
		# The host this VM is placed on, so the target Boat can enforce §11.1's
		# "server == self" gate: a host boots a VM only when the record names it.
		"server": virtual_machine.server,
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

	A Fake-backed host is never called, exactly as `run_boat_task` gives it no
	request: there is no daemon there to hold a fence."""
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
	# would go nowhere, so the synthesized Task row stands in for the call.
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
	`_finalize` so the row's shape cannot drift between the two transports.

	The verb is ASKED for and then POLLED for, rather than waited on inside one
	request. Two things follow, and both are the point:

	  - a verb that takes half an hour holds no connection for half an hour, so
	    no proxy, no keep-alive and no worker timeout sits between Atlas and an
	    outcome that is already recorded on the host;
	  - a connection dropped mid-verb costs nothing. The operation is keyed by
	    this Task's own name, so the next poll finds the record whether the verb
	    is still running or long finished — where a broken request used to leave
	    a Task that could never be answered.

	`timeout_seconds` is the deadline for the whole verb, as it always was. It
	bounds the poll rather than one HTTP request."""
	_mark_running(task)
	start = time.monotonic()
	try:
		client = BoatClient.for_server(server, timeout_seconds=POLL_REQUEST_TIMEOUT_SECONDS, poll=True)
		operation = _run_verb(client, script, task.virtual_machine, task.name, variables)
		operation = _await_operation(client, task.name, operation, timeout_seconds)
		status, exit_code = _outcome(operation, task.name)
	except Exception as exception:
		_finalize(task, "", str(exception), None, "Failure", _elapsed_ms(start))
		if isinstance(exception, frappe.ValidationError):
			raise
		raise frappe.ValidationError(str(exception)) from exception

	# Fold the daemon's own record onto the row: its trace becomes the Task's
	# stdout, its one-sentence error the stderr. The operator surface is the
	# same text it has always been.
	error = operation.get("error") or ""
	_finalize(task, _task_stdout(operation), error, exit_code, status, _elapsed_ms(start))
	if status == "Failure":
		frappe.throw(f"Task {task.name} ({script}) exited {exit_code}: {error[-500:]}")


def _run_verb(client: BoatClient, script: str, uuid: str, operation_id: str, variables: dict) -> dict:
	"""Map a Task verb onto its Boat endpoint, passing the Task name as `op_id`.

	The variables dict is the same one the SSH path renders to `--kebab-flags`,
	so a verb's inputs are stated once and mean the same thing on either
	transport. Only start, stop and rebuild read it: every other verb's inputs are
	desired state, which reached the host by `put_desired` before this ran.

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
	if script == REBUILD_VERB:
		return client.rebuild_virtual_machine(uuid, operation_id=operation_id, **rebuild_request(variables))
	if script == RESERVED_IP_VERB:
		return client.reserved_ip_virtual_machine(
			uuid,
			operation_id=operation_id,
			action=variables.get("ACTION", "attach"),
			reserved_ipv4=variables.get("RESERVED_IPV4"),
		)
	call = OPERATION_VERBS.get(script)
	if call:
		return call(client, uuid, operation_id=operation_id)
	served = ", ".join(sorted((START_VERB, STOP_VERB, REBUILD_VERB, RESERVED_IP_VERB, *OPERATION_VERBS)))
	raise BoatError(f"Boat serves no endpoint for verb {script!r} (it serves {served})")


def _task_stdout(operation: dict) -> str:
	"""The Task stdout one operation record implies.

	Boat's `output` is the verb's trace, and that is what an operator reads. A verb
	that ALSO produced a typed result gets it appended as the very `ATLAS_RESULT=`
	line an SSH script would have emitted, so `task_results.parse_result` reads one
	Task the same way whichever transport filled it. That is the whole of what keeps
	`run_boat_task` a twin of `run_task`: a call site that parses its verb's result
	must not have to know which one ran it, and today only `sleep` is routed here —
	the rest hold `run_task` — so this is the seam that stops the next one repeating
	it the day it gains a Boat endpoint.

	No result — which is EVERY operation today, because `OPERATION_RESULT_FIELD` is
	not in the contract yet — leaves the trace alone and a parser finds no line.
	Callers survive that with `parse_optional_result`."""
	output = operation.get("output") or ""
	result = operation.get(OPERATION_RESULT_FIELD)
	if result is None:
		return output
	if output and not output.endswith("\n"):
		output += "\n"
	return output + result_line(result)


def _await_operation(client: BoatClient, operation_id: str, operation: dict, timeout_seconds: int) -> dict:
	"""Poll `GET /ops/{operation_id}` until the operation finishes.

	The daemon answers a verb with its claim, so the record is where the outcome
	appears. The interval backs off to a second: a start settles in well under
	one, an image sync takes minutes, and one poll a second for a long verb is
	nothing next to the SSH session it replaced.

	A deadline reached is a failed Task with a sentence saying what to look at,
	never a guess. The operation is still on the host under this Task's name, so
	a retry of the same Task reads the same record rather than running the verb
	again."""
	deadline = time.monotonic() + timeout_seconds
	interval = POLL_FIRST_INTERVAL_SECONDS
	while operation.get("status") not in TERMINAL_OPERATION_STATUSES:
		if time.monotonic() >= deadline:
			raise BoatError(
				f"Boat operation {operation_id} did not finish within {timeout_seconds}s; "
				f"it is still recorded on the host — GET /ops/{operation_id} has its state"
			)
		time.sleep(interval)
		interval = min(interval * 2, POLL_MAX_INTERVAL_SECONDS)
		operation = client.get_operation(operation_id)
	return operation


def _outcome(operation: dict, operation_id: str) -> tuple[str, int]:
	"""The Task status and exit code an operation record implies.

	A non-terminal status here is a bug in the caller, not a surprise from the
	host: `_await_operation` is what waits, and it only returns a record that has
	finished or raises. Raise rather than guess — a Task marked Success off a
	Running record would be Atlas inventing an outcome."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Migration phases (spec/33 §8, item 9). The cross-host migration saga's host work
# moves off SSH onto Boat one PHASE at a time, exactly as the lifecycle verbs did.
# `run_boat_migration_phase` is `run_boat_task`'s twin for the saga: same Task row,
# same replay-by-operation-id, same Fake shortcut. The two transports differ only
# in the grammar — a lifecycle verb is `POST /vms/{uuid}/{verb}`, a migration phase
# is `POST /vms/{uuid}/migrate/{phase}` — so the mapping below is the whole of the
# translation, the migration analogue of `_run_verb` + the rebuild collections.
#
# Boat DERIVES every per-VM device from the UUID (nbd port = NBDPort(uuid), the nbd
# client slots, the tunnel device/port, the route table), so the matching Atlas
# variables — NBD_PORT, NBD_BASE_SLOT, TUNNEL_DEVICE, TUNNEL_PORT, ROUTE_TABLE — are
# NOT sent: two sides deriving the same value from the same UUID need no wire field,
# and sending one invites a disagreement. VIRTUAL_MACHINE_NAME is likewise dropped
# because the path names the VM.
# ─────────────────────────────────────────────────────────────────────────────

# The Hydrating poll's Atlas verb. Special-cased out of MIGRATION_PHASES because it
# is a GET with no operation record (spec/33 §8), not one of the mutating POST phases.
MIGRATION_POLL_HYDRATION = "migration-poll-hydration"

# migration-inject-identity variable -> GuestIdentity field. Every value is written
# into the mounted rootfs verbatim; Boat parses none of it (spec/33 §7.2). Absent
# here on purpose:
#   VIRTUAL_MACHINE_NAME   the path names the VM
#   CLONE_DEVICE           Boat derives the write device from the UUID
#                          (/dev/mapper/atlas-vm-<uuid>-clone, falling back to the
#                          plain LV — inject_identity.go), so a device sent here
#                          could only disagree with the one it will actually use
#   ROUTING_BASE_URL       becomes one anonymous guest file (extra_env), never a
#                          named field — the guest-service seam, exactly as rebuild
MIGRATION_INJECT_IDENTITY = {
	"VIRTUAL_MACHINE_IPV6": "ipv6_address",
	"IPV4_GUEST_CIDR": "ipv4_guest_cidr",
	"IPV4_GATEWAY": "ipv4_gateway",
	"SSH_PUBLIC_KEY": "authorized_keys_blob",
	"DATA_DISK_MOUNT_AT": "data_disk_mount_at",
}


def _migration_guest_identity(variables: dict) -> dict:
	"""The `GuestIdentity` blob an inject-identity phase writes through the clone.

	Absent fields are left out rather than sent empty (Boat writes a defined value
	for each either way), and DATA_DISK_MOUNT_AT empty means no data mount and no
	fstab line. ROUTING_BASE_URL rides as one anonymous `extra_env` file, reusing
	`_guest_files` so the byte-for-byte content matches the rebuild path's."""
	identity = {
		field: variables[key] for key, field in MIGRATION_INJECT_IDENTITY.items() if variables.get(key)
	}
	files = _guest_files(variables)
	if files:
		identity["extra_env"] = files
	return identity


def _export_source_params(variables: dict) -> dict:
	# The source's own public IPv4 qemu-nbd binds — the one thing the source cannot
	# derive from the UUID. NBD_PORT is dropped (Boat derives it).
	return {"bind_address": variables["BIND_ADDRESS"]}


def _export_base_params(variables: dict) -> dict:
	return {"image_name": variables["IMAGE_NAME"], "bind_address": variables["BIND_ADDRESS"]}


def _clone_target_params(variables: dict) -> dict:
	return {
		"image_name": variables["IMAGE_NAME"],
		"disk_gb": int(variables["DISK_GB"]),
		"data_disk_gb": int(variables["DATA_DISK_GB"]),
		"source_host": variables["SOURCE_HOST"],
	}


def _receive_base_params(variables: dict) -> dict:
	# PHASE (prepare|finalize) is the base-ship's own half, not a UUID-derived value,
	# so it IS sent — as `base_phase`.
	return {
		"image_name": variables["IMAGE_NAME"],
		"disk_gb": int(variables["DISK_GB"]),
		"source_host": variables["SOURCE_HOST"],
		"base_phase": variables["PHASE"],
	}


def _inject_identity_params(variables: dict) -> dict:
	return {"identity": _migration_guest_identity(variables)}


def _collapse_clone_params(variables: dict) -> dict:
	# Whether a second (data) clone must be collapsed too; NBD_BASE_SLOT dropped.
	return {"data_disk_gb": int(variables["DATA_DISK_GB"])}


def _forward_up_params(variables: dict) -> dict:
	# ROLE is required; SOURCE_HOST rides on the target role only (the connector's
	# dial address); VIRTUAL_MACHINE_IPV6 is optional — forward-up runs once bare
	# before the routes are known and again at cutover once they are. TUNNEL_DEVICE,
	# TUNNEL_PORT and ROUTE_TABLE are all UUID-derived and dropped.
	params = {"role": variables["ROLE"]}
	if variables.get("SOURCE_HOST"):
		params["source_host"] = variables["SOURCE_HOST"]
	if variables.get("VIRTUAL_MACHINE_IPV6"):
		params["virtual_machine_ipv6"] = variables["VIRTUAL_MACHINE_IPV6"]
	return params


def _source_forward_params(variables: dict) -> dict:
	# REASSERT_PROXY_NDP is NOT sent: Boat's source-forward re-asserts proxy-NDP
	# UNCONDITIONALLY on every provider (forward.go), so the flag has no field — it
	# is the always-on behaviour, not a choice. TUNNEL_DEVICE is UUID-derived.
	return {"virtual_machine_ipv6": variables["VIRTUAL_MACHINE_IPV6"]}


def _target_receive_params(variables: dict) -> dict:
	return {"virtual_machine_ipv6": variables["VIRTUAL_MACHINE_IPV6"]}


def _forward_down_params(variables: dict) -> dict:
	# DEASSERT_PROXY_NDP is likewise unconditional in Boat's forward-down, so only
	# the role and the /128 are sent. Driven by migration.collapse_forward through
	# run_boat_migration_phase (the operator-initiated teardown, outside the phase
	# machine), not _run_phase_task's self-driving saga.
	return {"role": variables["ROLE"], "virtual_machine_ipv6": variables["VIRTUAL_MACHINE_IPV6"]}


def _withdraw_private_params(variables: dict) -> dict:
	# The /128 to withdraw from the source's local-ownership cache; empty is a clean
	# no-op for a tenant-less VM, so the empty string is passed through as-is.
	return {"private_address": variables.get("PRIVATE_ADDRESS", "")}


def _source_autostart_params(variables: dict) -> dict:
	# Whether the source VM's systemd unit starts itself on the next host reboot.
	# ENABLED="0" (what Pending sends) takes the unit out of multi-user.target so the
	# source stays Stopped from Pending until Cleanup — a plain `systemctl stop` does
	# not survive a host reboot, which cold-booted a second live copy of the guest
	# (spec/24 §3). Boat's MigrateRequest carries an `enabled` bool; the Atlas variable
	# is the string "1"/"0", so it is mapped to the boolean here. VIRTUAL_MACHINE_NAME
	# is the uuid the path already names, so it is dropped.
	return {"enabled": variables.get("ENABLED") == "1"}


def _cleanup_source_params(variables: dict) -> dict:
	# nbd_pid is the qemu-nbd Boat must reap (NBD_PORT is UUID-derived and dropped).
	# keep_address carries the ingress-teardown suppression the keep-address path needs:
	# Atlas passes KEEP_ADDRESS=1 so Boat's cleanup-source LEAVES the proxy-NDP entry and
	# the nft forward rules migration-source-forward installed, instead of running the
	# full vm-network-down that would delete them and black-hole the migrated tenant's
	# public ingress (spec/33 §8). Boat's MigrateRequest carries a `keep_address` bool;
	# the Atlas variable is the string "1"/"0", so it is mapped to the boolean here.
	return {
		"nbd_pid": int(variables.get("NBD_PID") or 0),
		"keep_address": variables.get("KEEP_ADDRESS") == "1",
	}


# Atlas migration verb -> (Boat phase string, `variables -> body params` builder).
# The 13 mutating phases Boat's MigrateVirtualMachine serves (api/migration.go); the
# poll-only Hydrating phase is MIGRATION_POLL_HYDRATION above. A verb not in this map
# raises rather than appearing to have run — the migration analogue of `_run_verb`'s
# "Boat serves no endpoint" refusal.
MIGRATION_PHASES = {
	# Pending: toggle the source unit's reboot-autostart. Driven directly by
	# _disable_source_autostart (outside the phase machine), not _run_phase_task.
	"migration-source-autostart": ("source-autostart", _source_autostart_params),
	"migration-export-source": ("export-source", _export_source_params),
	"migration-export-base": ("export-base", _export_base_params),
	"migration-clone-target": ("clone-target", _clone_target_params),
	"migration-receive-base": ("receive-base", _receive_base_params),
	"migration-inject-identity": ("inject-identity", _inject_identity_params),
	# The collapse script keeps its Atlas name (migration-cutover-target.py); Boat
	# calls the phase collapse-clone.
	"migration-cutover-target": ("collapse-clone", _collapse_clone_params),
	"migration-forward-up": ("forward-up", _forward_up_params),
	"migration-source-forward": ("source-forward", _source_forward_params),
	"migration-target-receive": ("target-receive", _target_receive_params),
	"migration-forward-down": ("forward-down", _forward_down_params),
	"migration-withdraw-private-source": ("withdraw-private", _withdraw_private_params),
	"migration-cleanup-source": ("cleanup-source", _cleanup_source_params),
}


def run_boat_migration_phase(
	*,
	script: str,
	variables: dict,
	server: str,
	virtual_machine: str | None = None,
	timeout_seconds: int = 1800,
) -> "Task":
	"""Run one migration saga phase through the host's Boat daemon, recording the
	Task row `run_task` would have recorded — `run_boat_task`'s twin for the phases
	(spec/33 §8, item 9).

	Keyword-for-keyword `run_task`'s signature, so `migration._run_phase_task` calls
	it exactly as it called `run_task`. The Hydrating poll (`migration-poll-hydration`)
	is a GET whose reading is folded onto the row as the one `ATLAS_RESULT=` line the
	SSH poll script emitted, so `_phase_hydrating` parses it unchanged; the 12 mutating
	phases are POST-claimed and polled like every other verb. Raises
	`frappe.ValidationError` on any failure, with the Task saved first."""
	# Fake provider (developer_mode): a Task on a Fake-backed Server succeeds (or
	# fails on demand) with no Boat call, exactly as run_boat_task/run_task give it no
	# request. The dev/test fleet is Fake hosts, so a real socket would go nowhere —
	# the synthesized Task row (with its ATLAS_RESULT for the phases that return one)
	# stands in, which is what keeps the Fake↔Fake migration tests transport-real.
	if is_fake_server(server):
		from atlas.atlas.providers.fake_tasks import run_fake_task

		return run_fake_task(server, script, variables, virtual_machine)

	if not virtual_machine:
		frappe.throw(
			_("run_boat_migration_phase: {0} needs a virtual_machine — Boat addresses VMs by UUID").format(
				script
			)
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

	_execute_migration_phase_on_boat(task, server, script, variables, timeout_seconds)
	return task


def _execute_migration_phase_on_boat(
	task: "Task",
	server: str,
	script: str,
	variables: dict,
	timeout_seconds: int,
) -> None:
	"""Drive an inserted migration-phase Task to its outcome over Boat. Mirrors
	`_execute_on_boat` step for step — same `_mark_running` / `_finalize` so the row
	shape cannot drift between the lifecycle and migration transports — and forks only
	on the one structural difference: the Hydrating poll is a GET with no operation
	record, so it takes no claim and no poll loop.

	Both branches end at the same `_finalize`: the mutating phases fold the operation's
	trace + typed result the way every verb does (`_task_stdout`), and the poll folds
	its `hydration_percent`/`source_healthy` reading onto the row as the same
	`ATLAS_RESULT=` line the SSH poll script wrote, so the controller reads one Task
	the same way whichever transport filled it."""
	_mark_running(task)
	start = time.monotonic()
	try:
		client = BoatClient.for_server(server, timeout_seconds=POLL_REQUEST_TIMEOUT_SECONDS, poll=True)
		if script == MIGRATION_POLL_HYDRATION:
			hydration = client.get_migration_hydration(
				task.virtual_machine, clone_device=variables.get("CLONE_DEVICE")
			)
			result = {
				"hydration_percent": hydration["hydration_percent"],
				"source_healthy": hydration["source_healthy"],
			}
			# Reuse _task_stdout with an operation-shaped payload so the row's stdout is
			# assembled by the one place that owns the ATLAS_RESULT= fold.
			stdout = _task_stdout({OPERATION_RESULT_FIELD: result})
			status, exit_code, error = "Success", 0, ""
		else:
			phase, build_params = _migration_phase(script)
			operation = client.migrate(
				task.virtual_machine, phase, operation_id=task.name, params=build_params(variables)
			)
			operation = _await_operation(client, task.name, operation, timeout_seconds)
			status, exit_code = _outcome(operation, task.name)
			stdout = _task_stdout(operation)
			error = operation.get("error") or ""
	except Exception as exception:
		_finalize(task, "", str(exception), None, "Failure", _elapsed_ms(start))
		if isinstance(exception, frappe.ValidationError):
			raise
		raise frappe.ValidationError(str(exception)) from exception

	_finalize(task, stdout, error, exit_code, status, _elapsed_ms(start))
	if status == "Failure":
		frappe.throw(f"Task {task.name} ({script}) exited {exit_code}: {error[-500:]}")


def _migration_phase(script: str) -> "tuple[str, Callable[[dict], dict]]":
	"""The Boat phase string and body builder for one Atlas migration verb.

	An unmapped verb raises. A phase Boat cannot run must fail loud rather than appear
	to have run — the same discipline `_run_verb` keeps for the lifecycle verbs."""
	mapping = MIGRATION_PHASES.get(script)
	if mapping is None:
		served = ", ".join(sorted(MIGRATION_PHASES))
		raise BoatError(
			f"Boat serves no migration phase for verb {script!r} "
			f"(it serves {served}, plus {MIGRATION_POLL_HYDRATION})"
		)
	return mapping
