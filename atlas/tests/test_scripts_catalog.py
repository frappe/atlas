import unittest

from atlas.atlas import scripts_catalog


class TestScriptsCatalog(unittest.TestCase):
	def test_operator_visible_is_subset_of_allowed(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertTrue(operator.issubset(allowed), operator - allowed)

	def test_operator_visible_includes_expected_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertIn("bootstrap-server.sh", operator)
		self.assertIn("reboot-server.sh", operator)
		self.assertIn("sync-image.sh", operator)

	def test_operator_visible_excludes_lifecycle_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		for hidden in (
			"provision-vm.sh",
			"start-vm.sh",
			"stop-vm.sh",
			"restart-vm.sh",
			"terminate-vm.sh",
			"vm-network-up.sh",
			"vm-network-down.sh",
		):
			self.assertNotIn(hidden, operator)

	def test_operator_visible_is_sorted(self) -> None:
		operator = scripts_catalog.operator_visible_scripts()
		self.assertEqual(operator, sorted(operator))
