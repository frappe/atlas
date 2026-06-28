import unittest

from atlas.atlas import scripts_catalog


class TestScriptsCatalog(unittest.TestCase):
	def test_operator_visible_is_subset_of_allowed(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertTrue(operator.issubset(allowed), operator - allowed)

	def test_operator_visible_includes_expected_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertIn("sync-image", operator)

	def test_operator_visible_excludes_lifecycle_scripts(self) -> None:
		operator = set(scripts_catalog.operator_visible_scripts())
		for hidden in (
			"provision-vm",
			"start-vm",
			"stop-vm",
			"terminate-vm",
			"snapshot-vm",
			"rebuild-vm",
			"resize-vm",
			"pause-vm",
			"resume-vm",
			"delete-snapshot-vm",
		):
			self.assertNotIn(hidden, operator)

	def test_operator_visible_excludes_scripts_with_dedicated_buttons(self) -> None:
		# bootstrap-server and reboot-server are reachable via dedicated top-bar
		# buttons (Bootstrap / Re-bootstrap / Reboot) with their own confirmation
		# guards. Offering them in the Run Task picker would duplicate the flow
		# without the guards.
		operator = set(scripts_catalog.operator_visible_scripts())
		self.assertNotIn("bootstrap-server", operator)
		self.assertNotIn("reboot-server", operator)

	def test_allowed_includes_py_and_remaining_sh(self) -> None:
		# The catalog speaks verbs but still globs both .py (ported tasks) and .sh
		# (reboot-server stays shell). Both must be runnable as verbs.
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertIn("provision-vm", allowed)
		self.assertIn("reboot-server", allowed)

	def test_allowed_excludes_systemd_hooks(self) -> None:
		# vm-disk-up / vm-network-up / vm-network-down / vm-restore live in scripts/
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
		# A production Task verb resolves to its durable /var/lib/atlas/bin FILE
		# path (the file keeps its suffix), which the runner reaches without a scp.
		self.assertEqual(
			scripts_catalog.durable_remote_path("start-vm"),
			"/var/lib/atlas/bin/start-vm.py",
		)

	def test_durable_remote_path_none_for_e2e_probe(self) -> None:
		# e2e probes live in the test-only directory, are not shipped durably, and
		# must keep the staging path (None tells the runner to scp them per Task).
		self.assertIsNone(scripts_catalog.durable_remote_path("phase1-probe"))

	def test_file_for_maps_verb_to_basename(self) -> None:
		self.assertEqual(scripts_catalog.file_for("provision-vm"), "provision-vm.py")
		self.assertEqual(scripts_catalog.file_for("reboot-server"), "reboot-server.sh")

	def test_kind_distinguishes_python_from_shell(self) -> None:
		self.assertEqual(scripts_catalog.kind("provision-vm"), "python")
		self.assertEqual(scripts_catalog.kind("reboot-server"), "shell")

	def test_allowed_scripts_are_suffixless_verbs(self) -> None:
		# The allowlist returns verbs, never filenames.
		for verb in scripts_catalog.allowed_scripts():
			self.assertFalse(verb.endswith((".py", ".sh")), verb)

	def test_resolve_finds_file_by_verb(self) -> None:
		path = scripts_catalog.resolve("provision-vm")
		self.assertEqual(path.name, "provision-vm.py")
