"""The dashboard's lifecycle action map calls only existing whitelisted methods.

The SPA invents no server-side method (spec/11-user-ui.md): every action in
frontend/src/data/actions.js names a method that is `@frappe.whitelist()`'d on
the Virtual Machine controller, OR the `__delete__` sentinel that maps to the
standard `frappe.client.delete` endpoint. This test parses actions.js and pins
that contract, so a renamed/un-whitelisted controller method or a typo in the
SPA can't ship a button that 500s for the user.

It also pins "one primary per status" — the design rule the review bar checks.
"""

import pathlib
import re
import unittest

import frappe

ACTIONS_JS = (
	pathlib.Path(frappe.get_app_path("atlas"))
	/ ".."
	/ "frontend"
	/ "src"
	/ "data"
	/ "actions.js"
)

# Sentinels that are NOT controller methods (handled in the SPA via standard
# Frappe endpoints, not run_doc_method).
SENTINELS = {"__delete__"}


def _parse_actions() -> dict[str, list[dict]]:
	"""Pull { status: [{method, kind}, ...] } out of actions.js without a JS
	engine. The file is a flat object literal of arrays of one-line objects."""
	text = ACTIONS_JS.read_text()
	body = text.split("export const ACTIONS = {", 1)[1]
	result: dict[str, list[dict]] = {}
	status = None
	for line in body.splitlines():
		stripped = line.strip()
		header = re.match(r"^([A-Z][A-Za-z]+):\s*\[", stripped)
		if header:
			status = header.group(1)
			result[status] = []
		if status is None:
			continue
		method = re.search(r"method:\s*'([^']+)'", stripped)
		kind = re.search(r"kind:\s*'([^']+)'", stripped)
		if method and kind:
			result[status].append({"method": method.group(1), "kind": kind.group(1)})
		if stripped.startswith("]"):
			status = None
	return result


class TestActionMap(unittest.TestCase):
	def setUp(self) -> None:
		self.actions = _parse_actions()
		from atlas.atlas.doctype.virtual_machine.virtual_machine import VirtualMachine

		# @frappe.whitelist() adds the underlying function object to the
		# frappe.whitelisted set (frappe/__init__.py). Resolve each VM attribute
		# to its function and keep the names whose function is in that set.
		self.whitelisted = {
			name
			for name in dir(VirtualMachine)
			if callable(getattr(VirtualMachine, name, None))
			and getattr(VirtualMachine, name) in frappe.whitelisted
		}

	def test_actions_parsed(self) -> None:
		# Every status in the controller's status set should be represented.
		for status in ("Running", "Stopped", "Paused", "Pending", "Failed", "Terminated"):
			self.assertIn(status, self.actions, f"{status} missing from actions.js")

	def test_every_method_is_whitelisted_or_sentinel(self) -> None:
		for status, actions in self.actions.items():
			for action in actions:
				method = action["method"]
				if method in SENTINELS:
					continue
				self.assertIn(
					method,
					self.whitelisted,
					f"{status} action calls '{method}', which is not @frappe.whitelist()'d "
					f"on Virtual Machine",
				)

	def test_one_primary_per_status(self) -> None:
		for status, actions in self.actions.items():
			primaries = [a for a in actions if a["kind"] == "primary"]
			self.assertLessEqual(
				len(primaries),
				1,
				f"{status} has {len(primaries)} primary actions — only one per page",
			)
