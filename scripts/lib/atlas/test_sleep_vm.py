"""Unit tests for sleep-vm.py's wake-trap preflight (spec/32-sleepy-vms.md).

Run with bare `python3 -m unittest atlas.test_sleep_vm` from scripts/lib, like
test_park.py: no Frappe, no site, no host.

The guard under test exists because the failure it prevents is silent. A host
with the scripts synced but the unit never installed will park a sleeping VM and
never wake it — the sleep Task reports success and the VM is simply unreachable
until an operator intervenes.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2]


def _load_sleep_vm():
	if str(_SCRIPTS_DIR / "lib") not in sys.path:
		sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
	spec = importlib.util.spec_from_file_location("sleep_vm", _SCRIPTS_DIR / "sleep-vm.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestRequireWakeTrap(unittest.TestCase):
	def setUp(self) -> None:
		self.sleep_vm = _load_sleep_vm()
		self.asked: list[str] = []

	def _with_trap(self, active: bool):
		def _run_ok(template, *args, **kwargs):
			self.asked.append(template.format(*args) if "{}" in template else template)
			return active

		original = self.sleep_vm.run_ok
		self.sleep_vm.run_ok = _run_ok
		self.addCleanup(setattr, self.sleep_vm, "run_ok", original)

	def test_passes_when_the_trap_is_active(self) -> None:
		self._with_trap(True)
		self.sleep_vm._require_wake_trap()  # must not raise
		self.assertTrue(any("atlas-wake-trap.service" in c for c in self.asked))

	def test_refuses_when_the_trap_is_inactive(self) -> None:
		# Exiting non-zero fails the Task, so the controller leaves the VM Running
		# rather than parking it somewhere nothing can wake it.
		self._with_trap(False)
		with self.assertRaises(SystemExit) as caught:
			self.sleep_vm._require_wake_trap()
		message = str(caught.exception)
		self.assertIn("atlas-wake-trap.service", message)
		self.assertIn("Bootstrap the server", message, "the message must name the fix")

	def test_checks_the_trap_before_touching_the_vm(self) -> None:
		# Ordering is the point: the guard runs before the snapshot preflight, so a
		# trap-less host never gets as far as pausing vCPUs or stopping the unit.
		source = (_SCRIPTS_DIR / "sleep-vm.py").read_text()
		body = source.split("def main()", 1)[1]
		self.assertLess(
			body.index("_require_wake_trap()"),
			body.index("_preflight(paths)"),
			"the wake-trap guard must run before the snapshot preflight",
		)


if __name__ == "__main__":
	unittest.main()
