import uuid

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import vm_images, vm_provisioning, vm_resize, vm_teardown
from atlas.atlas.boat_client import (
	FIRST_BOOT_EPOCH,
	BoatError,
	put_desired_state,
	run_boat_task,
)
from atlas.atlas.networking import (
	CPU_MODE_RELAXED,
	allocate_ipv6,
	derive_mac,
	derive_private_address,
	derive_tap,
	derive_uid,
)
from atlas.atlas.placement import apply_user_defaults
from atlas.atlas.ssh import run_probe, run_task
from atlas.atlas.task_results import parse_optional_result, parse_result

# Never change after insert — identity and the key the rootfs was built with.
IMMUTABLE_AFTER_INSERT = (
	"title",
	"server",
	"image",
	"ssh_public_key",
	"tenant",
)

# Frozen on ordinary saves (drift protection: the on-host VM must match the
# doc) but mutable through resize() on a Stopped VM, which rewrites the
# firecracker config and grows the disk to match. The resize() path sets
# `flags.resizing` so validate() lets these through.
RESIZE_MUTABLE = (
	"vcpus",
	"cpu_max_cores",
	"cpu_mode",
	"memory_megabytes",
	"disk_gigabytes",
	"data_disk_gigabytes",
)

# The desired power a status implies, for a VM Atlas has never stated one for —
# which is every VM in the fleet until its first Boat-routed verb. Sleeping
# implies Running: a parked VM wakes on traffic, it is not powered off (spec/32,
# and `boat_mirror._power_of` reads the observed side of the same rule). Every
# status that is not a live machine implies Stopped, so an unfinished or failed
# VM is never stated as one a host should be booting.
DESIRED_POWER_FOR_STATUS = {
	"Running": "Running",
	"Paused": "Running",
	"Sleeping": "Running",
}

# The one field a migration cutover is allowed to repoint, and nothing else may.
# `server` is otherwise immutable (identity + the key the rootfs was built with);
# migration is the single sanctioned path that moves a VM between hosts, gated by
# `flags.migrating` in validate() exactly as resize() gates RESIZE_MUTABLE.
# `ipv6_address` is not in IMMUTABLE_AFTER_INSERT, so it needs no gate — the
# change-address cutover rewrites it on an ordinary save. (spec/24 §1)
MIGRATE_MUTABLE = ("server",)


class VirtualMachine(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		boot_epoch: DF.Int
		build_mode: DF.Literal["", "site", "admin"]
		clone_source_data_rootfs: DF.Data | None
		clone_source_rootfs: DF.Data | None
		cpu_max_cores: DF.Float
		cpu_mode: DF.Literal["Hard cap", "Relaxed"]
		data_disk_format_and_mount: DF.Check
		data_disk_gigabytes: DF.Int
		data_disk_mount_point: DF.Data | None
		desired_power: DF.Literal["", "Running", "Stopped"]
		disk_gigabytes: DF.Int
		has_memory_snapshot: DF.Check
		idle_timeout_seconds: DF.Int
		image: DF.Link
		ipv6_address: DF.Data | None
		is_gateway: DF.Check
		is_proxy: DF.Check
		last_started: DF.Datetime | None
		last_stopped: DF.Datetime | None
		last_traffic_at: DF.Datetime | None
		mac_address: DF.Data | None
		memory_megabytes: DF.Int
		memory_snapshot_on_stop: DF.Check
		observed_status: DF.Literal["", "Running", "Stopped", "Sleeping", "Unknown", "Failed"]
		public_ipv4: DF.Data | None
		server: DF.Link
		pilot_credential_id: DF.Data | None
		size_preset: DF.Literal["Custom", "Shared 1x", "Shared 2x", "Shared 4x", "Shared 8x", "Dedicated 1x"]
		sleep_on_idle: DF.Check
		ssh_public_key: DF.LongText
		status: DF.Literal["Pending", "Running", "Paused", "Stopped", "Sleeping", "Failed", "Terminated"]
		stop_protection: DF.Check
		tap_device: DF.Data | None
		tenant: DF.Link | None
		termination_protection: DF.Check
		title: DF.Data
		traffic_forwarded_from: DF.Link | None
		traffic_forwarded_since: DF.Datetime | None
		vcpus: DF.Int
		warm_snapshot: DF.Link | None
	# end: auto-generated types

	@property
	def ssh_command(self) -> str:
		if not self.ipv6_address:
			return ""
		return f"ssh root@{self.ipv6_address}"

	@ssh_command.setter
	def ssh_command(self, _value: object) -> None:
		# Virtual field: ignore writes. Frappe's hydrate path setattrs every
		# field on the doc when loading from the form; the value is derived
		# from ipv6_address.
		pass

	def autoname(self) -> None:
		# autoname() runs from set_new_name(), called by Document.insert()
		# after before_insert(). Dependent fields are derived in
		# before_validate(), which runs after set_new_name.
		self.name = str(uuid.uuid4())

	def before_insert(self) -> None:
		# A dashboard user creates a VM with no server/image; fill them before
		# anything that depends on server (ipv6 allocation derives from it).
		# No-op for the operator path, which supplies both. See placement.py.
		apply_user_defaults(self)
		self.set_build_mode_default()
		self.set_status_default()
		self.set_ipv6_address()

	def after_insert(self) -> None:
		"""Auto-provision: enqueue the provision job so the operator never
		has to click `Provision` on a freshly-created Pending VM.

		enqueue_after_commit so the worker only starts once this insert's
		transaction has committed — otherwise auto_provision can look up the VM
		before the row exists ("Virtual Machine ... not found")."""
		frappe.enqueue(
			"atlas.atlas.doctype.virtual_machine.virtual_machine.auto_provision",
			queue="long",
			timeout=300,
			enqueue_after_commit=True,
			virtual_machine_name=self.name,
		)

	def before_validate(self) -> None:
		if not self.is_new():
			return
		self.set_cpu_defaults()
		self.set_mac_address()
		self.set_tap_device()
		self.set_private_address()
		self.validate_dark_vm_has_identity()
		self.validate_infra_role()

	def validate_infra_role(self) -> None:
		"""A VM is at most ONE infra role. The proxy fronts public subdomains; the gateway
		terminates customer WireGuard peers (spec/26). They carry different images, different
		reconcile paths (proxy map vs. wg0 peers), and would collide on the one attached
		reserved IPv4 — so a single VM can't be both."""
		if self.is_proxy and self.is_gateway:
			frappe.throw(_("A VM cannot be both a proxy and a customer gateway"))

	def validate_dark_vm_has_identity(self) -> None:
		"""A dark VM (public_networking=0, §6) has NO public /128, so its ONLY identity is
		the private fdaa:: address — which requires a tenant (the /48 the address derives
		from). Reject a tenant-less dark VM at insert: it would have no address at all, and
		vm_provisioning.ipv4_link_variables would have no private address to index its NAT44 /30 off. The
		design's §6 invariant: public_networking=0 ⟹ private addressing forced on."""
		if not self.public_networking and not self.tenant:
			frappe.throw(
				_(
					"A dark VM (Public Networking off) needs a Tenant — its only identity is the private address"
				)
			)

	def set_cpu_defaults(self) -> None:
		# cpu_max_cores is the VM's guaranteed CPU bandwidth share; vcpus is the
		# guest thread count. A caller who sets only vcpus (the operator desk path,
		# the bootstrap seed, direct API) wants whole-core bandwidth — default the
		# share to vcpus so those VMs behave exactly as before this field existed.
		# The size presets set it explicitly (fractional shares for sub-1 sizes).
		if not self.cpu_max_cores:
			self.cpu_max_cores = float(self.vcpus or 1)
		# cpu_mode picks how that share is enforced. Default to the relaxed
		# cpu.weight floor + burst ceiling — VMs get their guaranteed share under
		# contention but burst into spare host CPU when it's idle — for any caller
		# that does not opt into the hard-cap model. The JSON default covers the
		# form path; this covers direct API/test construction.
		if not self.cpu_mode:
			self.cpu_mode = CPU_MODE_RELAXED

	def set_build_mode_default(self) -> None:
		"""Inherit the bench bake mode from the base image when the caller didn't set
		one. A promoted bench golden carries build_mode (admin/site); a VM created from
		it via the ordinary `image` field should map its FQDN the same way the golden was
		baked, without the caller having to restate the mode. Only fills an unset value,
		so the recipe-stamped build VM (image_build) and snapshot clones — which set
		build_mode explicitly — are untouched, and an ordinary base image (no mode) leaves
		it empty (→ site, the harmless default everywhere it is read). See spec/08."""
		if self.build_mode or not self.image:
			return
		self.build_mode = frappe.db.get_value("Virtual Machine Image", self.image, "build_mode") or None

	def set_status_default(self) -> None:
		if not self.status:
			self.status = "Pending"

	def set_ipv6_address(self) -> None:
		# A dark VM (public_networking=0, §6) has NO public /128 — its only identity is
		# the private fdaa:: address (set in before_validate). Skip allocation so it does
		# not consume a scarce DO /124 slot. public_networking defaults to 1, so every
		# ordinary VM allocates exactly as before.
		if not self.public_networking:
			return
		if not self.ipv6_address:
			self.ipv6_address = allocate_ipv6(self.server)

	def set_private_address(self) -> None:
		"""Denormalize the VM's private-plane /128 (§8). Derived, not allocated — a pure
		function of (tenant, VM UUID), so it survives migration byte-for-byte and the
		field is just a legible read-through (the source of truth is
		derive_private_address). Empty when the VM has no tenant (operator-created): such
		a VM has no derivable /48, so it stays off the private plane entirely."""
		if self.tenant and not self.private_address:
			self.private_address = derive_private_address(self.tenant, self.name)

	def set_mac_address(self) -> None:
		if not self.mac_address:
			self.mac_address = derive_mac(self.name)

	def set_tap_device(self) -> None:
		if not self.tap_device:
			self.tap_device = derive_tap(self.name)

	def validate(self) -> None:
		# Role exclusivity holds for every save, not just insert — a later db-flip of
		# is_gateway on a live proxy (or vice versa) is caught here too.
		self.validate_infra_role()
		if self.sleep_on_idle and (not self.idle_timeout_seconds or self.idle_timeout_seconds < 120):
			frappe.throw(_("idle_timeout_seconds must be at least 120 when sleep_on_idle is enabled"))
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		guarded = IMMUTABLE_AFTER_INSERT
		if not self.flags.resizing:
			# Outside resize(), the resource fields are frozen too.
			guarded = guarded + RESIZE_MUTABLE
		if self.flags.migrating:
			# The cutover commits `server` (the host move already happened on-host);
			# let exactly that through. Everything else stays frozen.
			guarded = tuple(f for f in guarded if f not in MIGRATE_MUTABLE)
		for field in guarded:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def provision(self) -> str:
		if self.status not in ("Pending", "Failed"):
			frappe.throw(f"Cannot provision from {self.status}")
		task = run_task(
			server=self.server,
			script="provision-vm",
			variables=self._provision_variables(),
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		# Seed the idle clock (spec/32) so a freshly provisioned sleep_on_idle VM is
		# measured from now, not from a null that only the first traffic poll fills.
		self.last_traffic_at = self.last_started
		self.save()
		# Enrolment (spec/33 §11.1). Provision is where a VM comes to exist on a
		# host, so it is where Atlas first issues its fence: a Boat refuses to boot
		# a UUID it holds no fence for, and an unfenced VM would stay down through
		# the host's next reboot.
		self._enrol_after_provision()
		# The VM's private /128 is locally owned by this host; atlas-networkd's
		# periodic scan (spec/31 §11) detects it via the local-ownership cache
		# (vm-network-up.py writes it on success) and gossips the advertisement.
		# No controller-side reconcile — the mesh is self-healing.
		return task.name

	@frappe.whitelist()
	def migrate(self, target_server: str, release_reserved_ip: bool = False) -> str:
		"""Begin migrating this VM's disk to `target_server`, keeping its identity
		(UUID and everything derived from it). Returns the Virtual Machine Migration
		row name; `start_migration` (enqueued below) then drives it phase by phase
		back-to-back, with the `reconcile_migrations` cron as the idempotent, resumable
		safety net (spec/24).

		Cold migration: the VM is stopped during cutover. On the change-address path
		(stage 1) it gets a NEW public IPv6 on the target and the proxy/Subdomain
		layer is re-pointed. Pre-flight (the cheap synchronous half) runs here; the
		on-host checks that need SSH run in the first phase."""
		from atlas.atlas.migration import preflight_checks  # local import: avoids a cycle

		# frm.call / REST send a stringy bool.
		release_reserved_ip = release_reserved_ip in (True, 1, "1", "true", "True", "yes")

		preflight_checks(self, target_server, release_reserved_ip)

		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": self.name,
				"source_server": self.server,
				"target_server": target_server,
				"release_reserved_ip": 1 if release_reserved_ip else 0,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		# Drive the migration now instead of waiting for the reconcile_migrations
		# cron: start_migration runs the first phase and chains each subsequent step
		# (including each Hydrating poll) as soon as it completes, so the migration
		# walks its phases back-to-back and self-paces the long copy to 100% on its
		# own. enqueue_after_commit so the worker only starts once this insert has
		# committed (else start_migration can't load the row). The cron is the safety
		# net that re-drives the row if a self-drive job is ever dropped.
		frappe.enqueue(
			"atlas.atlas.migration.start_migration",
			queue="long",
			timeout=300,
			enqueue_after_commit=True,
			name=migration.name,
		)
		return migration.name

	@frappe.whitelist()
	def collapse_forward(self) -> None:
		"""Tear down this VM's keep-address forward and fall back to change-address
		(spec/24 §2.9.5). Only meaningful for a VM whose traffic is still forwarded
		from another host (set after a keep-address migration); the source host keeps
		egressing the VM's /128 until this runs. The VM gets a NEW /128 on its
		current host, the Subdomains re-point, and the cross-host tunnel is removed.

		Guarded against a concurrent migration (the phase machine owns the host while
		it runs). The heavy lifting — host teardown on both ends, re-provision,
		re-point — lives in migration.collapse_forward."""
		from atlas.atlas.migration import collapse_forward

		if not self.traffic_forwarded_from:
			frappe.throw(_("Virtual Machine {0} has no active forward to collapse").format(self.name))
		self._guard_no_active_migration()
		collapse_forward(self)

	def _guard_no_active_migration(self) -> None:
		"""Throw if a non-terminal migration exists for this VM. The migration phase
		machine owns every host operation while it runs; a concurrent lifecycle action
		would race it against the wrong (stale) server. The migration's own internal
		saves set `flags.migrating`, which exempts them from this guard."""
		if self.flags.migrating:
			return
		from atlas.atlas.doctype.virtual_machine_migration.virtual_machine_migration import (
			active_migration_for,
		)

		migration = active_migration_for(self.name)
		if migration:
			frappe.throw(
				_(
					"Virtual Machine {0} has an in-flight migration ({1}); wait for it to finish or fail"
				).format(self.name, migration)
			)

	def _transport(self, desired_power: str | None = None, **spec):
		"""The transport this lifecycle verb runs over and, on a Boat host, the
		desired state it acts on — stated before Boat is asked to act.

		WO-2's whole shape in one line at each call site (spec/33 §11.3): a verb
		mutates desired state and the host's per-VM reconciler drives observed
		toward it, so the PUT is the mutation and the verb that follows only says
		"now". Re-stating the whole spec before every verb costs one idempotent
		round trip and buys three things: a VM provisioned before Boat existed is
		fenced by its first verb, a Boat that lost its store is re-armed by the
		next one, and no verb can act on a spec its host never received.

		Every host is on Boat, so there is no second transport to choose between:
		the intent is stated and the verb goes to the daemon. The `boat_enabled`
		flag that used to pick between them is gone — a per-host rollback to SSH
		was the right shape while the port was in progress and is the wrong shape
		now that the SSH verbs it fell back to have been deleted."""
		self._put_desired_state(desired_power, **spec)
		return run_boat_task

	def _enrol_after_provision(self) -> None:
		"""State the fence for a VM that is ALREADY RUNNING, without letting a
		refusal undo the record of it.

		Every other verb states intent *before* its task, so a refused PUT means
		nothing ran. Provision is the one place that order is inverted: the VM has
		already been created and booted by `provision-vm`, and `self.save()` above
		is not yet committed. Letting the throw out would roll the row back to
		Pending while the guest is live on the host — and Pending is an allowed
		source state, so the operator's natural retry provisions a *second* VM for
		the same UUID.

		So this records the failure instead of raising. An unfenced VM is a VM
		that will not come back after its host's next reboot, which is bad; a
		live VM that Atlas has forgotten is worse. The next verb re-states the
		fence — every one of them PUTs first — and `assert_desired_state` is the
		explicit repair."""
		try:
			self._put_desired_state("Running")
		except Exception:
			frappe.log_error(
				title=f"Boat enrolment failed for {self.name}",
				message=frappe.get_traceback(),
			)

	def _put_desired_state(self, desired_power: str | None = None, **spec) -> dict:
		"""Record this VM's intent on the row, then state it on its host's Boat.

		Both halves, in that order, because the row is the issuer's record. Atlas
		is the fence epoch's sole issuer and a Boat refuses to boot a UUID it holds
		no fence for (spec/33 §11.1), so a VM that has never been fenced is issued
		epoch 1 here and its host learns that epoch from this PUT. The epoch bumps
		at exactly one point — a migration's repoint — and this is not it, so an
		epoch the row already carries is re-stated unchanged."""
		values = {"desired_power": desired_power or self._desired_power()}
		if not self.boot_epoch:
			values["boot_epoch"] = FIRST_BOOT_EPOCH
		self.db_set(values, update_modified=False)
		try:
			return put_desired_state(self, **spec)
		except BoatError as error:
			# Loud, and the verb never runs: a host that did not take the intent
			# would either refuse the verb for want of a fence or converge back to
			# the state Atlas just tried to leave.
			frappe.throw(_("Boat on {0} refused this VM's desired state: {1}").format(self.server, error))

	def _desired_power(self) -> str:
		"""The power the row already states, or the one its status implies for a
		VM Atlas has never stated one for."""
		return self.desired_power or DESIRED_POWER_FOR_STATUS.get(self.status, "Stopped")

	@frappe.whitelist()
	def assert_desired_state(self) -> dict:
		"""Re-state this VM's desired spec, fence epoch included, on its host's
		Boat. Changes nothing when the host already holds it.

		The operator's half of the resync pair (spec/33 §2.5): `sync_mirror` pulls
		the host's fact, this pushes Atlas's intent, and the two together restore a
		host from any state. It is also the recovery for the one window this seam
		has — an HTTP PUT is not part of the Frappe transaction, so a lifecycle
		call that states intent and then fails leaves the host holding an intent
		the rolled-back row no longer records."""
		return self._put_desired_state()

	@frappe.whitelist()
	def start(self) -> str:
		"""Start a Stopped VM. When the last stop captured a memory snapshot
		(has_memory_snapshot), the host resumes the guest from it in milliseconds
		instead of cold-booting; the start Task is the same either way — the
		launcher and the unit's vm-restore.py hook decide from the on-host marker.
		The snapshot is consumed by the start (restored or not), so the flag
		clears here unconditionally.

		A Sleeping VM is woken instead of started — Desk's Start button works for
		both states transparently."""
		if self.status == "Sleeping":
			return self.wake()
		if self.status != "Stopped":
			frappe.throw(f"Cannot start from {self.status}")
		self._guard_no_active_migration()
		run = self._transport("Running")
		task = run(
			server=self.server,
			script="start-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.has_memory_snapshot = 0
		self.last_started = frappe.utils.now_datetime()
		# Treat the start itself as activity (spec/32). Without this a sleep_on_idle
		# VM carries the last_traffic_at it had before it stopped — already older
		# than idle_timeout_seconds — and the next sleep_idle_vms tick puts it
		# straight back to sleep, within a minute of the operator starting it.
		self.last_traffic_at = self.last_started
		self.save()
		return task.name

	@frappe.whitelist()
	def stop(
		self,
		memory_snapshot: bool | None = None,
		stop_timeout_seconds: int = 0,
		graceful: bool = True,
	) -> str:
		"""Stop a Running/Paused VM. The default is the plain unit stop. With
		`memory_snapshot` (default: the VM's memory_snapshot_on_stop flag, off
		unless the operator opted in), the stop Task first captures the guest's
		full memory state so the next Start resumes it in milliseconds; on any
		snapshot failure the Task falls back to the plain stop on its own — the
		VM always ends up Stopped, only the next Start's speed differs.
		has_memory_snapshot records which way it went.

		`graceful` (default True) sends the guest a ctrl+alt+del first so its kernel
		syncs filesystems and unmounts before the unit is stopped; `graceful=False`
		is the forced kill (Firecracker SIGKILLed with the guest never told to halt —
		dirty guest page cache is lost). Forced is for callers that discard the RAM
		anyway (migration cold-stop) or capture the disk another way. Only applies to
		the plain (non-snapshot) stop; the snapshot path pauses+dumps RAM instead.

		`stop_timeout_seconds` (>0) bounds the graceful drain via a runtime
		TimeoutStopSec override (ExecStopPost still fires) — the migration fast-stop
		path passes it, since a cold migration discards the guest's RAM anyway
		(spec/24 §0.5.2). It only applies to the plain (non-snapshot) stop."""
		# A Paused VM's unit is still active (vCPUs frozen, not shut down), so
		# `systemctl stop` is the correct full shutdown from either state.
		# A Sleeping VM's unit is already stopped — wake it first to get it
		# Running, then call stop() normally.
		if self.status == "Sleeping":
			frappe.throw(_("VM is sleeping — wake it first, then stop"))
		if self.status not in ("Running", "Paused"):
			frappe.throw(f"Cannot stop from {self.status}")
		self._guard_no_active_migration()
		if self.stop_protection:
			frappe.throw(_("Disable stop protection before stopping this VM"))
		if memory_snapshot is None:
			memory_snapshot = bool(self.memory_snapshot_on_stop)
		# frm.call / REST send a JSON/stringy value; normalize to bool.
		memory_snapshot = memory_snapshot in (True, 1, "1", "true", "True", "yes")
		snapshotted = False
		if memory_snapshot:
			# Boat serves no snapshot-stop verb, so this one keeps its SSH path.
			# The intent has to be stated first: a reconciler whose desired power
			# still read Running would boot the VM again the moment the
			# snapshot-stop brought its unit down. Stating it first lets the
			# reconciler race the verb to the same Stopped end state, and the worst
			# that costs is the memory snapshot — which this verb is already
			# allowed to fall back from.
			self._put_desired_state("Stopped")
			# The memory dump is RAM-sized; give it disk-write time, not the
			# 30s a plain systemctl stop needs.
			task = run_task(
				server=self.server,
				script="snapshot-stop-vm",
				variables={
					"VIRTUAL_MACHINE_NAME": self.name,
					"ATLAS_FC_UID": str(derive_uid(self.name)),
				},
				virtual_machine=self.name,
				timeout_seconds=120,
			)
			snapshotted = bool(parse_result(task.stdout)["memory_snapshot"])
		else:
			# frm.call / REST send a JSON/stringy value; normalize to bool.
			graceful = graceful in (True, 1, "1", "true", "True", "yes")
			variables = {"VIRTUAL_MACHINE_NAME": self.name, "GRACEFUL": "1" if graceful else "0"}
			if stop_timeout_seconds > 0:
				variables["STOP_TIMEOUT_SECONDS"] = str(stop_timeout_seconds)
			# Stopped is the intent a stop states, and it is the one that outranks
			# the wake trap: from here the host will not bring this VM back for
			# traffic (spec/33 §11.3).
			run = self._transport("Stopped")
			# Boat's stop-vm waits up to its graceful-shutdown drain (30s in
			# internal/vm/stop.go) for the guest to power off after SendCtrlAltDel
			# BEFORE it stops the unit, so the op routinely runs ~30s+ on a guest
			# that doesn't ACPI-poweroff instantly. A 30s controller wait is shorter
			# than the drain it is waiting on and times out on the host's own
			# success. Wait past the drain + the unit stop, matching the
			# memory-snapshot stop path above. A custom STOP_TIMEOUT_SECONDS bounds
			# the drain, so honor it plus margin.
			op_timeout = max(120, (stop_timeout_seconds or 0) + 60)
			task = run(
				server=self.server,
				script="stop-vm",
				variables=variables,
				virtual_machine=self.name,
				timeout_seconds=op_timeout,
			)
		self.status = "Stopped"
		self.has_memory_snapshot = 1 if snapshotted else 0
		self.last_stopped = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def sleep(self) -> str:
		"""Put a Running VM to sleep: memory snapshot on the host + SLEEPING marker
		file that suppresses systemd auto-start on host reboot. The VM's cgroup is
		released, freeing its RAM on the host — that is the whole point.

		Falls back to a plain stop if the snapshot fails (launcher too old, not
		enough disk, etc.) — the VM always ends up Sleeping; only the next wake's
		speed differs. sleep_on_idle must be enabled on the VM."""
		if not self.sleep_on_idle:
			frappe.throw(_("Enable sleep_on_idle before putting this VM to sleep"))
		if self.status != "Running":
			frappe.throw(f"Cannot sleep from {self.status}")
		self._guard_no_active_migration()
		if self.stop_protection:
			frappe.throw(_("Disable stop protection before sleeping this VM"))
		if self.desired_power == "Stopped":
			# The precedence rule from the enrolment side (spec/33 §11.3). Sleeping
			# is a Running VM's low-power state — the address stays live and the
			# first SYN brings it back — so parking a VM that was told to stop
			# would arm exactly the resurrection the rule forbids.
			#
			# This read used to be gated on `boat_enabled`, because a VM left
			# stating Stopped on a rolled-back host could never sleep again —
			# silently, since the idle sweeper swallows this throw. With the flag
			# gone the field is authoritative everywhere and the guard is simply
			# the rule.
			frappe.throw(_("VM is stopped by intent — start it before putting it to sleep"))
		# Sleeping satisfies Running: the VM is parked and wakeable, not powered
		# off, so the intent it is parked under stays Running.
		run = self._transport("Running")
		task = run(
			server=self.server,
			script="sleep-vm",
			variables={
				"VIRTUAL_MACHINE_NAME": self.name,
				"ATLAS_FC_UID": str(derive_uid(self.name)),
			},
			virtual_machine=self.name,
			timeout_seconds=120,
		)
		# The VM is parked either way, so the row reads Sleeping either way. Whether
		# the host also dumped its RAM only changes the next wake's SPEED, and it is
		# the one thing this verb cannot always learn: Boat computes it and the
		# contract has nowhere to put it yet (`boat_client.OPERATION_RESULT_FIELD`),
		# so on a Boat host the Task carries no result line at all. Insisting on one
		# is how an idle VM ended up parked on its host and still Running in the DB —
		# with the throw swallowed by the idle sweeper, which then re-slept it once a
		# minute, forever, each time with a fresh op_id Boat genuinely re-ran.
		#
		# `has_memory_snapshot` is bookkeeping, not authority (spec/02): the on-host
		# READY marker decides at wake time, so leaving it untouched when the
		# transport did not say costs the operator a display detail and nothing else.
		result = parse_optional_result(task.stdout)
		self.status = "Sleeping"
		if result is not None:
			self.has_memory_snapshot = 1 if result.get("memory_snapshot") else 0
		self.last_stopped = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def wake(self) -> str:
		"""Wake a Sleeping VM. Removes the SLEEPING marker on the host so systemd
		will auto-start it on the next host reboot, then starts the unit. If a
		memory snapshot is present (has_memory_snapshot), the guest resumes in
		milliseconds; otherwise it cold-boots."""
		if self.status != "Sleeping":
			frappe.throw(f"Cannot wake from {self.status}")
		# FOR UPDATE holds the row lock for this transaction, preventing two
		# concurrent wake() calls (e.g. two proxy wake-ups) from both dispatching
		# a start Task and racing each other.
		frappe.db.sql("SELECT name FROM `tabVirtual Machine` WHERE name = %s FOR UPDATE", self.name)
		current_status = frappe.db.get_value("Virtual Machine", self.name, "status")
		if current_status != "Sleeping":
			return ""  # Another caller already woke it
		# An operator wake states Running, exactly as start() does — this is the
		# explicit reversal of an intent, which is the one thing allowed to
		# outrank a Stopped. What must not resurrect a stopped VM is *traffic*:
		# that path is `_adopt_wake`, and it refuses (spec/33 §11.3).
		run = self._transport("Running")
		task = run(
			server=self.server,
			script="wake-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.has_memory_snapshot = 0
		self.last_started = frappe.utils.now_datetime()
		# The wake is itself the activity (spec/32) — same reason as start(), and
		# more acute here: this VM slept *because* last_traffic_at was stale, so
		# leaving it would guarantee the next idle sweep re-sleeps it. _adopt_wake
		# stamps the same field for the host-initiated (packet-triggered) wake.
		self.last_traffic_at = self.last_started
		self.save()
		return task.name

	@frappe.whitelist()
	def restart(self, cold: bool = False) -> dict:
		"""Stop (if Running) then Start. Two Tasks. A Paused VM must resume or
		stop first — restart is deliberately Running/Stopped only.

		When the VM opted into memory_snapshot_on_stop, a restart is a
		state-preserving POWER CYCLE: the stop captures the guest's memory and
		the start resumes it — milliseconds, but the guest never reboots, so a
		wedged guest stays wedged. Pass `cold=True` for a true reboot (plain
		stop, full cold boot). Without the opt-in, restart is the plain
		stop + cold boot it always was."""
		if self.status not in ("Running", "Stopped"):
			frappe.throw(f"Cannot restart from {self.status}")
		cold = cold in (True, 1, "1", "true", "True", "yes")
		stop_task = self.stop(memory_snapshot=False if cold else None) if self.status == "Running" else None
		start_task = self.start()
		return {"stop_task": stop_task, "start_task": start_task}

	@frappe.whitelist()
	def pause(self) -> str:
		"""Freeze a Running VM's vCPUs via Firecracker's API socket. RAM stays
		resident (unlike Stop, which is a full shutdown). Reversible with
		resume()."""
		if self.status != "Running":
			frappe.throw(f"Cannot pause from {self.status}")
		self._guard_no_active_migration()
		# A paused VM's unit is still active and its RAM still resident, so the
		# intent stays Running: Stopped here would have the reconciler shut down
		# the machine the operator only meant to freeze.
		run = self._transport("Running")
		task = run(
			server=self.server,
			script="pause-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Paused"
		self.save()
		return task.name

	@frappe.whitelist()
	def resume(self) -> str:
		"""Unfreeze a Paused VM's vCPUs via the API socket."""
		if self.status != "Paused":
			frappe.throw(f"Cannot resume from {self.status}")
		self._guard_no_active_migration()
		run = self._transport("Running")
		task = run(
			server=self.server,
			script="resume-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def snapshot(self, title: str | None = None, live: bool = False) -> str:
		"""Snapshot this VM's disk(s) into a new Virtual Machine Snapshot row —
		the root disk and, if present, the data disk. Returns the snapshot's name.

		`title` is optional: omitted, it defaults to `<vm title> — <timestamp>`,
		so a caller (the SPA's one-click snapshot, or a direct API call) need not
		invent a name. The dashboard pre-fills the same default but lets the user
		edit it.

		Consistency — `live`:

		- Default (`live=False`): **Stopped-only**. A cleanly unmounted ext4 copies
		  flush-consistent, and with two disks a Stopped VM makes the root/data pair
		  mutually consistent. This is the safe default.
		- `live=True`: snapshot a **Running** (or Paused) VM without stopping. The
		  LVM thin CoW snapshot is atomic per volume, but the captured image is
		  **crash-consistent** — equivalent to pulling power at that instant:
		  unflushed guest-cache writes are absent and the guest replays its ext4
		  journal on next mount. The host can't quiesce the guest (no in-guest
		  agent), and the root/data LVs are snapshotted microseconds apart, so
		  cross-disk consistency isn't guaranteed. This is the same guarantee a
		  cloud "crash-consistent volume snapshot" gives; stop first for a
		  guaranteed-clean image."""
		# frm.call / REST send `live` as a JSON/stringy value; normalize to bool.
		live = live in (True, 1, "1", "true", "True", "yes")
		if self.status == "Sleeping":
			frappe.throw(_("Cannot snapshot a Sleeping VM — wake it first, stop it, then snapshot"))
		if live:
			if self.status not in ("Running", "Paused"):
				frappe.throw(
					f"Live snapshot needs a Running or Paused VM (status is {self.status}); "
					f"for a Stopped VM take a normal snapshot"
				)
		elif self.status != "Stopped":
			frappe.throw(
				f"Stop the VM before snapshotting (status is {self.status}), "
				f"or pass live=True for a crash-consistent live snapshot"
			)
		self._guard_no_active_migration()
		title = (title or "").strip() or self._default_snapshot_title()
		# A snapshot captures BOTH disks: the data disk is a first-class peer of
		# root. We record its size + mount config on the row so a clone/restore can
		# reconstruct the data disk faithfully even if the source VM later changes.
		has_data = bool(self.data_disk_gigabytes)
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": title,
				"virtual_machine": self.name,
				"server": self.server,
				"status": "Pending",
				"source_image": self.image,
				"disk_gigabytes": self.disk_gigabytes,
				"data_disk_gigabytes": self.data_disk_gigabytes,
				"data_disk_mount_point": self.data_disk_mount_point,
				"data_disk_format_and_mount": self.data_disk_format_and_mount,
				# Carry the bench bake mode so a clone of this golden maps its FQDN to
				# the baked site (site) or the admin console (admin) — empty for an
				# ordinary VM snapshot (spec/08).
				"build_mode": self.build_mode or None,
			}
		).insert(ignore_permissions=True)
		# The snapshot is an LVM thin snapshot, not a file copy. rootfs_path holds
		# its LV device path (derived from the snapshot's UUID, like the VM disk
		# LV) — no schema change, and it flows unchanged into restore/clone, which
		# read the LV name back from this path. The data snapshot LV is named off
		# the SAME snapshot UUID (atlas-datasnap-<id>), so the pair is recoverable.
		rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
		data_rootfs_path = f"/dev/atlas/atlas-datasnap-{snapshot.name}" if has_data else ""
		variables = {
			"VIRTUAL_MACHINE_NAME": self.name,
			"SNAPSHOT_ROOTFS_PATH": rootfs_path,
		}
		if data_rootfs_path:
			variables["DATA_SNAPSHOT_ROOTFS_PATH"] = data_rootfs_path
		task = run_task(
			server=self.server,
			script="snapshot-vm",
			variables=variables,
			virtual_machine=self.name,
			timeout_seconds=300,
		)
		# One atomic update: the Task already succeeded and the on-host file
		# exists, so the row must end up Available. Folding the writes into a
		# single db_set means there's no window where rootfs_path/size_bytes
		# landed but status didn't (a half-update that stranded the row in
		# Pending). size_bytes is a Long Int / bigint column — a real multi-GB
		# rootfs overflows a plain Int.
		result = parse_result(task.stdout)
		snapshot.db_set(
			{
				"rootfs_path": rootfs_path,
				"size_bytes": result["size_bytes"],
				"data_rootfs_path": data_rootfs_path,
				"data_size_bytes": result.get("data_size_bytes", 0),
				"status": "Available",
			}
		)
		return snapshot.name

	def _default_snapshot_title(self) -> str:
		"""`<vm title> — <YYYY-MM-DD HH:mm>` for an unnamed snapshot."""
		stamp = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M")
		return f"{self.title} — {stamp}"

	@frappe.whitelist()
	def capture_warm_snapshot(self, title: str | None = None) -> str:
		"""Capture this live VM's memory AND disk at one paused instant into a new
		`kind=Warm` Virtual Machine Snapshot. Returns the snapshot's name.

		Named with a verb (not `warm_snapshot`) on purpose: `warm_snapshot` is the
		Link *field* that records the golden a warm clone was restored from, and a
		method of that name would be shadowed by the field value on a hydrated doc.

		The capture half of the Image Builder's warm bake
		(`image_build._warm_snapshot`), exposed as a per-VM operator action: pause
		the running guest's vCPUs, write the memory pair (`vmstate.bin` +
		`mem.bin`) and an LVM thin disk snapshot at the *same* paused instant to a
		durable per-snapshot directory, capture the host signature, then resume —
		the VM never stops. The frozen RAM references exactly those disk blocks, so
		the pair is only valid together (see
		[05-virtual-machine-lifecycle.md → Warm snapshot fan-out]).

		Running or Paused only (there is a live guest to freeze); a Stopped VM has
		no memory to capture — take a plain `snapshot()` instead. The capture
		script rejects a VM with a data disk (warm snapshots are root-only).

		The row records the captured machine config (vcpus, memory) and tap name —
		the vmstate pins all three, so a restore must reproduce them exactly. This
		action only *produces* the artifact; restoring it onto its own VM is the
		fast stop/start shape, and fanning it out into clones is safe only for a
		golden baked with the in-guest freshen unit (the Image Builder warm bake) —
		see `Virtual Machine Snapshot.clone_to_new_vm`."""
		if self.status not in ("Running", "Paused"):
			frappe.throw(
				f"A warm snapshot needs a Running or Paused VM (status is {self.status}); "
				f"for a Stopped VM take a plain snapshot"
			)
		self._guard_no_active_migration()
		title = (title or "").strip() or self._default_snapshot_title()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": title,
				"virtual_machine": self.name,
				"server": self.server,
				"status": "Pending",
				"kind": "Warm",
				"source_image": self.image,
				"disk_gigabytes": self.disk_gigabytes,
				# Carry the bench bake mode (empty for an ordinary VM) so a clone of a
				# golden maps its FQDN correctly on first boot (spec/08).
				"build_mode": self.build_mode or None,
				# The frozen vmstate pins the machine and its tap name; a warm clone
				# must reproduce all three exactly (clone_to_new_vm enforces it).
				"vcpus": self.vcpus,
				"memory_megabytes": self.memory_megabytes,
				"tap_device": self.tap_device,
			}
		).insert(ignore_permissions=True)
		rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
		memory_directory = f"/var/lib/atlas/snapshots/{snapshot.name}"
		task = run_task(
			server=self.server,
			script="warm-snapshot-vm",
			variables={
				"VIRTUAL_MACHINE_NAME": self.name,
				"ATLAS_FC_UID": str(derive_uid(self.name)),
				"SNAPSHOT_ROOTFS_PATH": rootfs_path,
				"MEMORY_DIRECTORY": memory_directory,
			},
			virtual_machine=self.name,
			timeout_seconds=600,
		)
		# One atomic update, like snapshot(): the Task succeeded and the durable
		# artifacts exist on the host, so the row ends up Available with no window
		# where the paths landed but the status didn't.
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
		return snapshot.name

	@frappe.whitelist()
	def rebuild(self, source_type: str, source: str | None = None) -> str:
		return vm_images.rebuild(self, source_type, source)

	def _rebuild_variables(self, source_type: str, source: str | None) -> dict:
		return vm_images.rebuild_variables(self, source_type, source)

	@frappe.whitelist()
	def resize(
		self,
		vcpus: int | None = None,
		cpu_max_cores: float | None = None,
		cpu_mode: str | None = None,
		memory_megabytes: int | None = None,
		disk_gigabytes: int | None = None,
		data_disk_gigabytes: int | None = None,
	) -> str:
		return vm_resize.resize(
			self,
			vcpus,
			cpu_max_cores,
			cpu_mode,
			memory_megabytes,
			disk_gigabytes,
			data_disk_gigabytes,
		)

	@frappe.whitelist()
	def regenerate_host_keys(self) -> str:
		"""Rotate this VM's SSH host keys (change its SSH identity) on a **Stopped**
		VM. Stopped-only because the host mounts the rootfs to rewrite the keys.

		This is the explicit, opt-in counterpart to the preserve-by-default rule:
		provision establishes host keys at birth and rebuild/restore PRESERVE them
		(so a rollback never breaks clients' known_hosts), so changing them is a
		deliberate action. After the next Start the VM presents new host keys and
		clients must refresh known_hosts — that is the intended effect."""
		if self.status != "Stopped":
			frappe.throw(f"Stop the VM before regenerating host keys (status is {self.status})")
		self._guard_no_active_migration()
		task = run_task(
			server=self.server,
			script="regenerate-host-keys-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=60,
		)
		# The script dropped any pending memory snapshot (the rootfs changed
		# under it); mirror that on the row.
		self.db_set("has_memory_snapshot", 0)
		return task.name

	@frappe.whitelist()
	def deploy_gateway(self) -> bool:
		"""Stand up (or re-assert) this gateway VM's wg0 + the static same_48 guard, over
		guest-SSH (spec/26). Gateway-only: a non-gateway VM has no wg0 to bring up.
		Idempotent — safe to re-run after a reboot or rebuild."""
		if not self.is_gateway:
			frappe.throw(f"{self.name} is not a customer gateway (is_gateway unset)")
		from atlas.atlas import customer_gateway

		return customer_gateway.deploy_gateway(self.name)

	@frappe.whitelist()
	def read_proxy_maps(self) -> dict:
		"""Return this proxy's three live maps (sites / sni / acme) alongside the
		desired maps and a per-map drift flag — read-only. Proxy-only: a non-proxy VM
		has no admin sockets to read."""
		if not self.is_proxy:
			frappe.throw(f"{self.name} is not a proxy (is_proxy unset)")
		from atlas.atlas import proxy

		return proxy.read_live_maps(self.name)

	@frappe.whitelist()
	def terminate(self) -> str:
		if self.status == "Terminated":
			frappe.throw(_("VM is already terminated"))
		if self.termination_protection:
			frappe.throw(_("Disable termination protection before terminating this VM"))
		self._guard_no_active_migration()
		# Stopped first, and it matters: a terminate that fails half way through
		# must not leave a host whose intent still reads Running, or its
		# reconciler boots what the verb was in the middle of destroying.
		run = self._transport("Stopped")
		task = run(
			server=self.server,
			script="terminate-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=60,
		)
		self.status = "Terminated"
		self.save()
		vm_teardown.detach_reserved_ip(self)
		vm_teardown.revoke_tunnels(self)
		vm_teardown.revoke_vpc_peers(self)
		vm_teardown.delete_subdomains(self)
		vm_teardown.delete_custom_domains(self)
		self._delete_snapshots()
		vm_teardown.deprovision_proxy(self)
		self._terminate_front_doors()
		# The VM's private /128 leaves the local-ownership cache (vm-network-down.py
		# removes it on teardown); atlas-networkd's scan detects the change and gossips
		# the withdrawal (spec/31 §11). No controller-side reconcile.
		return task.name

	def _terminate_front_doors(self) -> None:
		vm_teardown.terminate_front_doors(self)

	def _delete_snapshots(self) -> None:
		vm_teardown.delete_snapshots(self)

	def _guest_authorized_keys(self) -> str:
		return vm_provisioning.guest_authorized_keys(self)

	def _provision_variables(self) -> dict:
		return vm_provisioning.provision_variables(self)


def auto_provision(virtual_machine_name: str) -> None:
	"""Background-job entrypoint. Called by `after_insert` so the operator
	doesn't have to click Provision. No-op if the VM has moved past Pending
	(operator intervened, manual provision raced us, etc.)."""
	virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine_name)
	if virtual_machine.status != "Pending":
		return
	virtual_machine.provision()


def poll_vm_traffic() -> None:
	"""Scheduled job (*/1 * * * *): for each server with sleep_on_idle Running VMs,
	dispatch a poll-vm-traffic Task and stamp last_traffic_at on active VMs."""
	import json

	vms = frappe.get_all(
		"Virtual Machine",
		filters={"status": "Running", "sleep_on_idle": 1},
		fields=["name", "server", "ipv6_address"],
	)
	if not vms:
		return

	by_server: dict = {}
	for vm in vms:
		by_server.setdefault(vm.server, []).append(vm)

	now = frappe.utils.now_datetime()
	for server, server_vms in by_server.items():
		vms_json = json.dumps([{"name": vm.name, "ipv6_address": vm.ipv6_address} for vm in server_vms])
		# run_probe, not run_task: a read-only poll on every server every minute
		# would bury the Task log in rows nobody reads. It logs its own failures
		# and returns "" instead of raising, so one bad server can't abort the rest.
		stdout = run_probe(
			server=server,
			script="poll-vm-traffic",
			variables={"VMS_JSON": vms_json},
			timeout_seconds=30,
		)
		if not stdout:
			continue
		counters = parse_result(stdout).get("counters", {})
		for vm_name, counter in counters.items():
			if counter.get("active"):
				frappe.db.set_value("Virtual Machine", vm_name, "last_traffic_at", now)


def sleep_idle_vms() -> None:
	"""Scheduled job (*/1 * * * *): find Running sleep_on_idle VMs whose
	last_traffic_at is older than their idle_timeout_seconds and put them to sleep.
	The per-minute poll (poll_vm_traffic) keeps last_traffic_at fresh; this sweeper
	acts on the staleness."""
	vms = frappe.get_all(
		"Virtual Machine",
		filters={"status": "Running", "sleep_on_idle": 1},
		fields=["name", "last_traffic_at", "idle_timeout_seconds"],
	)
	for vm_data in vms:
		if not vm_data.last_traffic_at:
			continue
		elapsed = (frappe.utils.now_datetime() - vm_data.last_traffic_at).total_seconds()
		if elapsed < (vm_data.idle_timeout_seconds or 300):
			continue
		# Re-read BOTH fields the decision rests on, not just status: the batch read
		# above is a snapshot, and poll_vm_traffic can stamp last_traffic_at between
		# it and here. Acting on the stale timestamp would sleep a VM that just went
		# active — self-correcting (the next SYN wakes it) but a real interruption.
		current = frappe.db.get_value(
			"Virtual Machine", vm_data.name, ["status", "last_traffic_at"], as_dict=True
		)
		if current.status != "Running":
			continue
		if not current.last_traffic_at or (
			frappe.utils.now_datetime() - current.last_traffic_at
		).total_seconds() < (vm_data.idle_timeout_seconds or 300):
			continue  # traffic arrived since the batch read
		try:
			vm = frappe.get_doc("Virtual Machine", vm_data.name)
			vm.sleep()
		except Exception as exception:
			# Don't abort the sweep — one VM that cannot sleep must not stop the
			# others. But don't swallow it silently either: this runs every minute
			# against a row that is still Running, so a refusal that repeats is a VM
			# being re-slept sixty times an hour, and `pass` left that invisible
			# everywhere except a Task list nobody is watching. A failure BEFORE the
			# Task (a refused desired-state PUT) left no row at all.
			frappe.logger("atlas").warning(f"sleep_idle_vms: {vm_data.name} could not sleep: {exception}")


def reconcile_sleeping_vms() -> None:
	"""Scheduled job (*/1 * * * *, BEFORE sleep_idle_vms): flip a Sleeping VM to
	Running once the host has woken it on its own — the DB catch-up for a
	packet-triggered wake (spec/32 sleepy VMs).

	atlas-wake-trap.py on the host wakes a Sleeping VM the moment it receives an
	inbound TCP SYN (removing the `sleeping` marker + starting the unit), but the
	host cannot reach into Atlas's DB. This probes each server's sleeping VMs for the
	marker's absence and mirrors that back into the status, so the DB drifts by at
	most one minute while the guest is reachable throughout. Ordered before
	sleep_idle_vms so a same-tick idle sweep sees the fresh last_traffic_at and does
	not immediately re-sleep a just-woken VM."""
	import json

	vms = frappe.get_all(
		"Virtual Machine",
		filters={"status": "Sleeping"},
		fields=["name", "server"],
	)
	if not vms:
		return

	by_server: dict = {}
	for vm in vms:
		by_server.setdefault(vm.server, []).append(vm.name)

	now = frappe.utils.now_datetime()
	for server, names in by_server.items():
		# run_probe, not run_task — see poll_vm_traffic: read-only, once a minute
		# per server with any sleeping VM, and its rows would be pure noise.
		stdout = run_probe(
			server=server,
			script="probe-woken-vms",
			variables={"VMS_JSON": json.dumps(names)},
			timeout_seconds=30,
		)
		if not stdout:
			continue
		woken = parse_result(stdout).get("woken", {})
		for name, is_woken in woken.items():
			if is_woken:
				_adopt_wake(name, now)


def _adopt_wake(name: str, now) -> None:
	"""Record a host-initiated wake in the DB, race-safe against an operator wake().
	Takes the same row lock wake() uses and re-reads status inside it, so whichever
	of the two commits first flips Sleeping->Running and the other no-ops. Sets the
	same fields wake() does: last_started + last_traffic_at (so sleep_idle_vms won't
	immediately re-sleep it) and clears has_memory_snapshot (the wake consumed it)."""
	frappe.db.sql("SELECT name FROM `tabVirtual Machine` WHERE name = %s FOR UPDATE", name)
	row = frappe.db.get_value("Virtual Machine", name, ["status", "desired_power", "server"], as_dict=True)
	if not row or row.status != "Sleeping":
		return  # operator wake() (or a previous tick) already adopted it
	if row.desired_power == "Stopped":
		# The precedence rule (spec/33 §11.3): a VM Atlas has stated Stopped is not
		# woken by traffic. A host that woke one anyway is drift for the mirror to
		# report — never an observation Atlas launders into its own status.
		return
	frappe.db.set_value(
		"Virtual Machine",
		name,
		{
			"status": "Running",
			"last_started": now,
			"last_traffic_at": now,
			"has_memory_snapshot": 0,
		},
	)
