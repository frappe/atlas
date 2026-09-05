from __future__ import annotations

from frappe.tests import UnitTestCase

from atlas.atlas.core.parsing import strict_bool


class TestParsing(UnitTestCase):
	def test_strict_bool_reads_the_boolean_forms(self) -> None:
		for value in (None, False, 0, "0", "false", "FALSE"):
			self.assertFalse(strict_bool(value, "flag"), value)
		for value in (True, 1, "1", "true", "TRUE"):
			self.assertTrue(strict_bool(value, "flag"), value)

	def test_strict_bool_rejects_a_value_that_is_not_a_boolean(self) -> None:
		"""Truthiness would read "yes" and 2 as true."""
		for value in ("yes", "", 2, -1, {}, []):
			with self.assertRaises(ValueError):
				strict_bool(value, "flag")

	def test_strict_bool_names_the_field(self) -> None:
		with self.assertRaises(ValueError) as caught:
			strict_bool("yes", "is_privileged")

		self.assertIn("is_privileged", str(caught.exception))
