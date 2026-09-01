# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.server_providers.base import ACCEPTED_OS_VERSIONS


class ServerImage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		image: DF.Data
		os: DF.Literal["Ubuntu", "Debian"]
		provider_metadata: DF.Code | None
		provider_type: DF.Literal["Scaleway"]
		version: DF.Literal["22.04", "24.04", "26.04", "11", "12", "13"]
	# end: auto-generated types

	def autoname(self) -> None:
		if not self.provider_type or not self.image:
			frappe.throw(_("Server Image requires provider_type and image"))
		self.name = f"{self.provider_type}/{self.image}"

	def validate(self) -> None:
		if self.version not in ACCEPTED_OS_VERSIONS.get(self.os, ()):
			frappe.throw(_("Server Image version {0} is not valid for {1}").format(self.version, self.os))

		expected = f"{self.provider_type}/{self.image}"
		if self.name and self.name != expected:
			frappe.throw(_("Server Image name {0} does not match {1}").format(self.name, expected))
