from frappe.model.document import Document


class CentralImage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bake_status: DF.Literal["Expected", "Baked", "Stale"]
		central_metadata: DF.Code | None
		enabled: DF.Check
		image_name: DF.Data
		local_image: DF.Link | None
		series: DF.Data | None
		title: DF.Data | None
	# end: auto-generated types

	# Upserted by atlas.atlas.central.upsert_central_images from the Fetch Images
	# button. Central declares which bench images this Atlas is expected to offer;
	# bake_status shows whether each has actually been baked. See spec/16-central.md.
	pass
