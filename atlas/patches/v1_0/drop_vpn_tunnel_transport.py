"""Drop the dead `transport` column from VPN Tunnel.

`transport` was a locked-immutable Select (`public-ipv4` / `private-vpc`) that only
ever held its `public-ipv4` default — the `private-vpc` seam was never wired and
nothing branched on the value. The endpoint the client dials is derived at dispatch
(`networking.tunnel_endpoint_address`), so the field carried no decision. Removed
from the DocType JSON in this commit; Frappe leaves the orphan column on migrate, so
drop it explicitly.

Idempotent: no-ops once the column is gone (or on a fresh site that never had it).
"""

import frappe


def execute() -> None:
	if frappe.db.table_exists("VPN Tunnel") and frappe.db.has_column("VPN Tunnel", "transport"):
		frappe.db.sql_ddl("ALTER TABLE `tabVPN Tunnel` DROP COLUMN `transport`")
