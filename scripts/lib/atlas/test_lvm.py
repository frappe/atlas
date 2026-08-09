"""Unit tests for the pure half of the OO LVM layer + the typed task I/O.

Run with bare `python3 -m unittest atlas.test_lvm` from scripts/lib: no Frappe,
no site, no droplet, no LVM stack, no mocking. Everything here covers a line
that, as shell, could only be checked on a real host — the parsing the
LVM bench-traps memory records us getting wrong there, plus the typed boundary
that replaces the env-soup and SIZE_BYTES= grepping.
"""

import contextlib
import io
import json
import os
import typing
import unittest
from dataclasses import dataclass
from unittest import mock

from atlas._run import _render
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import (
	DeviceNumber,
	LogicalVolume,
	PoolBacking,
	PoolUsage,
	ProtectedVolumeError,
	ThinPool,
	discover_pool_disks,
)

UUID = "d4f7c1a2-0000-0000-0000-000000000000"


def _executed(call) -> str:
	"""Render a mocked `run`/`run_ok` call back into the command line it would have
	executed: the new runner takes `(template, *params)` with `{}` auto-quoted holes,
	so re-render via `_render` and space-join. This lets the existing substring/argv
	assertions check the REAL command independent of how it is templated."""
	template, *params = call.args
	return " ".join(_render(template, tuple(params)))


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


# --- PV backing selection: real NVMe device(s) vs the loopback fallback ---


_DEFAULT_DISK_BYTES = 1 << 40  # 1 TiB — a real pool disk, well over the 1 GiB floor


def _lsblk(*devices) -> str:
	"""Build `lsblk -J` fixture JSON from (name, type, fstype, mountpoint,
	children?) tuples — the shape discover_pool_disks parses.

	A 5th element may be the children list (as before) OR a dict of extra node
	fields (e.g. {"size": 0, "rm": True}) to model a phantom/removable disk. A
	disk with neither defaults to a real fixed disk: 1 TiB, non-removable — so
	every pre-existing "picked" fixture still qualifies under the size/removable
	guard without restating size on each tuple."""
	nodes = []
	for name, dtype, fstype, mountpoint, *rest in devices:
		node = {"name": name, "type": dtype, "fstype": fstype, "mountpoint": mountpoint}
		extra = rest[0] if rest else None
		if isinstance(extra, list):
			node["children"] = extra
		elif isinstance(extra, dict):
			node.update(extra)
		if dtype == "disk":
			node.setdefault("size", _DEFAULT_DISK_BYTES)
			node.setdefault("rm", False)
		nodes.append(node)
	return json.dumps({"blockdevices": nodes})


def _raid_box(data_children=None) -> str:
	"""lsblk -J fixture for the Scaleway RAID-partitioned box: two NVMe disks,
	each p1(uefi)/p2(boot)/p3(root)/p4(data). The md0/md1/md2 arrays are nested
	under their FIRST member partition (md2 also appears under nvme1n1p4 — lsblk
	lists an array under every member, which exercises the dedup). md0/md1 carry a
	mounted ext4; md2 (data) is raw unless `data_children` is given.

	The md array carries SIZE/RM like any block node so the size floor applies.
	"""

	def part(name, raid_child=None):
		node = {"name": name, "type": "part", "fstype": "linux_raid_member", "mountpoint": None}
		if raid_child is not None:
			node["children"] = [raid_child]
		return node

	def md(name, fstype, mountpoint, children=None):
		node = {
			"name": name,
			"type": "raid1",
			"fstype": fstype,
			"mountpoint": mountpoint,
			"size": _DEFAULT_DISK_BYTES,
			"rm": False,
		}
		if children:
			node["children"] = children
		return node

	md0 = md("md0", "ext4", "/boot")
	md1 = md("md1", "ext4", "/")
	md2 = md("md2", None, None, children=data_children)
	nvme0 = _lsblk_node(
		"nvme0n1",
		"disk",
		None,
		None,
		children=[
			{"name": "nvme0n1p1", "type": "part", "fstype": "vfat", "mountpoint": "/boot/efi"},
			part("nvme0n1p2", md0),
			part("nvme0n1p3", md1),
			part("nvme0n1p4", md2),
		],
	)
	# Second disk: md arrays are listed again under its members (same nodes).
	nvme1 = _lsblk_node(
		"nvme1n1",
		"disk",
		None,
		None,
		children=[
			{"name": "nvme1n1p1", "type": "part", "fstype": "vfat", "mountpoint": None},
			part("nvme1n1p2", md0),
			part("nvme1n1p3", md1),
			part("nvme1n1p4", md2),
		],
	)
	return json.dumps({"blockdevices": [nvme0, nvme1]})


def _lsblk_node(name, dtype, fstype, mountpoint, children=None) -> dict:
	"""A single lsblk node dict (the building block _lsblk wraps). A disk defaults
	to a real fixed disk (1 TiB, non-removable) like _lsblk's tuples do."""
	node = {"name": name, "type": dtype, "fstype": fstype, "mountpoint": mountpoint}
	if children is not None:
		node["children"] = children
	if dtype == "disk":
		node.setdefault("size", _DEFAULT_DISK_BYTES)
		node.setdefault("rm", False)
	return node


class TestDiscoverPoolDisks(unittest.TestCase):
	def test_two_blank_nvme_disks_are_picked(self):
		# A Scaleway Elastic Metal box: OS on sda (partitioned + mounted), two
		# raw NVMe drives free. Only the NVMe pair backs the pool.
		out = _lsblk(
			("sda", "disk", None, None, [{"name": "sda1", "type": "part", "mountpoint": "/"}]),
			("nvme0n1", "disk", None, None),
			("nvme1n1", "disk", None, None),
		)
		self.assertEqual(discover_pool_disks(out), ["/dev/nvme0n1", "/dev/nvme1n1"])

	def test_result_is_sorted_stably(self):
		out = _lsblk(("nvme1n1", "disk", None, None), ("nvme0n1", "disk", None, None))
		self.assertEqual(discover_pool_disks(out), ["/dev/nvme0n1", "/dev/nvme1n1"])

	def test_partitioned_disk_is_skipped(self):
		out = _lsblk(("sda", "disk", None, None, [{"name": "sda1", "type": "part"}]))
		self.assertEqual(discover_pool_disks(out), [])

	def test_formatted_whole_disk_is_skipped(self):
		# A disk carrying a filesystem directly (no partition table) is in use.
		out = _lsblk(("vdb", "disk", "ext4", None))
		self.assertEqual(discover_pool_disks(out), [])

	def test_mounted_whole_disk_is_skipped(self):
		out = _lsblk(("vdb", "disk", None, "/data"))
		self.assertEqual(discover_pool_disks(out), [])

	def test_loop_and_dm_nodes_are_skipped(self):
		out = _lsblk(("loop0", "loop", None, None), ("dm-0", "lvm", None, None))
		self.assertEqual(discover_pool_disks(out), [])

	def test_stock_droplet_has_no_spare_disk(self):
		# Single partitioned+mounted disk → loopback fallback (empty list).
		out = _lsblk(("vda", "disk", None, None, [{"name": "vda1", "type": "part", "mountpoint": "/"}]))
		self.assertEqual(discover_pool_disks(out), [])

	def test_zero_byte_phantom_disk_is_skipped(self):
		# A Scaleway Elastic Metal box exposes an empty card-reader slot as a
		# 0-byte removable `disk` (/dev/sda) that looks "unused" (no children/
		# fstype/mount) but has no path names — feeding it to pvcreate fails
		# "Device open 8:0 has no path names". It must NOT be selected.
		out = _lsblk(("sda", "disk", None, None, {"size": 0, "rm": True}))
		self.assertEqual(discover_pool_disks(out), [])

	def test_removable_disk_is_skipped(self):
		# Removable media (USB / card reader) is never pool backing, even if it
		# reports a real size.
		out = _lsblk(("sdb", "disk", None, None, {"size": _DEFAULT_DISK_BYTES, "rm": True}))
		self.assertEqual(discover_pool_disks(out), [])

	def test_below_min_size_disk_is_skipped(self):
		# A tiny fixed disk (under 1 GiB) is not usable pool backing.
		out = _lsblk(("sdc", "disk", None, None, {"size": 512 << 20, "rm": False}))
		self.assertEqual(discover_pool_disks(out), [])

	def test_string_size_and_rm_are_tolerated(self):
		# Older lsblk renders size/rm as strings; the guard must still parse them.
		out = _lsblk(
			("sda", "disk", None, None, {"size": "0", "rm": "1"}),
			("nvme0n1", "disk", None, None, {"size": str(_DEFAULT_DISK_BYTES), "rm": "0"}),
		)
		self.assertEqual(discover_pool_disks(out), ["/dev/nvme0n1"])

	def test_live_scaleway_box_picks_nothing(self):
		# The exact live-box topology that broke bootstrap: a 0-byte removable
		# sda phantom + two NVMe disks fully consumed by the root RAID (children).
		# Nothing qualifies → loopback fallback (empty list), no pvcreate on sda.
		out = _lsblk(
			("sda", "disk", None, None, {"size": 0, "rm": True}),
			("nvme0n1", "disk", None, None, [{"name": "nvme0n1p4", "type": "part"}]),
			("nvme1n1", "disk", None, None, [{"name": "nvme1n1p3", "type": "part"}]),
		)
		self.assertEqual(discover_pool_disks(out), [])

	def test_empty_input_is_empty(self):
		self.assertEqual(discover_pool_disks(""), [])
		self.assertEqual(discover_pool_disks('{"blockdevices": []}'), [])

	def test_raw_data_raid_array_is_picked(self):
		# The Scaleway RAID partitioning schema leaves the `data` RAID-1 (/dev/md2)
		# raw for the pool. lsblk nests the md array UNDER a member partition; an
		# unused array (no fstype/mount/children) of usable size qualifies.
		out = _raid_box()
		self.assertEqual(discover_pool_disks(out), ["/dev/md2"])

	def test_data_raid_array_with_lvm_already_on_it_is_skipped(self):
		# Once the pool is built, md2 carries an LVM child — it is no longer a raw
		# candidate (a re-run must not try to re-pvcreate it; ThinPool.ensure guards
		# anyway, but the probe shouldn't surface it).
		out = _raid_box(data_children=[{"name": "atlas-pool0_tdata", "type": "lvm"}])
		self.assertEqual(discover_pool_disks(out), [])

	def test_boot_and_root_raid_arrays_are_skipped(self):
		# md0 (/boot) and md1 (/) carry an ext4 fstype + mountpoint — never pool
		# backing. Only the raw data array (md2) is selected.
		out = _raid_box()
		picked = discover_pool_disks(out)
		self.assertNotIn("/dev/md0", picked)
		self.assertNotIn("/dev/md1", picked)

	def test_raid_array_deduped_across_both_members(self):
		# An md array appears once under EACH member partition in the lsblk tree;
		# the recursion must dedup it to a single device path.
		out = _raid_box()
		self.assertEqual(discover_pool_disks(out).count("/dev/md2"), 1)


class TestImportBaseImageFromLV(unittest.TestCase):
	"""Promote a snapshot LV into a read-only base image LV — the same dd-then-RO
	shape as import_base_image, but the source is a local LV device, not a file.
	No real LVM stack: every host poke (`run`, `run_ok`) is mocked."""

	def setUp(self):
		self.pool = ThinPool()

	def test_idempotent_noop_when_target_exists(self):
		# A re-promote with the image LV already present touches nothing (no dd, no
		# lvcreate) — exactly import_base_image's idempotency.
		with (
			mock.patch("atlas.lvm.run_ok", return_value=True),  # target lv.exists -> True
			mock.patch("atlas.lvm.run") as run,
		):
			lv = self.pool.import_base_image_from_lv(
				self.pool.from_device("/dev/atlas/atlas-snap-x"), "golden-v1", 28
			)
		self.assertEqual(lv.name, "atlas-image-golden-v1")
		run.assert_not_called()

	def test_dd_from_source_device_then_read_only(self):
		# Target absent, source present: create the thin LV, dd the SOURCE LV's
		# device into it, then flip the base read-only. Mirrors import_base_image
		# but reads a block device rather than a file.
		exists = iter([False, True])  # lv.exists -> False ; source.exists -> True

		def run_ok(*args, **kwargs):
			arg_str = " ".join(str(a) for a in args)
			if "lvs" in arg_str:
				return next(exists)
			return True

		with (
			mock.patch("atlas.lvm.run_ok", side_effect=run_ok),
			mock.patch("atlas.lvm.run", return_value="") as run,
			mock.patch.object(LogicalVolume, "_wait_for_node"),  # skip udev/stat on the fake node
		):
			self.pool.import_base_image_from_lv(
				self.pool.from_device("/dev/atlas/atlas-snap-abc"), "golden-v1", 28
			)
		calls = [_executed(c) for c in run.call_args_list]
		# dd reads the SOURCE device, writes the new image device.
		dd = [c for c in calls if "dd" in c]
		self.assertTrue(any("if=/dev/atlas/atlas-snap-abc" in c for c in dd), dd)
		self.assertTrue(any("of=/dev/atlas/atlas-image-golden-v1" in c for c in dd), dd)
		# The base image LV is flipped read-only at the LVM layer.
		self.assertTrue(any("lvchange --permission r" in c for c in calls), calls)

	def test_missing_source_raises_before_lvcreate(self):
		# A vanished source LV (e.g. its build VM was terminated and the snapshot
		# swept) fails loud — never a half-built empty base image.
		exists = iter([False, False])  # lv.exists -> False ; source.exists -> False

		def run_ok(*args, **kwargs):
			if "lvs" in " ".join(str(a) for a in args):
				return next(exists)
			return True

		with (
			mock.patch("atlas.lvm.run_ok", side_effect=run_ok),
			mock.patch("atlas.lvm.run") as run,
		):
			with self.assertRaises(FileNotFoundError):
				self.pool.import_base_image_from_lv(
					self.pool.from_device("/dev/atlas/atlas-snap-gone"), "golden-v1", 28
				)
		# No lvcreate happened: we bailed before touching the pool.
		self.assertFalse(any("lvcreate" in _executed(c) for c in run.call_args_list))


class TestPoolBackingSelection(unittest.TestCase):
	"""select_devices() applies env → persisted → discovered → loopback, without
	touching the host beyond the calls we mock here."""

	def setUp(self):
		self.backing = PoolBacking("/var/lib/atlas/pool", "200G")

	def test_explicit_env_wins_and_splits(self):
		with mock.patch.dict(os.environ, {"ATLAS_POOL_DEVICE": "/dev/nvme0n1, /dev/nvme1n1"}):
			self.assertEqual(self.backing.select_devices(), ["/dev/nvme0n1", "/dev/nvme1n1"])

	def test_persisted_used_when_no_env(self):
		with (
			mock.patch.dict(os.environ, {}, clear=False),
			mock.patch.object(self.backing, "_persisted_devices", return_value=["/dev/nvme0n1"]),
		):
			os.environ.pop("ATLAS_POOL_DEVICE", None)
			self.assertEqual(self.backing.select_devices(), ["/dev/nvme0n1"])

	def test_discovers_when_nothing_persisted(self):
		out = _lsblk(("nvme0n1", "disk", None, None))
		with (
			mock.patch.object(self.backing, "_persisted_devices", return_value=[]),
			mock.patch("atlas.lvm.run", return_value=out) as run,
		):
			os.environ.pop("ATLAS_POOL_DEVICE", None)
			self.assertEqual(self.backing.select_devices(), ["/dev/nvme0n1"])
			run.assert_called_once()  # the lsblk probe

	def test_empty_discovery_falls_back_to_loopback(self):
		with (
			mock.patch.object(self.backing, "_persisted_devices", return_value=[]),
			mock.patch("atlas.lvm.run", return_value='{"blockdevices": []}'),
		):
			os.environ.pop("ATLAS_POOL_DEVICE", None)
			self.assertEqual(self.backing.select_devices(), [])

	def test_state_and_backing_paths(self):
		self.assertEqual(self.backing.backing_image, "/var/lib/atlas/pool/atlas-pool.img")
		self.assertEqual(self.backing.state_file, "/var/lib/atlas/pool/pool-devices")

	def test_register_device_adds_real_disk_to_lvm_devices_file(self):
		# A bare-metal disk must be registered in LVM's system.devices allowlist or
		# pvcreate rejects it ("has no path names") — proven on a live Scaleway box.
		with mock.patch("atlas.lvm.run") as run:
			self.backing.register_device("/dev/sda")
		run.assert_called_once()
		self.assertEqual(_executed(run.call_args), "sudo lvmdevices --adddev /dev/sda")
		self.assertFalse(run.call_args.kwargs.get("check", True))  # tolerant: || true

	def test_register_device_skips_loopback(self):
		# The loopback PV is created locally and already allowed; no lvmdevices.
		with mock.patch("atlas.lvm.run") as run:
			self.backing.register_device("/dev/loop3")
		run.assert_not_called()

	def test_thinpool_exposes_backing(self):
		self.assertIsInstance(ThinPool().backing, PoolBacking)


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
