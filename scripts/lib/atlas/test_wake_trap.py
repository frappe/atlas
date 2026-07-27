"""Unit tests for the atlas-wake-trap daemon (spec/32-sleepy-vms.md).

The daemon is the wake half of the parked SYN trap: it polls the `wake_<hex>`
named counters and, on the first packet for a still-sleeping VM, does the local
wake. These cover the three things it can get wrong without anyone noticing —
misparsing `nft -j` output, waking a VM that is already awake, and failing to
re-park after a reboot.

Run with bare `python3 -m unittest atlas.test_wake_trap` from scripts/lib, like
test_park.py: no Frappe, no site, no host, no nft. It has to live here rather
than under atlas/tests/ because the Frappe app is also called `atlas` and would
shadow the scripts package this daemon imports.

The daemon is a top-level dashed file, not an importable module, so it is loaded
by path — its own sys.path shim assumes the durable /var/lib/atlas/bin layout
where the package sits beside it, which is not the repo layout.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[2]

UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
HEX = "3f2504e04f8941d39a0c0305e82c3301"


def _load_daemon():
	if str(_SCRIPTS_DIR / "lib") not in sys.path:
		sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
	spec = importlib.util.spec_from_file_location(
		"atlas_wake_trap", _SCRIPTS_DIR / "atlas-wake-trap.py"
	)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestWakeCounters(unittest.TestCase):
	"""_wake_counters turns `nft -j list counters` into {uuid: packets}."""

	def setUp(self) -> None:
		self.daemon = _load_daemon()

	def _with_nft_output(self, output: str) -> dict:
		with patch.object(self.daemon, "run", return_value=output):
			return self.daemon._wake_counters()

	def test_maps_wake_counters_to_uuids(self) -> None:
		output = json.dumps(
			{"nftables": [{"counter": {"name": f"wake_{HEX}", "packets": 3, "bytes": 180}}]}
		)
		self.assertEqual(self._with_nft_output(output), {UUID: 3})

	def test_ignores_counters_that_are_not_ours(self) -> None:
		# Another feature's named counter in the same table must not yield a uuid.
		output = json.dumps(
			{
				"nftables": [
					{"counter": {"name": "bytes_total", "packets": 9}},
					{"counter": {"name": f"wake_{HEX}", "packets": 1}},
				]
			}
		)
		self.assertEqual(self._with_nft_output(output), {UUID: 1})

	def test_missing_table_returns_empty(self) -> None:
		# `nft` writes the error to stderr and run(check=False) hands back "".
		self.assertEqual(self._with_nft_output(""), {})

	def test_malformed_json_returns_empty(self) -> None:
		# A partial read must not kill the poll loop.
		self.assertEqual(self._with_nft_output("{not json"), {})

	def test_non_counter_entries_are_skipped(self) -> None:
		output = json.dumps({"nftables": [{"metainfo": {"version": "1.0"}}, "junk"]})
		self.assertEqual(self._with_nft_output(output), {})


class TestTick(unittest.TestCase):
	"""_tick wakes only a VM that is still asleep and has a non-zero counter."""

	def setUp(self) -> None:
		self.daemon = _load_daemon()

	def _tick(self, counters: dict, *, marker_exists: bool) -> list:
		woken = []
		with (
			patch.object(self.daemon, "_wake_counters", return_value=counters),
			patch.object(self.daemon.os.path, "exists", return_value=marker_exists),
			patch.object(self.daemon, "_wake", side_effect=lambda paths: woken.append(paths.uuid)),
		):
			self.daemon._tick()
		return woken

	def test_wakes_on_the_first_packet(self) -> None:
		self.assertEqual(self._tick({UUID: 1}, marker_exists=True), [UUID])

	def test_zero_packets_does_not_wake(self) -> None:
		self.assertEqual(self._tick({UUID: 0}, marker_exists=True), [])

	def test_stale_counter_on_an_already_woken_vm_does_not_wake(self) -> None:
		# vm-network-up removes the counter as it rebuilds the real path, so a
		# lingering count with no marker is stale. The marker is the authority.
		self.assertEqual(self._tick({UUID: 5}, marker_exists=False), [])

	def test_one_failed_wake_does_not_skip_the_others(self) -> None:
		other = "11111111-2222-3333-4444-555555555555"
		woken = []

		def _wake(paths):
			if paths.uuid == UUID:
				raise RuntimeError("systemctl failed")
			woken.append(paths.uuid)

		with (
			patch.object(self.daemon, "_wake_counters", return_value={UUID: 1, other: 1}),
			patch.object(self.daemon.os.path, "exists", return_value=True),
			patch.object(self.daemon, "_wake", side_effect=_wake),
		):
			self.daemon._tick()  # must not raise
		self.assertEqual(woken, [other])


class TestWakeOrder(unittest.TestCase):
	"""_wake removes the marker BEFORE starting the unit."""

	def setUp(self) -> None:
		self.daemon = _load_daemon()

	def test_marker_is_removed_before_the_unit_starts(self) -> None:
		# A reboot between the two steps must not leave a marker suppressing the
		# unit — the VM would stay dark with no trap to wake it.
		commands = []
		paths = self.daemon.VirtualMachinePaths(UUID)
		with patch.object(self.daemon, "run", side_effect=lambda t, *a, **k: commands.append(t.format(*a))):
			self.daemon._wake(paths)
		self.assertEqual(len(commands), 2)
		self.assertIn("rm -f", commands[0])
		self.assertIn("systemctl start", commands[1])


class TestResweep(unittest.TestCase):
	"""The boot re-sweep rebuilds park state from the on-disk markers, DB-free."""

	def setUp(self) -> None:
		self.daemon = _load_daemon()

	def test_re_parks_every_marked_vm(self) -> None:
		parked = []
		with (
			patch.object(self.daemon, "ensure_park_device"),
			patch.object(self.daemon, "_sleeping_uuids", return_value=[UUID]),
			patch.object(self.daemon, "park", side_effect=parked.append),
		):
			self.daemon._resweep_parked_vms()
		self.assertEqual(parked, [UUID])

	def test_one_failed_re_park_does_not_stop_the_sweep(self) -> None:
		other = "11111111-2222-3333-4444-555555555555"
		parked = []

		def _park(uuid):
			if uuid == UUID:
				raise RuntimeError("nft busy")
			parked.append(uuid)

		with (
			patch.object(self.daemon, "ensure_park_device"),
			patch.object(self.daemon, "_sleeping_uuids", return_value=[UUID, other]),
			patch.object(self.daemon, "park", side_effect=_park),
		):
			self.daemon._resweep_parked_vms()  # must not raise
		self.assertEqual(parked, [other])

	def test_unreadable_directory_yields_no_uuids(self) -> None:
		with patch.object(self.daemon.os, "listdir", side_effect=OSError):
			self.assertEqual(self.daemon._sleeping_uuids(), [])

	def test_only_vms_carrying_a_marker_are_returned(self) -> None:
		other = "11111111-2222-3333-4444-555555555555"
		with (
			patch.object(self.daemon.os, "listdir", return_value=[UUID, other]),
			patch.object(
				self.daemon.os.path, "exists", side_effect=lambda path: UUID in str(path)
			),
		):
			self.assertEqual(self.daemon._sleeping_uuids(), [UUID])


if __name__ == "__main__":
	unittest.main()
