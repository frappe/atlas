"""Memory-snapshot fast path — static and pure checks, no host.

The scripts' lib package is also named `atlas`, which collides with this app's
package inside the bench process, so anything that imports the scripts' lib
(the launcher generator, VirtualMachinePaths) runs in a SUBPROCESS — a fresh
interpreter where scripts/lib wins. The host facts (an actual snapshot-stop /
restore round trip) belong to the vm-lifecycle e2e; these tests pin the
contracts the round trip depends on: the launcher's marker conditional, the
snapshot paths living inside the jail, the systemd wiring, and the new
scripts' CLI/compile health.
"""

import py_compile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

# The memory-snapshot launcher (the jailer boot line with its marker/metadata
# conditionals and the in-jail snapshot paths) was rendered by provision-vm.py and
# tested here by loading that module in a clean interpreter. The .py is deleted —
# boat renders the launch and its config — so those checks now live in boat's
# internal/provision/render_test.go. The systemd-wiring and vm-restore contracts
# below stay: vm-restore.py is a systemd hook, not a boat verb.


class TestMemorySnapshotScripts(unittest.TestCase):
	# snapshot-stop-vm's argparse contract was proven here by `snapshot-stop-vm.py
	# --help`; the .py is deleted (boat serves snapshot-vm's stop), so its flag
	# contract now lives in boat's internal/snapshot/snapshot_stop_test.go.

	def test_vm_restore_compiles(self) -> None:
		# vm-restore.py imports the DURABLE package that only exists next to it
		# on a bootstrapped host, so it can't be imported here — but it must at
		# least be valid Python before bootstrap uploads it.
		py_compile.compile(str(_SCRIPTS_DIR / "vm-restore.py"), doraise=True)


class TestMemorySnapshotWiring(unittest.TestCase):
	def test_unit_restores_after_start(self) -> None:
		unit = (_SCRIPTS_DIR / "systemd" / "firecracker-vm@.service").read_text()
		self.assertIn("ExecStartPost=/usr/local/bin/boat vm-restore %i", unit)
		# The pre-start jail cleanup must NOT sweep the snapshot directory, or a
		# stop-with-snapshot could never be restored.
		for line in unit.splitlines():
			if line.startswith("ExecStartPre=") and "rm" in line:
				self.assertNotIn("snapshot", line)

	def test_bootstrap_uploads_the_restore_hook(self) -> None:
		from atlas.atlas.doctype.server.server import Server

		self.assertIn(
			("vm-restore.py", "/var/lib/atlas/bin/vm-restore.py"),
			Server.BOOTSTRAP_UPLOAD_SOURCES,
		)

	def test_restore_hook_is_not_a_task(self) -> None:
		from atlas.atlas import scripts_catalog

		# SYSTEMD_HOOKS / allowed_scripts() speak VERBS (suffix-less).
		self.assertIn("vm-restore", scripts_catalog.SYSTEMD_HOOKS)
		self.assertNotIn("vm-restore", scripts_catalog.allowed_scripts())
		# The fast stop IS a Task (the controller invokes it), but not one the
		# Run Task picker should offer.
		self.assertIn("snapshot-stop-vm", scripts_catalog.allowed_scripts())
		self.assertNotIn("snapshot-stop-vm", scripts_catalog.operator_visible_scripts())
