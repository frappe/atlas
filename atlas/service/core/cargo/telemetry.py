# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frappe
from frappe import _

from atlas.service.core.cargo.storage_cluster import get_node_size

CONFIG_FILE_PATH = ("private", "files", "cargo-telemetry.json")
DATUM_REPOSITORY = "https://github.com/frappe/Datum.git"
DATUM_VERSION = "main"


def config_file() -> Path:
	"""Return the file that holds the requested Datum host between provision and installation."""
	return Path(frappe.get_site_path(*CONFIG_FILE_PATH))


def store_telemetry_config(values: Any) -> dict[str, Any]:
	"""Validate the requested Datum host and keep it for the installer. Throws on a bad request."""
	config = _validated_config(values)
	path = config_file()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(config, indent=2))
	return config


def telemetry_config_json() -> str:
	"""Return the stored Datum host as the compact JSON the installer passes to Cargo."""
	path = config_file()
	if not path.exists():
		frappe.throw(_("No telemetry configuration was stored for this Cargo Server."))

	return json.dumps(json.loads(path.read_text()), separators=(",", ":"))


def remove_telemetry_config() -> None:
	"""Forget the Datum host of an archived Cargo Server."""
	config_file().unlink(missing_ok=True)


def _validated_config(values: Any) -> dict[str, Any]:
	if not isinstance(values, dict):
		frappe.throw(_("The telemetry configuration must be an object."))

	return {
		"repository": DATUM_REPOSITORY,
		"version": DATUM_VERSION,
		"telemetry": get_node_size(values.get("telemetry"), "telemetry"),
	}
