import json

import frappe
from frappe import _
from frappe.model.document import Document


class CentralEventLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attempts: DF.Int
		event_type: DF.Data | None
		http_status: DF.Int
		last_error: DF.SmallText | None
		occurred_at: DF.Datetime | None
		payload: DF.Code | None
		reference_doctype: DF.Data | None
		reference_name: DF.Data | None
		status: DF.Literal["pending", "ok", "error", "skipped"]
	# end: auto-generated types

	"""Append-only audit trail of every event Atlas tries to report to Central
	(spec/16-central.md § Event reporting). The DocType is declared
	`"engine": "MyISAM"` so the insert is auto-committed per statement and is NOT
	rolled back when the surrounding request transaction unwinds — which is the
	whole point: an event is emitted from a doc_event mid-transaction, and if that
	business change (a VM/Site save) later rolls back, the InnoDB row vanishes but
	the MyISAM audit row survives. So you can always see what we *tried* to emit,
	even for a reverted change — without ever delivering that reverted change to
	Central (the after-commit deliver job never runs, so the row stays `pending`).

	`atlas.atlas.core.central_report` is the sole automatic writer: `_emit` inserts
	the row at `pending`; `deliver` (and its `_stamp` helper) updates `status` /
	`attempts` / `last_error` / `http_status` on the delivery outcome. The one
	operator action is `retry_delivery` — an on-demand redelivery from the desk."""

	@frappe.whitelist()
	def retry_delivery(self) -> None:
		"""Re-attempt delivery to Central from the desk. Resets the attempt budget (and clears
		the stale error) so the periodic retry cron re-arms too, then hands the POST to a
		background worker.

		Delivery must not run in this web request: `deliver` makes a network call to Central
		and stamps this row, and the Central Event Log is MyISAM (table-level locking). Doing
		it inline pins a web worker on the network round-trip and holds a table lock that
		blocks concurrent event-log writes (live emits, other retries)."""
		if self.status not in ("queued", "error", "skipped"):
			frappe.throw(_("Only queued, error or skipped events can be retried."))

		self.db_set("attempts", 0)
		self.db_set("last_error", None)
		self.db_set("status", "queued")

		payload = json.loads(self.payload) if self.payload else {}
		frappe.enqueue(
			"atlas.atlas.core.central_report.deliver",
			queue="default",
			timeout=60,
			log_name=self.name,
			event_type=self.event_type,
			payload=payload,
			occurred_at=self.occurred_at,
		)
