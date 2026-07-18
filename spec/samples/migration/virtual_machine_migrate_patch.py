"""The focused changes to atlas/atlas/doctype/virtual_machine/virtual_machine.py.

SAMPLE / ILLUSTRATIVE — shown as standalone snippets so the diff is legible, not
as a full file. Each block notes where it slots into the real controller.
See spec/24-vm-migration.md.
"""

import frappe
from frappe import _

# ─────────────────────────────────────────────────────────────────────────────
# 1. validate(): the `flags.migrating` gate.
#
# REPLACES the body of VirtualMachine.validate() (currently lines ~137-149).
# The ONLY change is the new `if not self.flags.migrating` branch that drops
# `server` and `ipv6_address` from the guarded set during a cutover commit —
# exactly mirroring how `flags.resizing` drops RESIZE_MUTABLE today. Migration
# is the one sanctioned path that repoints `server`; nothing else may.
# ─────────────────────────────────────────────────────────────────────────────

# Fields the migration cutover is allowed to change, and nothing else may.
MIGRATE_MUTABLE = ("server", "ipv6_address")


def validate(self) -> None:  # method on VirtualMachine
	if self.is_new():
		return
	original = self.get_doc_before_save()
	if not original:
		return
	guarded = self.IMMUTABLE_AFTER_INSERT
	if not self.flags.resizing:
		# Outside resize(), the resource fields are frozen too.
		guarded = guarded + self.RESIZE_MUTABLE
	if self.flags.migrating:
		# Cutover commits server + ipv6_address together (the host move already
		# happened); let exactly those two through. Everything else — including
		# the resource fields, which a migration never touches — stays frozen.
		guarded = tuple(f for f in guarded if f not in MIGRATE_MUTABLE)
	for field in guarded:
		if getattr(self, field) != getattr(original, field):
			frappe.throw(f"{field} is immutable after insert")


# ─────────────────────────────────────────────────────────────────────────────
# 2. migrate(): the operator/Central entry point. Inserts a migration row; the
#    scheduler (migration.reconcile_migrations) drives the rest. Mirrors the
#    fire-and-forget shape of provision()/snapshot() but hands off to the
#    resumable phase machine instead of running a single Task inline.
#
# ADD as a new whitelisted method on VirtualMachine.
# ─────────────────────────────────────────────────────────────────────────────


@frappe.whitelist()
def migrate(self, target_server: str, release_reserved_ip: bool = False) -> str:
	"""Begin migrating this VM's disk to `target_server`, keeping its identity.

	Cold migration: the VM is stopped during cutover and gets a NEW public IPv6 on
	the target (the /128 is per-server and not portable — see spec/06, spec/24).
	Returns the Virtual Machine Migration row name; the scheduled
	`reconcile_migrations` callback advances it phase by phase, idempotently and
	resumably.

	Pre-flight (the cheap, synchronous half — the on-host checks that need SSH run
	in the first phase): single-flight per VM, target Active + same provider/region,
	not already on the target, and an explicit ack before dropping inbound v4."""
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
	return migration.name


# ─────────────────────────────────────────────────────────────────────────────
# 3. The lifecycle guard. A VM mid-migration must not be started/stopped/
#    terminated/snapshotted/resized/rebuilt out from under the phase machine —
#    every one of those SSHes `self.server`, which is the STALE source until the
#    Repointing phase flips it. Call `_guard_no_active_migration()` at the top of
#    each (after its existing status/protection guards).
#
# ADD this helper and one call line to each of: start, stop, terminate,
# snapshot, capture_warm_snapshot, rebuild, resize, regenerate_host_keys,
# pause, resume.
# ─────────────────────────────────────────────────────────────────────────────


def _guard_no_active_migration(self) -> None:  # method on VirtualMachine
	"""Throw if a non-terminal migration exists for this VM. The migration phase
	machine owns every host operation while it runs; a concurrent lifecycle action
	would race it against the wrong (stale) server."""
	from atlas.atlas.doctype.virtual_machine_migration.virtual_machine_migration import (
		active_migration_for,
	)

	migration = active_migration_for(self.name)
	if migration:
		frappe.throw(
			_("Virtual Machine {0} has an in-flight migration ({1}); wait for it to finish or fail").format(
				self.name, migration
			)
		)
