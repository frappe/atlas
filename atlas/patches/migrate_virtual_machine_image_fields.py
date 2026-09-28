import frappe


def execute() -> None:
	"""Move legacy virtual machine image values to the current fields."""
	frappe.db.sql(
		"""
		UPDATE `tabVirtual Machine Image`
		SET `image_type` = LOWER(`image_type`)
		WHERE BINARY `image_type` IN ('System', 'Machine')
		"""
	)

	if not frappe.db.has_column("Virtual Machine Image", "platform"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabVirtual Machine Image`
		SET `architecture` = `platform`
		WHERE COALESCE(`architecture`, '') = ''
			AND `platform` IN ('amd64', 'arm64')
		"""
	)
