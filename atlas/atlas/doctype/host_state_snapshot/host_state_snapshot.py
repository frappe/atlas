import json

import frappe
from frappe.model.document import Document

# How many exports to keep per host.
#
# Bounded on purpose. The mirror is disposable and never authority (spec/33 §1):
# losing every row here costs one `GET /export` round trip, so the table exists
# for the hour after something went wrong, not for history. Unbounded, it would
# grow with time × hosts × poll rate and each row is a whole-host document — the
# largest thing Atlas would be storing, in service of the least authoritative
# thing it holds. Bounded per host, it is O(hosts) instead: a 100-host fleet
# holds at most 2000 rows, and an operator still gets a run of recent epochs to
# diff a drift against its neighbours.
#
# The exact number is left open by spec/33 §16; this is the first answer, not a
# derived one. Raise it if a forensic window of 20 epochs proves too short.
SNAPSHOTS_KEPT_PER_HOST = 20


class HostStateSnapshot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		boat_version: DF.Data | None
		drift: DF.Code | None
		drift_count: DF.Int
		export_document: DF.Code | None
		observed_at: DF.Datetime | None
		server: DF.Link
		virtual_machine_count: DF.Int
	# end: auto-generated types

	"""One host's whole observed state at one epoch, as Boat reported it
	(spec/33-boat.md §2.5).

	`GET /v1/export` returns Boat's entire observed state in one document, and
	Atlas lands it in two places deliberately: the hot fields denormalized onto
	`Server`, because placement queries them on every provision and cannot afford
	a document parse, and the full document here — for debugging, mirror rebuild
	and drift forensics.

	**This is a mirror, not a record of truth.** Nothing is ever rebuilt from it
	and no contended decision is ever taken from it; a lost row costs one export
	round trip. That is why it is bounded (`SNAPSHOTS_KEPT_PER_HOST`), why every
	field is read-only, why it tracks no changes, and why `prune` deletes rows
	outright rather than archiving them. `atlas.atlas.boat_mirror` is the sole
	writer."""

	@classmethod
	def record(cls, server: str, export: dict, observed_at, drift: list[dict]) -> "HostStateSnapshot":
		"""Archive one export against its host, then prune that host's history.

		Keyed by (host, observed epoch): the epoch is monotonic per host, so the
		pair names exactly one snapshot. The caller ingests in epoch order and
		skips an epoch it already holds, which is what keeps that a fact rather
		than a hope."""
		snapshot = frappe.get_doc(
			{
				"doctype": "Host State Snapshot",
				"server": server,
				"observed_epoch": export.get("observed_epoch"),
				"observed_at": observed_at,
				"boat_version": (export.get("host") or {}).get("boat_version"),
				"virtual_machine_count": len(export.get("virtual_machines") or []),
				"drift_count": len(drift),
				"drift": json.dumps(drift, indent=1),
				"export_document": json.dumps(export, indent=1),
			}
		).insert(ignore_permissions=True)
		cls.prune(server)
		return snapshot

	@classmethod
	def prune(cls, server: str) -> list[str]:
		"""Delete every snapshot for `server` beyond the newest
		`SNAPSHOTS_KEPT_PER_HOST`, and return the names dropped.

		Newest by observed epoch, which is the host's own ordering — creation
		order would put a replayed backlog in the wrong sequence.

		A raw delete, not `frappe.delete_doc`: nothing links to a snapshot and it
		runs no lifecycle, so dropping the row is the whole operation. Ordering
		the whole (bounded) history and slicing in Python beats an OFFSET query
		here — the excess is one row per ingest, and the list never grows past
		the bound plus that."""
		names = frappe.get_all(
			"Host State Snapshot",
			filters={"server": server},
			order_by="observed_epoch desc, creation desc",
			pluck="name",
		)
		stale = names[SNAPSHOTS_KEPT_PER_HOST:]
		if stale:
			frappe.db.delete("Host State Snapshot", {"name": ("in", stale)})
		return stale
