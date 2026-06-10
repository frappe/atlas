"""Unit tests for the pure half of the OO LVM layer + the typed task I/O.

Run with bare `python3 -m unittest atlas.test_lvm` from scripts/lib: no Frappe,
no site, no droplet, no LVM stack, no mocking. Everything here covers a line
that, as shell, could only be checked on a real host — the parsing the
LVM bench-traps memory records us getting wrong there, plus the typed boundary
that replaces the env-soup and SIZE_BYTES= grepping.
"""

import contextlib
import io
import typing
import unittest
from dataclasses import dataclass

from atlas._task import TaskInputs, TaskResult
from atlas.lvm import (
	DeviceNumber,
	LogicalVolume,
	PoolUsage,
	ProtectedVolumeError,
	ThinPool,
)

UUID = "d4f7c1a2-0000-0000-0000-000000000000"


class TestLogicalVolumeNaming(unittest.TestCase):
	def setUp(self):
		self.pool = ThinPool()

	def test_roles_get_prefixed_names(self):
		self.assertEqual(self.pool.vm_disk(UUID).name, f"atlas-vm-{UUID}")
		self.assertEqual(self.pool.snapshot(UUID).name, f"atlas-snap-{UUID}")
		self.assertEqual(self.pool.base_image("ubuntu-24").name, "atlas-image-ubuntu-24")

	def test_data_disk_roles_get_prefixed_names(self):
		# The data disk and its snapshot are the root disk's peers, named off the
		# same UUID so the pair (VM disk / data disk, snapshot / data-snapshot) is
		# recoverable from the device paths alone.
		self.assertEqual(self.pool.data_disk(UUID).name, f"atlas-data-{UUID}")
		self.assertEqual(self.pool.data_snapshot(UUID).name, f"atlas-datasnap-{UUID}")

	def test_data_snapshot_roundtrips_with_device_path(self):
		ds = self.pool.data_snapshot(UUID)
		self.assertEqual(self.pool.from_device(ds.device_path), ds)

	def test_device_path(self):
		self.assertEqual(self.pool.vm_disk("x").device_path, "/dev/atlas/atlas-vm-x")

	def test_from_device_recovers_the_lv(self):
		lv = self.pool.from_device("/dev/atlas/atlas-snap-d4f7c1a2")
		self.assertEqual(lv.name, "atlas-snap-d4f7c1a2")

	def test_from_device_roundtrips_with_device_path(self):
		snap = self.pool.snapshot(UUID)
		self.assertEqual(self.pool.from_device(snap.device_path), snap)

	def test_custom_volume_group_flows_into_path(self):
		pool = ThinPool(volume_group="vg1")
		self.assertEqual(pool.vm_disk("x").device_path, "/dev/vg1/atlas-vm-x")


class TestDeviceNumber(unittest.TestCase):
	"""The lsblk-whitespace trap from the LVM bench-traps memory, now typed."""

	def test_strips_trailing_pad(self):
		self.assertEqual(DeviceNumber.from_lsblk("252:5  "), DeviceNumber(252, 5))

	def test_strips_newline(self):
		self.assertEqual(DeviceNumber.from_lsblk("252:5\n"), DeviceNumber(252, 5))

	def test_dm_major_with_surrounding_space(self):
		self.assertEqual(DeviceNumber.from_lsblk(" 252:13 \n"), DeviceNumber(252, 13))


class TestPoolUsage(unittest.TestCase):
	def test_below_threshold(self):
		self.assertFalse(PoolUsage.from_lvs("50.00", "12.34").too_full_to_snapshot)

	def test_data_over_threshold(self):
		self.assertTrue(PoolUsage.from_lvs("90.01", "1.00").too_full_to_snapshot)

	def test_metadata_trips_independently(self):
		self.assertTrue(PoolUsage.from_lvs("3.00", "95.50").too_full_to_snapshot)

	def test_exactly_at_threshold_trips(self):
		self.assertTrue(PoolUsage.from_lvs("90.00", "0").too_full_to_snapshot)

	def test_blank_parses_as_zero(self):
		# The `${data_pct:-0}` default: missing lvs output must not crash.
		self.assertFalse(PoolUsage.from_lvs("", "").too_full_to_snapshot)


class TestProtection(unittest.TestCase):
	def setUp(self):
		self.pool = ThinPool()

	def test_pool_and_base_image_protected(self):
		self.assertTrue(LogicalVolume(self.pool.pool_name, self.pool).is_protected)
		self.assertTrue(self.pool.base_image("ubuntu-24").is_protected)

	def test_vm_disk_and_snapshot_removable(self):
		self.assertFalse(self.pool.vm_disk(UUID).is_protected)
		self.assertFalse(self.pool.snapshot(UUID).is_protected)

	def test_data_disk_and_data_snapshot_removable(self):
		# Per-VM data volumes must be lvremovable on terminate/snapshot-delete.
		self.assertFalse(self.pool.data_disk(UUID).is_protected)
		self.assertFalse(self.pool.data_snapshot(UUID).is_protected)

	def test_remove_refuses_protected_before_any_host_call(self):
		with self.assertRaises(ProtectedVolumeError):
			self.pool.base_image("ubuntu-24").remove()


# --- The typed I/O contract that replaces env-soup + SIZE_BYTES= grepping ---


@dataclass(frozen=True)
class _Inputs(TaskInputs):
	command: typing.ClassVar[str] = "demo"
	virtual_machine_name: str
	disk_gigabytes: int
	snapshot_rootfs_path: str = ""  # optional


@dataclass(frozen=True)
class _Result(TaskResult):
	size_bytes: int


def _parse_args_stderr(argv):
	"""Run _Inputs.from_args(argv), capturing argparse's stderr; returns the
	captured text. Asserts it raised SystemExit(2) — argparse's usage-error code."""
	buf = io.StringIO()
	with contextlib.redirect_stderr(buf):
		try:
			_Inputs.from_args(argv)
		except SystemExit as exit:
			assert exit.code == 2, f"expected argparse exit 2, got {exit.code}"
			return buf.getvalue()
	raise AssertionError("expected SystemExit from argparse")


class TestTypedInputs(unittest.TestCase):
	def test_parses_flags_and_coerces_types(self):
		got = _Inputs.from_args(
			[
				"--virtual-machine-name",
				UUID,
				"--disk-gigabytes",
				"20",
			]
		)
		self.assertEqual(got.virtual_machine_name, UUID)
		self.assertEqual(got.disk_gigabytes, 20)  # --disk-gigabytes parsed as int
		self.assertEqual(got.snapshot_rootfs_path, "")  # default

	def test_optional_flag_is_accepted(self):
		got = _Inputs.from_args(
			[
				"--virtual-machine-name",
				UUID,
				"--disk-gigabytes",
				"20",
				"--snapshot-rootfs-path",
				"/dev/atlas/atlas-snap-x",
			]
		)
		self.assertEqual(got.snapshot_rootfs_path, "/dev/atlas/atlas-snap-x")

	def test_field_name_maps_to_kebab_flag(self):
		# snapshot_rootfs_path -> --snapshot-rootfs-path in the generated parser.
		flags = {a.option_strings[0] for a in _Inputs.build_parser()._actions if a.option_strings}
		self.assertIn("--snapshot-rootfs-path", flags)
		self.assertIn("--virtual-machine-name", flags)

	def test_missing_required_names_the_flag(self):
		stderr = _parse_args_stderr(["--disk-gigabytes", "20"])
		self.assertIn("--virtual-machine-name", stderr)

	def test_bad_int_names_the_flag(self):
		stderr = _parse_args_stderr(
			[
				"--virtual-machine-name",
				UUID,
				"--disk-gigabytes",
				"big",
			]
		)
		self.assertIn("--disk-gigabytes", stderr)
		self.assertIn("invalid int value", stderr)


class TestTypedResult(unittest.TestCase):
	def test_emit_parse_roundtrip(self):
		# Controller-side parse recovers the exact typed object the task emitted,
		# even with bash -x trace noise around the marker line.
		from atlas._task import RESULT_MARKER

		emitted = RESULT_MARKER + '{"size_bytes": 21474836480}'
		stdout = f"+ lvcreate ...\n{emitted}\nSnapshotted x.\n"
		self.assertEqual(_Result.parse(stdout), _Result(size_bytes=21474836480))

	def test_parse_takes_the_last_marker(self):
		from atlas._task import RESULT_MARKER

		stdout = f'{RESULT_MARKER}{{"size_bytes": 1}}\n{RESULT_MARKER}{{"size_bytes": 2}}\n'
		self.assertEqual(_Result.parse(stdout).size_bytes, 2)

	def test_missing_marker_raises(self):
		# Unlike the old _parse_size_bytes (silently 0), a declared result must
		# be produced — a truncated run is a loud failure, not a silent 0.
		with self.assertRaises(ValueError):
			_Result.parse("+ lvcreate ...\nno result here\n")


if __name__ == "__main__":
	unittest.main()
