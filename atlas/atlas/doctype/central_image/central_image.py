from frappe.model.document import Document


class CentralImage(Document):
	# Upserted by atlas.atlas.central.upsert_central_images from the Fetch Images
	# button. Central declares which bench images this Atlas is expected to offer;
	# bake_status shows whether each has actually been baked. See spec/16-central.md.
	pass
