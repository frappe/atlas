from frappe.model.document import Document


class BenchRoutingAudit(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		business_reject: DF.Check
		endpoint: DF.Data | None
		fwd_headers: DF.LongText | None
		label: DF.Data | None
		request_body: DF.LongText | None
		source_ip: DF.Data | None
		status: DF.Data | None
		vm: DF.Data | None
	# end: auto-generated types

	"""Append-only forensic log of every bench-routing endpoint call (spec/18
	Component I). The DocType is declared `"engine": "MyISAM"` so an insert is
	auto-committed per statement and is NOT rolled back when the surrounding request
	transaction unwinds — which is the whole point: a rejected `register`/`deregister`
	(or a non-resolving source) calls `frappe.throw`, rolling back its own InnoDB
	transaction; on MyISAM the audit row survives, so we keep the record of exactly the
	attempts most worth auditing (the rejected / hijack-attempt ones).

	The controller (`bench_routing._audit`) is the SOLE writer; nothing edits a row
	after insert. No controller logic lives here — the durability argument rests
	entirely on the table engine, asserted at migrate (test + e2e)."""

	pass
