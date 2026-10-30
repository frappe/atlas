import frappe


def execute() -> None:
	"""Create one kernel record per SHA-256 value from the old image columns, and link each image to it.

	An image whose kernel is still stored is read first, so the record points at a kernel that exists.
	"""
	if not frappe.db.has_column("Virtual Machine Image", "kernel_sha256"):
		return

	images = frappe.db.sql(
		"""
		select name, architecture, kernel_sha256, kernel_object_key, kernel_file, kernel_size_mib
		from `tabVirtual Machine Image`
		where ifnull(kernel_sha256, '') != ''
		order by (ifnull(kernel_object_key, '') != '' or ifnull(kernel_file, '') != '') desc, creation desc
		""",
		as_dict=True,
	)
	for image in images:
		if not frappe.db.exists("Virtual Machine Kernel", image.kernel_sha256):
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Kernel",
					"sha256": image.kernel_sha256,
					"architecture": image.architecture,
					"size_mib": image.kernel_size_mib,
					"object_key": image.kernel_object_key,
					"file": image.kernel_file,
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Virtual Machine Image", image.name, "kernel", image.kernel_sha256, update_modified=False
		)

	# A Machine image still uploading boots the kernel of its source VM.
	for image in frappe.get_all(
		"Virtual Machine Image",
		filters={"kernel": ["is", "not set"], "source_virtual_machine": ["is", "set"]},
		fields=["name", "source_virtual_machine"],
	):
		source_image = frappe.db.get_value(
			"Virtual Machine", image.source_virtual_machine, "virtual_machine_image"
		)
		kernel = (
			frappe.db.get_value("Virtual Machine Image", source_image, "kernel") if source_image else None
		)
		if kernel:
			frappe.db.set_value("Virtual Machine Image", image.name, "kernel", kernel, update_modified=False)
