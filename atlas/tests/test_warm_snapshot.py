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

import subprocess
import sys
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

# The warm launcher, firecracker config and MMDS metadata payload were rendered by
# provision-vm.py and tested here by loading that module in a clean interpreter.
# The .py is deleted — boat renders the warm launch, its config and the clone
# identity — so those checks now live in boat's internal/provision/render_test.go
# and clone_test.go. The stdlib-only in-guest pieces below (hostinfo, warm.sh, the
# freshen unit) are unaffected and keep loading by path.


def _load_by_path(name: str, path: Path):
	"""Import a stdlib-only file directly (no scripts/lib package imports)."""
	spec = importlib_util.spec_from_file_location(name, path)
	module = importlib_util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestWarmScripts(unittest.TestCase):
	# warm-snapshot-vm's argparse contract was proven here by `warm-snapshot-vm.py
	# --help`; the .py is deleted (boat serves warm-snapshot-vm), so its flag
	# contract now lives in boat's internal/snapshot/warm_test.go.

	# delete-snapshot-vm's `--memory-directory` flag was proven here by
	# `delete-snapshot-vm.py --help`; the .py is deleted (boat serves
	# delete-snapshot-vm), so its contract now lives in boat's
	# internal/snapshot/delete_snapshot_test.go.

	# vm-restore's signature guard (consume the marker before any load attempt) and
	# its MMDS-PUT-while-paused ordering were proven here by compiling vm-restore.py
	# and asserting on its source. The .py is deleted (`boat vm-restore` serves it),
	# so that ordering now lives in boat's internal/image restore path.

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
		self.assertEqual(hostinfo.parse_firecracker_version("Firecracker v1.16.0\n"), "v1.16.0")


class TestGuestWarmTooling(unittest.TestCase):
	def test_warm_sh_parses_and_arms_the_capture(self) -> None:
		result = subprocess.run(["bash", "-n", str(_BENCH_DIR / "warm.sh")], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		source = (_BENCH_DIR / "warm.sh").read_text()
		self.assertIn("atlas-warm-freshen", source)  # the unit must be live at capture
		# The stack is already UP (build.sh's `bench start` under systemd-mode); warm.sh
		# pre-warms it with real HTTP and asserts it is serving before the freeze, so the
		# frozen RAM answers `pong` the instant a clone resumes.
		self.assertIn("/api/method/ping", source)  # pre-warm + serving assertion
		self.assertIn("*pong*", source)  # the stack must be serving in the frozen RAM
		self.assertIn("rm -f /var/lib/systemd/random-seed", source)  # clone-entropy hygiene
		self.assertIn("/etc/atlas-vm-uuid", source)  # the adopted-identity marker
		# The freshen unit MUST be verified enabled + running BEFORE the freeze, so a
		# golden captured without a live freshen fails the bake loud instead of fanning
		# out into clones that never adopt their identity (unreachable on their own /128).
		self.assertIn("systemctl is-enabled --quiet atlas-warm-freshen.service", source)
		self.assertIn("systemctl is-active --quiet atlas-warm-freshen.service", source)
		# The check must precede the freeze's final sync (the capture point).
		self.assertLess(source.index("is-active --quiet atlas-warm-freshen"), source.index("\nsync\n"))

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
			subdomain = "warm-site"
			tenant = None

		clone_name = _provision_backing_vm(FakeSite())
		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertEqual(clone.warm_snapshot, warm.name)
		self.assertEqual(clone.vcpus, 2)  # captured size, not the tier's
		self.assertEqual(clone.cpu_max_cores, 0.25)  # the tier's cgroup cap still applies


class TestWarmSnapshotAction(IntegrationTestCase):
	"""The per-VM `capture_warm_snapshot()` operator action — the capture half of the
	Image Builder's warm bake exposed on a live VM. Mocks the host Task and pins
	the row it produces (kind=Warm, captured config, folded to Available) and the
	capture variables, the same contract `image_build._warm_snapshot` relies on."""

	# A real ATLAS_RESULT line so the controller's own parse_result runs.
	RESULT_STDOUT = (
		'ATLAS_RESULT={"size_bytes": 12884901888, "memory_bytes": 2147483648, '
		'"host_signature": "{\\"kernel\\": \\"6.1\\"}"}'
	)

	def setUp(self) -> None:
		self.server = _ensure_server()
		self.image = make_image("warm-test-image").name
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _running_vm(self) -> "frappe.model.document.Document":
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "live vm",
				"server": self.server,
				"image": self.image,
				"vcpus": 2,
				"memory_megabytes": 2048,
				"disk_gigabytes": 12,
				"ssh_public_key": "k",
			}
		).insert(ignore_permissions=True)
		vm.db_set("status", "Running")
		vm.reload()
		return vm

	def test_capture_creates_warm_row_with_captured_config(self) -> None:
		vm = self._running_vm()
		with patch(
			"atlas.atlas.doctype.virtual_machine.virtual_machine.run_task",
			return_value=fake_task(stdout=self.RESULT_STDOUT),
		) as mocked:
			snapshot_name = vm.capture_warm_snapshot(title="hot")
		snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
		self.assertEqual(snapshot.kind, "Warm")
		self.assertEqual(snapshot.status, "Available")
		self.assertEqual(snapshot.virtual_machine, vm.name)
		self.assertEqual(snapshot.source_image, self.image)
		# Captured machine config + tap — the vmstate pins all three.
		self.assertEqual(snapshot.vcpus, 2)
		self.assertEqual(snapshot.memory_megabytes, 2048)
		self.assertEqual(snapshot.disk_gigabytes, 12)
		self.assertEqual(snapshot.tap_device, vm.tap_device)
		# Result folded back from the (mocked) host Task.
		self.assertEqual(snapshot.size_bytes, 12884901888)
		self.assertEqual(snapshot.memory_bytes, 2147483648)
		self.assertEqual(snapshot.rootfs_path, f"/dev/atlas/atlas-snap-{snapshot_name}")
		self.assertEqual(snapshot.memory_directory, f"/var/lib/atlas/snapshots/{snapshot_name}")
		# The capture ran warm-snapshot-vm.py with the durable artifact targets.
		self.assertEqual(mocked.call_args.kwargs["script"], "warm-snapshot-vm")
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["VIRTUAL_MACHINE_NAME"], vm.name)
		self.assertEqual(variables["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)
		self.assertEqual(variables["MEMORY_DIRECTORY"], snapshot.memory_directory)
		self.assertIn("ATLAS_FC_UID", variables)

	def test_capture_allowed_on_paused_vm(self) -> None:
		vm = self._running_vm()
		vm.db_set("status", "Paused")
		vm.reload()
		with patch(
			"atlas.atlas.doctype.virtual_machine.virtual_machine.run_task",
			return_value=fake_task(stdout=self.RESULT_STDOUT),
		):
			self.assertTrue(vm.capture_warm_snapshot())

	def test_capture_rejected_on_stopped_vm(self) -> None:
		vm = self._running_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		with self.assertRaises(frappe.ValidationError):
			vm.capture_warm_snapshot()
		# No half-built row left behind by the rejected capture.
		self.assertFalse(frappe.get_all("Virtual Machine Snapshot", filters={"virtual_machine": vm.name}))


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

	def test_delete_snapshots_keeps_an_image_builds_output(self) -> None:
		"""A COLD bake's snapshot is neither the registered golden nor warm, so before
		this guard `terminate_build_vm` deleted the very artifact `Image Build.promote()`
		needs — leaving the build pointing at a row that no longer exists. Retiring a
		bake artifact is an explicit operator act, never a side effect of reaping the
		scratch VM."""
		server = _ensure_server()
		image = make_image("bake-output-image").name
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
		baked = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "bake output",
				"virtual_machine": vm.name,
				"server": server,
				"status": "Available",
				"rootfs_path": "/dev/atlas/atlas-snap-bake",
			}
		).insert(ignore_permissions=True)
		build = frappe.get_doc(
			{
				"doctype": "Image Build",
				"recipe": "bench-v16",
				"server": server,
				"base_image": image,
				"status": "Available",
			}
		).insert(ignore_permissions=True)
		build.db_set("snapshot", baked.name)
		with patch(
			"atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot.run_task",
			return_value=fake_task(),
		):
			vm._delete_snapshots()
		self.assertTrue(frappe.db.exists("Virtual Machine Snapshot", baked.name))


if __name__ == "__main__":
	unittest.main()
