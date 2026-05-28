"""Migrate the Atlas workspace from the legacy `bsc_block` custom-HTML
checklist to the canonical `Atlas Setup` Module Onboarding widget.

The DocType-JSON fixture in `atlas/atlas/workspace/atlas/atlas.json`
already carries the correct content; the bug is purely in the live DB
on bootstrapped sites that were last touched before the fixture
landed. This patch reads the fixture's `content` string and writes it
back to the live row, then deletes the stale Custom HTML Block if it
survives.

Idempotent: re-running is a no-op once the content already matches.
"""

import json
import os

import frappe


def _fixture_content() -> str:
	"""Load the canonical workspace content from the on-disk fixture."""
	app_path = frappe.get_app_path("atlas")
	fixture_path = os.path.join(app_path, "atlas", "workspace", "atlas", "atlas.json")
	with open(fixture_path) as handle:
		fixture = json.load(handle)
	return fixture["content"]


def execute():
	if not frappe.db.exists("Workspace", "Atlas"):
		return

	# Drop the orphan child rows that reference the stale block first; a
	# subsequent `workspace.save()` walks the child tables and would fail
	# Link validation if `atlas-bootstrap-checklist` is still listed.
	frappe.db.delete(
		"Workspace Custom Block",
		{"parent": "Atlas", "custom_block_name": "atlas-bootstrap-checklist"},
	)

	canonical = _fixture_content()
	current = frappe.db.get_value("Workspace", "Atlas", "content")
	if current != canonical:
		workspace = frappe.get_doc("Workspace", "Atlas")
		workspace.content = canonical
		workspace.save(ignore_permissions=True)

	if frappe.db.exists("Custom HTML Block", "atlas-bootstrap-checklist"):
		frappe.delete_doc("Custom HTML Block", "atlas-bootstrap-checklist", force=1)
