import unittest

from atlas.atlas import scripts_catalog


class TestScriptsCatalog(unittest.TestCase):
	def test_operator_visible_is_subset_of_allowed(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertTrue(operator.issubset(allowed), operator - allowed)

	def test_operator_visible_includes_expected_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertIn("sync-image.py", operator)

	def test_operator_visible_excludes_lifecycle_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		for hidden in (
			"provision-vm.py",
			"start-vm.py",
			"stop-vm.py",
			"terminate-vm.py",
			"snapshot-vm.py",
			"rebuild-vm.py",
			"resize-vm.py",
			"pause-vm.py",
			"resume-vm.py",
			"delete-snapshot-vm.py",
		):
			self.assertNotIn(hidden, operator)

	def test_operator_visible_excludes_scripts_with_dedicated_buttons(self) -> None:
		# bootstrap-server.py and reboot-server.sh are reachable via dedicated
		# top-bar buttons (Bootstrap / Re-bootstrap / Reboot) with their own
		# confirmation guards. Offering them in the Run Task picker would
		# duplicate the flow without the guards.
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertNotIn("bootstrap-server.py", operator)
		self.assertNotIn("reboot-server.sh", operator)

	def test_allowed_includes_py_and_remaining_sh(self) -> None:
		# The catalog now globs both .py (ported tasks) and .sh (reboot-server.sh
		# stays shell). Both extensions must be runnable.
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertIn("provision-vm.py", allowed)
		self.assertIn("reboot-server.sh", allowed)

	def test_allowed_excludes_systemd_hooks(self) -> None:
		# vm-disk-up.py / vm-network-up.py / vm-network-down.py live in scripts/
		# but are systemd-invoked (positional uuid), not Task-runnable — they must
		# never appear in the runner's allowlist.
		allowed = set(scripts_catalog.allowed_scripts())
		for hook in scripts_catalog.SYSTEMD_HOOKS:
			self.assertNotIn(hook, allowed)

	def test_operator_visible_is_sorted(self) -> None:
		operator = scripts_catalog.operator_visible_scripts()
		self.assertEqual(operator, sorted(operator))

	def test_host_task_scripts_equals_allowed(self) -> None:
		# Every host SSH Task entry point is shipped durably and invoked in place;
		# the durable set is exactly the allowlist.
		self.assertEqual(scripts_catalog.host_task_scripts(), scripts_catalog.allowed_scripts())

	def test_durable_remote_path_for_shipped_script(self) -> None:
		# A production Task script resolves to its durable /var/lib/atlas/bin path,
		# which the runner invokes without a per-Task scp.
		self.assertEqual(
			scripts_catalog.durable_remote_path("start-vm.py"),
			"/var/lib/atlas/bin/start-vm.py",
		)

	def test_durable_remote_path_none_for_e2e_probe(self) -> None:
		# e2e probes live in the test-only directory, are not shipped durably, and
		# must keep the staging path (None tells the runner to scp them per Task).
		self.assertIsNone(scripts_catalog.durable_remote_path("phase1-probe.sh"))
