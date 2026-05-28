import pathlib

import frappe
from frappe.utils.password import get_decrypted_password


def get_secret(doctype: str, name: str, fieldname: str) -> str:
	"""Read a Password-type field, decrypted. Single chokepoint so the
	storage backend can be swapped later."""
	return get_decrypted_password(doctype, name, fieldname, raise_exception=True)


def get_ssh_key_from_disk(path: str) -> str:
	"""Read an SSH private key from `path`. Single chokepoint so the
	location/auth model can change later without touching SSH callers."""
	expanded = pathlib.Path(path).expanduser()
	if not expanded.is_file():
		frappe.throw(f"SSH private key not found at {path!r}")
	return expanded.read_text()
