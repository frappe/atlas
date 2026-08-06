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

	def test_host_task_scripts_is_the_file_derived_subset_of_allowed(self) -> None:
		# The durable set (shipped to /var/lib/atlas/bin and invoked in place) is the
		# FILE-derived host SSH Tasks. allowed_scripts() is that set PLUS the BOAT_ONLY
		# verbs, which the boat binary implements with no file to ship — so they are
		# runnable (in the allowlist) but not durably shipped.
		host = set(scripts_catalog.host_task_scripts())
		allowed = set(scripts_catalog.allowed_scripts())
		self.assertTrue(host.issubset(allowed))
		excluded = scripts_catalog.SYSTEMD_HOOKS | scripts_catalog.CONTROLLER_ONLY
		self.assertEqual(allowed - host, set(scripts_catalog.BOAT_ONLY_VERBS) - excluded)
		# Everything durably shipped has a file; no BOAT_ONLY verb does.
		for verb in host:
			self.assertNotIn(verb, scripts_catalog.BOAT_ONLY_VERBS, verb)

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
		# start-vm keeps its .py (a lifecycle verb, not a deleted boat verb);
		# reboot-server keeps its .sh.
		self.assertEqual(scripts_catalog.file_for("start-vm"), "start-vm.py")
		self.assertEqual(scripts_catalog.file_for("reboot-server"), "reboot-server.sh")

	def test_kind_distinguishes_python_from_shell(self) -> None:
		# A file-derived python verb reads "python" off its suffix; reboot-server.sh
		# reads "shell". (BOAT_ONLY verbs answer "python" without a file — see above.)
		self.assertEqual(scripts_catalog.kind("start-vm"), "python")
		self.assertEqual(scripts_catalog.kind("reboot-server"), "shell")

	def test_a_boat_only_verb_has_no_file_and_is_still_flag_shaped(self) -> None:
		# `bootstrap` is implemented by the boat binary and by nothing in scripts/,
		# so every file-derived answer has to come from BOAT_ONLY_VERBS instead:
		# `kind()` cannot read a suffix (it is asked what SHAPE the command line
		# has, and a boat verb's shape is the flag-taking one), `file_for()` has
		# nothing to return, and nothing is shipped durably for it. It IS runnable —
		# in allowed_scripts() (the run-task gate) — just not in host_task_scripts()
		# (the durable ship), because there is no file to upload.
		for verb in scripts_catalog.BOAT_ONLY_VERBS:
			self.assertTrue(scripts_catalog.runs_on_boat(verb), verb)
			self.assertEqual(scripts_catalog.kind(verb), "python", verb)
			self.assertIsNone(scripts_catalog.durable_remote_path(verb), verb)
			self.assertIn(verb, scripts_catalog.allowed_scripts(), verb)
			self.assertNotIn(verb, scripts_catalog.host_task_scripts(), verb)
			with self.assertRaises(FileNotFoundError):
				scripts_catalog.file_for(verb)

	def test_the_host_prep_verb_is_bootstrap_and_the_oracle_is_gone(self) -> None:
		# The cutover renamed the Task's verb because the runner renders
		# `<entry> <verb>` and `boat bootstrap-server` is not a command boat has.
		# `bootstrap` is the BOAT_ONLY verb Server.bootstrap() drives. Its Python
		# oracle (bootstrap-server.py) is deleted, so `bootstrap-server` is no longer
		# a runnable verb — no file, not in the allowlist — and was never routed at
		# boat.
		self.assertIn("bootstrap", scripts_catalog.BOAT_ONLY_VERBS)
		self.assertIn("bootstrap", scripts_catalog.allowed_scripts())
		self.assertNotIn("bootstrap-server", scripts_catalog.allowed_scripts())
		self.assertFalse(scripts_catalog.runs_on_boat("bootstrap-server"))
		with self.assertRaises(FileNotFoundError):
			scripts_catalog.file_for("bootstrap-server")

	def test_allowed_scripts_are_suffixless_verbs(self) -> None:
		# The allowlist returns verbs, never filenames.
		for verb in scripts_catalog.allowed_scripts():
			self.assertFalse(verb.endswith((".py", ".sh")), verb)

	def test_resolve_finds_file_by_verb(self) -> None:
		path = scripts_catalog.resolve("start-vm")
		self.assertEqual(path.name, "start-vm.py")
