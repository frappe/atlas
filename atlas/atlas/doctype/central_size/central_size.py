from frappe.model.document import Document


class CentralSize(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		central_metadata: DF.Code | None
		cpu_max_cores: DF.Float
		disk_gigabytes: DF.Int
		enabled: DF.Check
		memory_megabytes: DF.Int
		monthly_cost_usd: DF.Int
		slug: DF.Data
		title: DF.Data | None
		vcpus: DF.Int
	# end: auto-generated types

	# Upserted by atlas.atlas.central.upsert_central_sizes from the Fetch Sizes
	# button. A Central-owned catalog, distinct from Provider Size (which is what
	# the vendor sells). See spec/16-central.md.
	pass
