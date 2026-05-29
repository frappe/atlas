import unittest

from atlas.atlas import scripts_catalog


class TestScriptsCatalog(unittest.TestCase):
	def test_operator_visible_is_subset_of_allowed(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertTrue(operator.issubset(allowed), operator - allowed)

	def test_operator_visible_includes_expected_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertIn("sync-image.sh", operator)

	def test_operator_visible_excludes_lifecycle_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		for hidden in (
			"provision-vm.sh",
			"start-vm.sh",
			"stop-vm.sh",
			"restart-vm.sh",
			"terminate-vm.sh",
			"snapshot-vm.sh",
			"rebuild-vm.sh",
			"resize-vm.sh",
			"pause-vm.sh",
			"resume-vm.sh",
			"delete-snapshot-vm.sh",
			"vm-network-up.sh",
			"vm-network-down.sh",
		):
			self.assertNotIn(hidden, operator)

	def test_operator_visible_excludes_scripts_with_dedicated_buttons(self) -> None:
		# bootstrap-server.sh and reboot-server.sh are reachable via dedicated
		# top-bar buttons (Bootstrap / Re-bootstrap / Reboot) with their own
		# confirmation guards. Offering them in the Run Task picker would
		# duplicate the flow without the guards.
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertNotIn("bootstrap-server.sh", operator)
		self.assertNotIn("reboot-server.sh", operator)

	def test_operator_visible_is_sorted(self) -> None:
		operator = scripts_catalog.operator_visible_scripts()
		self.assertEqual(operator, sorted(operator))
