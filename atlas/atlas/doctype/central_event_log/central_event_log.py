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

	`atlas.atlas.central_report` is the sole automatic writer: `_emit` inserts the
	row at `pending`; `deliver` (and its `_stamp` helper) updates `status` /
	`attempts` / `last_error` / `http_status` on the delivery outcome. The one
	operator action is `retry_delivery` — an on-demand redelivery from the desk."""

	@frappe.whitelist()
	def retry_delivery(self) -> None:
		"""Re-attempt delivery to Central right now, from the desk. Resets the attempt
		budget (and clears the stale error) so the periodic retry cron re-arms too."""
		if self.status not in ("queued", "error", "skipped"):
			frappe.throw(_("Only queued, error or skipped events can be retried."))

		from atlas.atlas.central_report import deliver

		self.db_set("attempts", 0)
		self.db_set("last_error", None)

		if self.payload:
			payload = json.loads(self.payload)
		else:
			payload = {}

		deliver(self.name, self.event_type, payload, self.occurred_at)
