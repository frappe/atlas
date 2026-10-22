import frappe


def execute() -> None:
	"""Name each untitled kernel after the oldest image that boots it."""
	for kernel in frappe.get_all(
		"Virtual Machine Kernel", filters={"title": ["is", "not set"]}, pluck="name"
	):
		image_title = frappe.db.get_value(
			"Virtual Machine Image", {"kernel": kernel}, "title", order_by="creation asc"
		)
		if image_title:
			frappe.db.set_value(
				"Virtual Machine Kernel", kernel, "title", f"{image_title} - kernel", update_modified=False
			)
