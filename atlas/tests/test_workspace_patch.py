"""Tests for atlas.patches.v1_0.migrate_workspace_to_onboarding."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from atlas.patches.v1_0.migrate_workspace_to_onboarding import _fixture_content, execute


class TestMigrateWorkspaceToOnboarding(FrappeTestCase):
	"""The patch backfills the live `Atlas` workspace's `content` field with
	the canonical fixture content, removing the legacy `bsc_block`
	custom-HTML reference."""

	STALE_CONTENT = json.dumps([
		{"id": "bsc_block", "type": "custom_block", "data": {
			"custom_block_name": "atlas-bootstrap-checklist", "col": 12,
		}},
	])

	def setUp(self) -> None:
		# Snapshot the live workspace state so each test can mutate freely
		# and `tearDown` restores it. Child tables get re-created from the
		# snapshot rows, not via `set_value` on a JSON column.
		self._original_content = frappe.db.get_value("Workspace", "Atlas", "content")
		self._original_custom_blocks = [
			row.as_dict()
			for row in frappe.get_doc("Workspace", "Atlas").custom_blocks
		]

	def tearDown(self) -> None:
		if self._original_content is not None:
			frappe.db.set_value("Workspace", "Atlas", "content", self._original_content)
		# Restore custom_blocks child rows to whatever they were before.
		frappe.db.delete("Workspace Custom Block", {"parent": "Atlas"})
		for index, row in enumerate(self._original_custom_blocks, start=1):
			row.pop("name", None)
			frappe.get_doc({
				"doctype": "Workspace Custom Block",
				"parenttype": "Workspace",
				"parentfield": "custom_blocks",
				"parent": "Atlas",
				"idx": index,
				**row,
			}).insert(ignore_permissions=True)
		if frappe.db.exists("Custom HTML Block", "atlas-bootstrap-checklist"):
			frappe.delete_doc("Custom HTML Block", "atlas-bootstrap-checklist", force=1)

	def _seed_stale_state(self) -> None:
		"""Mirror the legacy on-disk shape: the JSON content references the
		`bsc_block`, a `Workspace Custom Block` child row points at the
		(also stale) custom HTML block. The block itself may or may not
		survive in DB — both shapes show up across legacy sites."""
		frappe.db.set_value("Workspace", "Atlas", "content", self.STALE_CONTENT)
		frappe.db.delete("Workspace Custom Block", {"parent": "Atlas"})
		# Insert the child row directly so we don't pay Workspace's full
		# `save()` validation cost (and so the link-target absence doesn't
		# trip on the way in — the patch exists precisely to clear this).
		frappe.db.sql(
			"""
			INSERT INTO `tabWorkspace Custom Block`
				(name, parent, parenttype, parentfield, idx, custom_block_name)
			VALUES (%s, %s, %s, %s, %s, %s)
			""",
			(
				frappe.generate_hash(length=10),
				"Atlas",
				"Workspace",
				"custom_blocks",
				1,
				"atlas-bootstrap-checklist",
			),
		)

	def test_replaces_stale_content_with_fixture(self) -> None:
		"""A workspace carrying the legacy bsc_block content gets rewritten
		to match the canonical fixture."""
		self._seed_stale_state()

		execute()

		self.assertEqual(
			frappe.db.get_value("Workspace", "Atlas", "content"),
			_fixture_content(),
		)

	def test_drops_stale_custom_block_child_row(self) -> None:
		"""The orphan `Workspace Custom Block` child row that pointed at
		the now-deleted `atlas-bootstrap-checklist` is cleared so that the
		canonical-content save passes Link validation."""
		self._seed_stale_state()

		execute()

		self.assertEqual(
			frappe.db.count(
				"Workspace Custom Block",
				{"parent": "Atlas", "custom_block_name": "atlas-bootstrap-checklist"},
			),
			0,
		)

	def test_drops_legacy_custom_html_block(self) -> None:
		"""The stale `atlas-bootstrap-checklist` Custom HTML Block gets
		deleted if it survives in the DB."""
		if not frappe.db.exists("Custom HTML Block", "atlas-bootstrap-checklist"):
			frappe.get_doc({
				"doctype": "Custom HTML Block",
				"name": "atlas-bootstrap-checklist",
				"html": "<div>stale</div>",
				"private": 0,
			}).insert(ignore_permissions=True)

		execute()

		self.assertFalse(frappe.db.exists("Custom HTML Block", "atlas-bootstrap-checklist"))

	def test_no_op_when_content_already_canonical(self) -> None:
		"""Re-running the patch on a workspace that already matches the
		fixture leaves the content untouched."""
		canonical = _fixture_content()
		frappe.db.set_value("Workspace", "Atlas", "content", canonical)

		execute()

		self.assertEqual(
			frappe.db.get_value("Workspace", "Atlas", "content"),
			canonical,
		)
