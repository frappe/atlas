"""Warm snapshot fan-out — static and pure checks, no host.

Same split as test_memory_snapshot.py: the scripts' lib package is also named
`atlas` (collides with this app inside the bench process), so anything that
imports the scripts' lib runs in a SUBPROCESS; pure stdlib files (hostinfo, the
in-guest freshen) load via importlib by path. The host facts — a real warm bake,
a real restore, two genuinely distinct clones — belong to the warm-restore e2e;
these tests pin the contracts that round trip depends on: the launcher's
metadata/marker conditionals, MMDS in every Firecracker config, the staging
paths, the identity payload's derivation rules, the clone-time size pinning,
the per-server warm resolution, and the durable-artifact GC wiring.
"""

import json
import py_compile
import subprocess
import sys
import tempfile
import unittest
from importlib import util as importlib_util
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_BENCH_DIR = _REPO_ROOT / "bench"

# Runs in a clean interpreter: load provision-vm.py by path (its sys.path shim
# brings in scripts/lib), build WARM ProvisionInputs, and emit the launcher,
# the firecracker config, the MMDS payload and the relevant paths as JSON.
_WARM_DRIVER = """
import importlib.util, json, sys

spec = importlib.util.spec_from_file_location("provision_vm", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from atlas.paths import VirtualMachinePaths, warm_snapshot_directory

uuid = "12345678-1234-1234-1234-123456789abc"
inputs = module.ProvisionInputs(
    virtual_machine_name=uuid,
    image_name="img",
    kernel_filename="vmlinux",
    rootfs_filename="rootfs.squashfs",
    vcpus=2,
    memory_mb=2048,
    disk_gb=12,
    mac_address="06:00:01:02:03:04",
    tap_device="atlas-golden999",
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
    warm_snapshot_directory="/var/lib/atlas/snapshots/snapid",
)
paths = VirtualMachinePaths(uuid)
print(json.dumps({
    "launcher": module._jailer_launch(inputs, paths),
    "config": json.loads(module._firecracker_config(inputs)),
    "metadata": json.loads(module._mmds_metadata(inputs)),
    "marker": paths.memory_snapshot_marker,
    "metadata_file": paths.metadata_file,
    "signature": paths.memory_snapshot_signature,
    "warm_directory": warm_snapshot_directory("snapid"),
}))
"""


def _load_by_path(name: str, path: Path):
	"""Import a stdlib-only file directly (no scripts/lib package imports)."""
	spec = importlib_util.spec_from_file_location(name, path)
	module = importlib_util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestWarmProvision(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		result = subprocess.run(
			[sys.executable, "-c", _WARM_DRIVER, str(_SCRIPTS_DIR / "provision-vm.py")],
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr
		cls.data = json.loads(result.stdout)

	def test_launcher_adds_metadata_on_cold_path_only(self) -> None:
		launcher = self.data["launcher"]
		# The metadata conditional rides the cold (config-file) path so the
		# cold-boot FALLBACK still adopts the clone identity via --metadata.
		self.assertIn(f"if [[ -f {self.data['metadata_file']} ]]", launcher)
		self.assertIn("boot_args+=(--metadata metadata.json)", launcher)
		# The marker conditional comes AFTER and resets boot_args to empty: the
		# idle (restore) launch passes neither --config-file nor --metadata —
		# vm-restore.py PUTs the payload over the API instead.
		self.assertLess(launcher.index("--metadata"), launcher.index("boot_args=()"))
		self.assertIn(f"if [[ -f {self.data['marker']} ]]", launcher)

	def test_launcher_parses(self) -> None:
		with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
			handle.write(self.data["launcher"])
			handle.flush()
			result = subprocess.run(["bash", "-n", handle.name], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_every_config_carries_mmds(self) -> None:
		# MMDS on every VM: the golden's vmstate must carry the MMDS-enabled net
		# device, and a uniform config keeps every VM bakeable. V1 pinned.
		mmds = self.data["config"]["mmds-config"]
		self.assertEqual(mmds["version"], "V1")
		self.assertEqual(mmds["network_interfaces"], ["eth0"])

	def test_metadata_identity_matches_inject_identity_rules(self) -> None:
		identity = self.data["metadata"]["identity"]
		self.assertEqual(identity["uuid"], "12345678-1234-1234-1234-123456789abc")
		# Same derivations as rootfs.Identity, so a warm clone's identity equals
		# what a cold provision of the same UUID would have injected.
		self.assertEqual(identity["hostname"], "atlas-12345678")
		self.assertEqual(identity["machine_id"], "12345678123412341234123456789abc")
		self.assertEqual(identity["ipv6"], "2001:db8::2")
		self.assertEqual(identity["ipv4_cidr"], "100.64.0.2/30")
		self.assertEqual(identity["ipv4_gateway"], "100.64.0.1")
		self.assertEqual(identity["ssh_public_key"], "ssh-ed25519 AAAA")

	def test_durable_artifacts_live_under_snapshots(self) -> None:
		self.assertEqual(self.data["warm_directory"], "/var/lib/atlas/snapshots/snapid")
		# The staged signature sits beside the marker inside the jail.
		self.assertEqual(
			self.data["signature"],
			self.data["marker"].rsplit("/", 1)[0] + "/host-signature.json",
		)


class TestWarmScripts(unittest.TestCase):
	def test_warm_snapshot_cli_contract(self) -> None:
		result = subprocess.run(
			[sys.executable, str(_SCRIPTS_DIR / "warm-snapshot-vm.py"), "--help"],
			capture_output=True,
			text=True,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		for flag in (
			"--virtual-machine-name",
			"--atlas-fc-uid",
			"--snapshot-rootfs-path",
			"--memory-directory",
		):
			self.assertIn(flag, result.stdout)

	def test_delete_snapshot_gains_memory_directory(self) -> None:
		result = subprocess.run(
			[sys.executable, str(_SCRIPTS_DIR / "delete-snapshot-vm.py"), "--help"],
			capture_output=True,
			text=True,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("--memory-directory", result.stdout)

	def test_vm_restore_compiles_and_guards(self) -> None:
		py_compile.compile(str(_SCRIPTS_DIR / "vm-restore.py"), doraise=True)
		source = (_SCRIPTS_DIR / "vm-restore.py").read_text()
		# The signature guard must consume the marker BEFORE any load attempt,
		# and the MMDS PUT must happen while still paused (before the resume).
		self.assertIn("_signature_mismatch", source)
		self.assertIn("_stage_mmds", source)
		self.assertLess(source.index("_signature_mismatch(paths)"), source.index("_load_snapshot(paths)"))

	def test_hostinfo_parses(self) -> None:
		hostinfo = _load_by_path("hostinfo_under_test", _SCRIPTS_DIR / "lib" / "atlas" / "hostinfo.py")
		cpuinfo = (
			"processor\t: 0\n"
			"model name\t: Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz\n"
			"microcode\t: 0x5003604\n"
			"flags\t\t: fpu vme de sse2\n"
			"\n"
			"processor\t: 1\n"
			"model name\t: SHOULD NOT BE READ\n"
		)
		signature = hostinfo.parse_cpu_signature(cpuinfo)
		self.assertEqual(signature["cpu_model"], "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz")
		self.assertEqual(signature["microcode"], "0x5003604")
		# Flag ORDER must not matter — the set is hashed sorted.
		reordered = cpuinfo.replace("fpu vme de sse2", "sse2 de vme fpu")
		self.assertEqual(
			signature["cpu_flags_sha256"],
			hostinfo.parse_cpu_signature(reordered)["cpu_flags_sha256"],
		)
		self.assertEqual(hostinfo.parse_firecracker_version("Firecracker v1.15.1\n"), "v1.15.1")


class TestGuestWarmTooling(unittest.TestCase):
	def test_warm_sh_parses_and_arms_the_capture(self) -> None:
		result = subprocess.run(["bash", "-n", str(_BENCH_DIR / "warm.sh")], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		source = (_BENCH_DIR / "warm.sh").read_text()
		self.assertIn("atlas-warm-freshen", source)  # the unit must be live at capture
		self.assertIn("setup production", source)  # the stack must be UP in the frozen RAM
		self.assertIn("rm -f /var/lib/systemd/random-seed", source)  # clone-entropy hygiene
		self.assertIn("/etc/atlas-vm-uuid", source)  # the adopted-identity marker

	def test_freshen_pure_helpers(self) -> None:
		freshen = _load_by_path("freshen_under_test", _BENCH_DIR / "atlas-warm-freshen.py")
		identity = {
			"ipv6": "2001:db8::5",
			"ipv4_cidr": "100.64.0.6/30",
			"ipv4_gateway": "100.64.0.5",
		}
		env = freshen.network_env(identity)
		# Byte-shape of the host's _write_network_env, so atlas-network.service
		# works unchanged on the clone's next plain reboot.
		self.assertEqual(
			env,
			"VIRTUAL_MACHINE_IPV6=2001:db8::5\n"
			"VIRTUAL_MACHINE_IPV4=100.64.0.6/30\n"
			"VIRTUAL_MACHINE_IPV4_GATEWAY=100.64.0.5\n",
		)
		hosts = freshen.hosts_lines("127.0.0.1 localhost\n127.0.1.1\tatlas-golden00\n", "atlas-12345678")
		self.assertIn("127.0.0.1 localhost", hosts)
		self.assertNotIn("atlas-golden00", hosts)  # the golden's entry is replaced, not accumulated
		self.assertIn("127.0.1.1\tatlas-12345678", hosts)

	def test_deploy_site_gains_warm_flag(self) -> None:
		result = subprocess.run(
			[sys.executable, str(_BENCH_DIR / "deploy-site.py"), "--help"],
			capture_output=True,
			text=True,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("--warm-vm-uuid", result.stdout)


def _ensure_server() -> str:
	provider = make_provider("warm-test-provider")
	server = make_server(
		provider,
		"warm-test-server",
		ipv4_address="10.0.0.98",
		ipv6_address="2001:db8:2::1",
		ipv6_prefix="2001:db8:2::/64",
		ipv6_virtual_machine_range="2001:db8:2::/124",
		status="Active",
	)
	return server.name


def _make_warm_snapshot(server: str, **overrides) -> "frappe.model.document.Document":
	doc = {
		"doctype": "Virtual Machine Snapshot",
		"title": "golden-bench",
		"virtual_machine": "gone-build-vm",
		"server": server,
		"status": "Available",
		"kind": "Warm",
		"source_image": make_image("warm-test-image").name,
		"disk_gigabytes": 12,
		"rootfs_path": "/dev/atlas/atlas-snap-warm1",
		"memory_directory": "/var/lib/atlas/snapshots/warm1",
		"vcpus": 2,
		"memory_megabytes": 2048,
		"tap_device": "atlas-golden999",
	}
	doc.update(overrides)
	snapshot = frappe.get_doc(doc)
	# The golden's build VM is scratch and may be long gone (its whole point);
	# the dangling link is the production reality these tests model.
	snapshot.flags.ignore_links = True
	return snapshot.insert(ignore_permissions=True)


class TestWarmClone(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = _ensure_server()
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine Snapshot", pluck="name"):
			with patch(
				"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_task",
				return_value=fake_task(),
			):
				frappe.delete_doc("Virtual Machine Snapshot", name, force=1, ignore_permissions=True)

	def test_warm_clone_pins_captured_size_and_carries_warm_fields(self) -> None:
		snapshot = _make_warm_snapshot(self.server)
		clone_name = snapshot.clone_to_new_vm(title="clone", ssh_public_key="ssh-ed25519 AAAA")
		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertEqual(clone.vcpus, 2)
		self.assertEqual(clone.memory_megabytes, 2048)
		self.assertEqual(clone.disk_gigabytes, 12)
		self.assertEqual(clone.warm_snapshot, snapshot.name)
		# The vmstate binds the tap by NAME; the clone's netns recreates it.
		self.assertEqual(clone.tap_device, "atlas-golden999")
		self.assertEqual(clone.clone_source_rootfs, snapshot.rootfs_path)

	def test_warm_clone_rejects_mismatched_overrides(self) -> None:
		snapshot = _make_warm_snapshot(self.server)
		for kwargs in (
			{"vcpus": 4},
			{"memory_megabytes": 4096},
			{"disk_gigabytes": 40},
		):
			with self.assertRaises(frappe.ValidationError):
				snapshot.clone_to_new_vm(title="clone", ssh_public_key="k", **kwargs)

	def test_warm_clone_allows_cpu_cap_override(self) -> None:
		snapshot = _make_warm_snapshot(self.server)
		clone_name = snapshot.clone_to_new_vm(title="clone", ssh_public_key="k", cpu_max_cores=0.25)
		self.assertEqual(frappe.db.get_value("Virtual Machine", clone_name, "cpu_max_cores"), 0.25)

	def test_warm_clone_provision_variables(self) -> None:
		snapshot = _make_warm_snapshot(self.server)
		clone_name = snapshot.clone_to_new_vm(title="clone", ssh_public_key="k")
		clone = frappe.get_doc("Virtual Machine", clone_name)
		variables = clone._provision_variables()
		self.assertEqual(variables["WARM_SNAPSHOT_DIRECTORY"], "/var/lib/atlas/snapshots/warm1")
		self.assertEqual(variables["SNAPSHOT_ROOTFS_PATH"], "/dev/atlas/atlas-snap-warm1")
		self.assertEqual(variables["TAP_DEVICE"], "atlas-golden999")

	def test_warm_resolution_is_per_server_and_newest(self) -> None:
		from atlas.atlas.placement import warm_bench_snapshot_for_server

		self.assertIsNone(warm_bench_snapshot_for_server(self.server))
		older = _make_warm_snapshot(self.server)
		newer = _make_warm_snapshot(self.server, rootfs_path="/dev/atlas/atlas-snap-warm2")
		_make_warm_snapshot(self.server, status="Pending", rootfs_path="/dev/atlas/atlas-snap-warm3")
		resolved = warm_bench_snapshot_for_server(self.server)
		self.assertIn(resolved, (older.name, newer.name))  # newest Available wins; both beat Pending
		self.assertEqual(resolved, newer.name)

	def test_on_trash_removes_memory_directory(self) -> None:
		snapshot = _make_warm_snapshot(self.server)
		with patch(
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_task",
			return_value=fake_task(),
		) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["MEMORY_DIRECTORY"], "/var/lib/atlas/snapshots/warm1")

	def test_site_provisioning_prefers_warm_for_the_goldens_server(self) -> None:
		from atlas.atlas.doctype.site.site import _provision_backing_vm

		cold = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "golden-bench",
				"virtual_machine": "gone-build-vm",
				"server": self.server,
				"status": "Available",
				"source_image": make_image("warm-test-image").name,
				"disk_gigabytes": 12,
				"rootfs_path": "/dev/atlas/atlas-snap-cold1",
			}
		)
		cold.flags.ignore_links = True
		cold.insert(ignore_permissions=True)
		frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", cold.name)
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAA fleet")
		warm = _make_warm_snapshot(self.server)

		class FakeSite:
			name = "warm-site.example.test"

		clone_name = _provision_backing_vm(FakeSite())
		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertEqual(clone.warm_snapshot, warm.name)
		self.assertEqual(clone.vcpus, 2)  # captured size, not the tier's
		self.assertEqual(clone.cpu_max_cores, 0.25)  # the tier's cgroup cap still applies


class TestWarmImageBuild(IntegrationTestCase):
	def test_warm_rejected_for_recipes_without_entrypoint(self) -> None:
		server = _ensure_server()
		with self.assertRaises(frappe.ValidationError) as raised:
			frappe.get_doc(
				{
					"doctype": "Image Build",
					"recipe": "proxy",
					"server": server,
					"region": "blr1",
					"base_image": make_image("warm-test-image").name,
					"warm": 1,
				}
			).insert(ignore_permissions=True)
		self.assertIn("no warm entrypoint", str(raised.exception))

	def test_bench_recipe_declares_warm_entrypoint(self) -> None:
		from atlas.atlas.image_recipes import get_recipe

		self.assertEqual(get_recipe("bench").warm_entrypoint, "warm.sh")
		self.assertEqual(get_recipe("proxy").warm_entrypoint, "")
		# The committed tree actually carries the entrypoint + the freshen unit
		# (tree_uploads ships everything under bench/).
		self.assertTrue((_BENCH_DIR / "warm.sh").is_file())
		self.assertTrue((_BENCH_DIR / "atlas-warm-freshen.py").is_file())


class TestTerminateKeepsWarmGoldens(IntegrationTestCase):
	def test_delete_snapshots_skips_available_warm(self) -> None:
		server = _ensure_server()
		image = make_image("warm-test-image").name
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "build vm",
				"server": server,
				"image": image,
				"vcpus": 2,
				"memory_megabytes": 2048,
				"disk_gigabytes": 12,
				"ssh_public_key": "k",
			}
		).insert(ignore_permissions=True)
		warm = _make_warm_snapshot(server, virtual_machine=vm.name)
		cold = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "scratch",
				"virtual_machine": vm.name,
				"server": server,
				"status": "Available",
				"rootfs_path": "/dev/atlas/atlas-snap-scratch",
			}
		).insert(ignore_permissions=True)
		with patch(
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_task",
			return_value=fake_task(),
		):
			vm._delete_snapshots()
		self.assertTrue(frappe.db.exists("Virtual Machine Snapshot", warm.name))
		self.assertFalse(frappe.db.exists("Virtual Machine Snapshot", cold.name))


if __name__ == "__main__":
	unittest.main()
