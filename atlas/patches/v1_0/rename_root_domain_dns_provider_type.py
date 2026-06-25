"""Rename `Root Domain.domain_provider_type` → `dns_provider_type`.

The denormalized DNS-vendor field on `Root Domain` is renamed to match the active
switch's new name (`Atlas Settings.dns_provider_type`) and the registry function
(`dns.for_dns_provider_type`). "Domain Provider" was a misnomer — Route53 /
Cloudflare are DNS providers, not domain registrars.

`Root Domain` is a normal DocType (a real column), so this is a column rename via
`frappe.model.rename_field`. It runs in **pre_model_sync**, before the JSON-driven
schema sync would otherwise add the new column and orphan the old one's data.

Idempotent: `rename_field` no-ops if the old column is already gone.
"""

import frappe
from frappe.model.utils.rename_field import rename_field


def execute() -> None:
	if not frappe.db.has_column("Root Domain", "domain_provider_type"):
		return  # already migrated, or fresh site
	if frappe.db.has_column("Root Domain", "dns_provider_type"):
		# Both columns exist (half-run): model sync added the new one before this
		# ran. Carry any old value into the new column, then drop the old.
		frappe.db.sql(
			"""UPDATE `tabRoot Domain`
			SET dns_provider_type = domain_provider_type
			WHERE (dns_provider_type IS NULL OR dns_provider_type = '')
			  AND domain_provider_type IS NOT NULL AND domain_provider_type != ''"""
		)
		frappe.db.sql_ddl("ALTER TABLE `tabRoot Domain` DROP COLUMN domain_provider_type")
		return
	rename_field("Root Domain", "domain_provider_type", "dns_provider_type")
