"""Read typed values from an untrusted request payload."""

from __future__ import annotations

from typing import Any

from frappe.utils import sbool


def strict_bool(value: Any, field: str) -> bool:
	"""Return one boolean, and reject a value that is not one.

	Truthiness would read the string "false" as true. `frappe.utils.sbool`
	converts the string forms and returns anything else unchanged, so refuse
	whatever it could not convert.
	"""
	if value is None:
		return False

	converted = sbool(value)
	if isinstance(converted, bool):
		return converted
	if isinstance(converted, int) and converted in (0, 1):
		return bool(converted)
	raise ValueError(f"{field} must be true or false.")
