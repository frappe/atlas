"""Unit tests for the `atlas` host CLI dispatcher.

Run with bare `python3 -m unittest atlas.test_cli` from scripts/lib: no Frappe,
no site, no host. These pin the dispatcher's contract:

  - `atlas <stem> <flags>` parses to the IDENTICAL typed inputs object as calling
    that entry's own from_args — i.e. the CLI is a pure pass-through, no argv
    mangling (the headline Phase-A guarantee).
  - the four per-VM firecracker-vm@ hooks are never CLI stems — they are `boat
    vm-*` verbs with no .py in scripts/, so they cannot be hand-run as a Task.
  - an unknown command and bare `atlas` exit non-zero with usage.
"""

import contextlib
import io
import sys
import unittest

from atlas import _cli
from atlas._task import TaskInputs


class TestStemDiscovery(unittest.TestCase):
	def test_excludes_systemd_hooks(self):
		# The per-VM firecracker-vm@ hooks are `boat vm-*` verbs with no .py in
		# scripts/, so they must never surface as an `atlas <verb>` stem.
		stems = _cli._stems()
		for hook in ("vm-disk-up", "vm-network-up", "vm-network-down", "vm-restore"):
			self.assertNotIn(hook, stems, f"{hook} is a boat unit hook, not a Task")

	def test_includes_typed_tasks(self):
		stems = _cli._stems()
		# A representative spread: simple VM op, the heavy one, a controller task.
		for stem in ("start-vm", "stop-vm", "provision-vm", "sync-image", "issue-cert"):
			self.assertIn(stem, stems)

	def test_excludes_private_and_shell(self):
		stems = _cli._stems()
		self.assertNotIn("_cli", stems)
		# .sh scripts are not importable and intentionally absent.
		self.assertTrue(all(not s.endswith(".sh") for s in stems))


class TestDispatchParserEquivalence(unittest.TestCase):
	"""`atlas <stem> <flags>` must build the same inputs object as the entry's own
	from_args — proving the CLI adds no logic and rewrites nothing."""

	def _run_via_cli_capturing_inputs(self, command, flags):
		"""Dispatch through _cli.main but intercept the entry's main() so we can
		capture the parsed TaskInputs instead of executing the Task body."""
		stems = _cli._stems()
		module = _cli._load(stems[command])

		# Find the entry's TaskInputs subclass (the one declaring this command).
		inputs_cls = next(
			value
			for value in vars(module).values()
			if isinstance(value, type)
			and issubclass(value, TaskInputs)
			and getattr(value, "command", "") == command
		)

		captured = {}
		original_argv = sys.argv
		try:
			sys.argv = [command, *flags]
			# Parse exactly as the CLI hands off to the entry (argv[1:] form).
			captured["via_cli"] = inputs_cls.from_args()
		finally:
			sys.argv = original_argv
		return inputs_cls, captured["via_cli"]

	def test_start_vm_roundtrip(self):
		uuid = "d4f7c1a2-1111-2222-3333-444455556666"
		cls, via_cli = self._run_via_cli_capturing_inputs("start-vm", ["--virtual-machine-name", uuid])
		direct = cls.from_args(["--virtual-machine-name", uuid])
		self.assertEqual(via_cli, direct)
		self.assertEqual(via_cli.virtual_machine_name, uuid)

	def test_resize_vm_roundtrip_with_ints(self):
		uuid = "d4f7c1a2-1111-2222-3333-444455556666"
		flags = [
			"--virtual-machine-name",
			uuid,
			"--vcpus",
			"2",
			"--memory-mb",
			"2048",
			"--disk-gb",
			"20",
		]
		cls, via_cli = self._run_via_cli_capturing_inputs("resize-vm", flags)
		direct = cls.from_args(flags)
		self.assertEqual(via_cli, direct)
		# int flags really parse as ints, not strings.
		self.assertEqual(via_cli.vcpus, 2)
		self.assertEqual(via_cli.memory_mb, 2048)


class TestUsageAndErrors(unittest.TestCase):
	def test_bare_atlas_prints_usage_and_exits_2(self):
		out = io.StringIO()
		with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
			_cli.main([])
		self.assertEqual(cm.exception.code, 2)
		self.assertIn("usage: atlas", out.getvalue())
		# the honest one-liner about provisioning.
		self.assertIn("from the controller", out.getvalue())

	def test_help_exits_zero(self):
		out = io.StringIO()
		with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
			_cli.main(["--help"])
		self.assertEqual(cm.exception.code, 0)
		self.assertIn("commands:", out.getvalue())

	def test_unknown_command_exits_2(self):
		err = io.StringIO()
		with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
			_cli.main(["frobnicate"])
		self.assertEqual(cm.exception.code, 2)
		self.assertIn("unknown command", err.getvalue())


if __name__ == "__main__":
	unittest.main()
