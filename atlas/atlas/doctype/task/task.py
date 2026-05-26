import json

import frappe
from frappe.model.document import Document

IMMUTABLE_AFTER_INSERT = ("server", "virtual_machine", "script", "variables", "triggered_by")


class Task(Document):
	@property
	def variables_dict(self) -> dict:
		return json.loads(self.variables or "{}")

	@variables_dict.setter
	def variables_dict(self, value: dict) -> None:
		if not isinstance(value, dict):
			frappe.throw("Task.variables_dict must be a dict")
		self.variables = json.dumps(value, sort_keys=True)

	def validate(self) -> None:
		if not self.variables:
			frappe.throw("variables is required")
		self._validate_variables_json()
		self._validate_immutability()

	def _validate_variables_json(self) -> None:
		try:
			parsed = json.loads(self.variables)
		except json.JSONDecodeError as exception:
			frappe.throw(f"variables must be valid JSON: {exception}")
		if not isinstance(parsed, dict):
			frappe.throw("variables must be a JSON object")

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is read-only after insert")
