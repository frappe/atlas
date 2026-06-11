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

import json
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

# Runs in a clean interpreter: load provision-vm.py by path (its sys.path shim
# brings in scripts/lib), build a minimal ProvisionInputs, and emit the
# generated launcher plus the snapshot paths as JSON for the asserts below.
_LAUNCHER_DRIVER = """
import importlib.util, json, sys

spec = importlib.util.spec_from_file_location("provision_vm", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from atlas.paths import VirtualMachinePaths

uuid = "12345678-1234-1234-1234-123456789abc"
inputs = module.ProvisionInputs(
    virtual_machine_name=uuid,
    image_name="img",
    kernel_filename="vmlinux",
    rootfs_filename="rootfs.squashfs",
    vcpus=1,
    memory_mb=512,
    disk_gb=2,
    mac_address="06:00:01:02:03:04",
    tap_device="atlas-12345678",
    virtual_machine_ipv6="2001:db8::2",
    ipv4_host_cidr="100.64.0.1/30",
    ipv4_guest_cidr="100.64.0.2/30",
    ipv4_gateway="100.64.0.1",
    ssh_public_key="ssh-ed25519 AAAA",
    atlas_fc_uid=12345,
    atlas_netns="atlas-ns",
    host_veth="ave-h",
    namespace_veth="ave-n",
    cgroup_arg=["cpu.max=100000 100000"],
    resource_arg=[],
)
paths = VirtualMachinePaths(uuid)
print(json.dumps({
    "launcher": module._jailer_launch(inputs, paths),
    "jail_root": paths.jail_root,
    "directory": paths.memory_snapshot_directory,
    "marker": paths.memory_snapshot_marker,
    "vmstate": paths.memory_snapshot_vmstate,
    "mem": paths.memory_snapshot_mem,
    "vmstate_in_jail": paths.memory_snapshot_vmstate_in_jail,
    "mem_in_jail": paths.memory_snapshot_mem_in_jail,
}))
"""


class TestMemorySnapshotLauncher(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		result = subprocess.run(
			[sys.executable, "-c", _LAUNCHER_DRIVER, str(_SCRIPTS_DIR / "provision-vm.py")],
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr
		cls.data = json.loads(result.stdout)

	def test_snapshot_paths_live_inside_the_jail(self) -> None:
		# Inside the jail so the jailed Firecracker (per-VM uid) can write the
		# pair and terminate's rm -rf of the VM directory sweeps it.
		jail_root = self.data["jail_root"]
		for key in ("directory", "marker", "vmstate", "mem"):
			self.assertTrue(self.data[key].startswith(jail_root + "/"), self.data[key])
		# The marker is what every party keys off — one well-known name.
		self.assertEqual(self.data["marker"], self.data["directory"] + "/READY")
		# The API bodies use jail-RELATIVE paths (resolved post-chroot), and they
		# must name the same files as the host-absolute forms.
		self.assertEqual(self.data["jail_root"] + "/" + self.data["vmstate_in_jail"], self.data["vmstate"])
		self.assertEqual(self.data["jail_root"] + "/" + self.data["mem_in_jail"], self.data["mem"])

	def test_launcher_cold_boots_by_default_and_goes_idle_on_marker(self) -> None:
		launcher = self.data["launcher"]
		# Default: --config-file boot, exactly as before the feature.
		self.assertIn("boot_args=(--config-file firecracker.json)", launcher)
		# Marker present: empty boot args, so Firecracker starts idle for
		# vm-restore.py's /snapshot/load (pre-boot only).
		self.assertIn(f"if [[ -f {self.data['marker']} ]]", launcher)
		self.assertIn("boot_args=()", launcher)
		self.assertIn('"${boot_args[@]}"', launcher)
		# The conditional must come before the exec line that consumes it.
		self.assertLess(launcher.index("boot_args=("), launcher.index("exec /usr/local/bin/jailer"))

	def test_launcher_parses(self) -> None:
		with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
			handle.write(self.data["launcher"])
			handle.flush()
			result = subprocess.run(["bash", "-n", handle.name], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)


class TestMemorySnapshotScripts(unittest.TestCase):
	def test_snapshot_stop_cli_contract(self) -> None:
		# --help proves the argparse contract (both required flags declared)
		# without touching a host.
		result = subprocess.run(
			[sys.executable, str(_SCRIPTS_DIR / "snapshot-stop-vm.py"), "--help"],
			capture_output=True,
			text=True,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("--virtual-machine-name", result.stdout)
		self.assertIn("--atlas-fc-uid", result.stdout)

	def test_vm_restore_compiles(self) -> None:
		# vm-restore.py imports the DURABLE package that only exists next to it
		# on a bootstrapped host, so it can't be imported here — but it must at
		# least be valid Python before bootstrap uploads it.
		py_compile.compile(str(_SCRIPTS_DIR / "vm-restore.py"), doraise=True)


class TestMemorySnapshotWiring(unittest.TestCase):
	def test_unit_restores_after_start(self) -> None:
		unit = (_SCRIPTS_DIR / "systemd" / "firecracker-vm@.service").read_text()
		self.assertIn("ExecStartPost=/usr/bin/python3 /var/lib/atlas/bin/vm-restore.py %i", unit)
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

		self.assertIn("vm-restore.py", scripts_catalog.SYSTEMD_HOOKS)
		self.assertNotIn("vm-restore.py", scripts_catalog.allowed_scripts())
		# The fast stop IS a Task (the controller invokes it), but not one the
		# Run Task picker should offer.
		self.assertIn("snapshot-stop-vm.py", scripts_catalog.allowed_scripts())
		self.assertNotIn("snapshot-stop-vm.py", scripts_catalog.operator_visible_scripts())
