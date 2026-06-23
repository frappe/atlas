"""Re-import the Atlas Workspace after the Provider collapse.

`bench migrate` does not re-sync a workspace from its JSON on an existing site, so
a site that had the old workspace keeps its dead `Provider` / `Domain Provider` /
`TLS Provider` link rows — which fail `Workspace.validate()` once those DocTypes are
gone. Force-import the workspace JSON (post_model_sync, so the new DocTypes the JSON
links to already exist). Mirrors `install_atlas_sidebar`, which re-imports the
sidebar the same way."""

from __future__ import annotations

import os

import frappe
from frappe.modules.import_file import import_file_by_path


def execute() -> None:
	path = os.path.join(frappe.get_app_path("atlas"), "atlas", "workspace", "atlas", "atlas.json")
	if os.path.exists(path):
		import_file_by_path(path, force=True)
