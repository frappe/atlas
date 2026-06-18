from frappe.model.document import Document


class CentralSize(Document):
	# Upserted by atlas.atlas.central.upsert_central_sizes from the Fetch Sizes
	# button. A Central-owned catalog, distinct from Provider Size (which is what
	# the vendor sells). See spec/16-central.md.
	pass
