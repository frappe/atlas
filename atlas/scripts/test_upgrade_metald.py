import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "linux", "The upgrade script requires Linux tools")
class TestMetaldUpgradeRollback(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name)
		self.commands = self.root / "commands"
		self.commands.mkdir()
		self.state = self.root / "state"
		self.state.mkdir()
		self.marker = self.state / "patches.json"
		self.marker.write_text('["existing_patch"]')
		(self.state / "metald.toml").write_text('listen = "[::1]:9000"\n')
		self.binary = self.root / "metald"
		self.mesh = self.root / "mesh"
		self.candidate = self.root / "candidate"
		self.mesh_candidate = self.root / "mesh-candidate"
		self.write_binary(self.binary, "baseline", ["existing_patch"])
		self.write_binary(self.mesh, "mesh-baseline", [])
		self.write_binary(self.candidate, "rescue-v1", ["existing_patch", "recognize_rescue_state"])
		self.write_binary(self.mesh_candidate, "mesh-candidate", [])
		source = Path(__file__).with_name("upgrade-metald.sh").read_text()
		# Relocate only host paths. The upgrade control flow runs unchanged.
		source = source.replace("/var/lib/metal", str(self.state))
		source = source.replace("/run/lock/metald-install.lock", str(self.root / "install.lock"))
		self.script = self.root / "upgrade.sh"
		self.script.write_text(source)
		stub = self.commands / "stub"
		stub.write_text(f"#!{sys.executable}\n" + COMMAND_STUB)
		stub.chmod(0o755)
		for command in ("id", "curl", "systemctl", "sleep", "journalctl"):
			(self.commands / command).symlink_to(stub)

	def write_binary(self, path, version, patches):
		patch_arguments = " ".join(f"'{patch}'" for patch in patches) or "''"
		path.write_text(
			'#!/bin/sh\ncase "$1" in\n'
			f"version) echo '{version}' ;;\n"
			f"patches) printf '%s\\n' {patch_arguments} ;;\n"
			"*) exit 1 ;;\nesac\n"
		)
		path.chmod(0o755)

	def upgrade(self, bad_hash=False):
		environment = os.environ | {
			"PATH": str(self.commands) + os.pathsep + os.environ["PATH"],
			"UPGRADE_TEST_DIRECTORY": str(self.root),
			"METALD_BINARY_PATH": str(self.binary),
			"MESH_BINARY_PATH": str(self.mesh),
			"METALD_DOWNLOAD_URL": str(self.candidate),
			"METALD_SHA256": "0" * 64
			if bad_hash
			else hashlib.sha256(self.candidate.read_bytes()).hexdigest(),
			"WG_MESH_DOWNLOAD_URL": str(self.mesh_candidate),
			"WG_MESH_SHA256": hashlib.sha256(self.mesh_candidate.read_bytes()).hexdigest(),
			"COORDINATION_LISTEN_ADDRESS": "[::1]:9001",
		}
		return subprocess.run(
			["bash", str(self.script)], env=environment, capture_output=True, text=True, timeout=30
		)

	def test_first_upgrade_failure_keeps_rescue_binary(self):
		candidate = self.candidate.read_bytes()
		result = self.upgrade()
		self.assertNotEqual(result.returncode, 0)
		self.assertIn("keeping rescue-v1", result.stderr)
		self.assertEqual(self.binary.read_bytes(), candidate)
		self.assertIn("recognize_rescue_state", json.loads(self.marker.read_text()))
		self.assertEqual(json.loads((self.state / "status.json").read_text()), {"rescue_enabled": False})

	def test_retry_cannot_restore_pre_rescue_binary(self):
		self.assertNotEqual(self.upgrade().returncode, 0)
		(self.root / "restarts").unlink()
		result = self.upgrade()
		self.assertNotEqual(result.returncode, 0)
		self.assertNotIn("keeping rescue-v1", result.stderr)
		self.assertEqual(self.binary.read_bytes(), self.candidate.read_bytes())
		self.assertEqual((self.root / "restarts").read_text(), "2")

	def test_later_failure_restores_compatible_binary(self):
		self.binary.write_bytes(self.candidate.read_bytes())
		previous = self.binary.read_bytes()
		previous_mesh = self.mesh.read_bytes()
		self.marker.write_text(json.dumps(["existing_patch", "recognize_rescue_state"]))
		self.write_binary(self.candidate, "rescue-v2", ["existing_patch", "recognize_rescue_state"])
		result = self.upgrade()
		self.assertNotEqual(result.returncode, 0)
		self.assertEqual(self.binary.read_bytes(), previous)
		self.assertEqual(self.mesh.read_bytes(), previous_mesh)
		self.assertEqual((self.root / "restarts").read_text(), "2")

	def test_bad_download_keeps_existing_binaries_and_state(self):
		paths = [self.binary, self.mesh, self.marker, self.state / "metald.toml"]
		before = {path: path.read_bytes() for path in paths}
		result = self.upgrade(bad_hash=True)
		self.assertNotEqual(result.returncode, 0)
		self.assertIn("has hash", result.stderr)
		self.assertEqual({path: path.read_bytes() for path in paths}, before)
		self.assertFalse((self.root / "restarts").exists())


COMMAND_STUB = """import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(os.environ["UPGRADE_TEST_DIRECTORY"])
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
if command == "id":
    print("0")
elif command == "curl":
    shutil.copyfile(arguments[-1], arguments[arguments.index("-o") + 1])
elif command == "systemctl":
    counter = root / "restarts"
    count = int(counter.read_text()) if counter.exists() else 0
    if arguments[0] == "restart":
        counter.write_text(str(count + 1))
        patches = subprocess.check_output([str(root / "metald"), "patches"], text=True).split()
        (root / "state/patches.json").write_text(json.dumps(patches))
        (root / "state/status.json").write_text('{"rescue_enabled":false}')
    elif arguments[0] == "show":
        field = arguments[arguments.index("-p") + 1]
        if field == "NotifyAccess":
            print("main")
        elif field == "StatusText":
            print("not ready")
        elif field == "NFileDescriptorStore":
            print("1")
        elif field == "FileDescriptorStorePreserve":
            print("yes")
    elif arguments[0] == "is-active":
        sys.exit(0 if count >= 2 else 1)
    elif arguments[0] == "list-units":
        print("metal-vm@test.service loaded active running")
"""


if __name__ == "__main__":
	unittest.main()
