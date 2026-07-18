"""Unit tests for the pure half of the host-facts gatherer.

Run with bare `python3 -m unittest atlas.test_hostfacts` from scripts/lib: no
Frappe, no site, no host. `parse_memory_megabytes` is the one line that, on a real
host, would only be exercised over SSH — the kB→MB conversion and the MemTotal
line-pick that capacity accounting depends on.
"""

import unittest

from atlas.hostfacts import parse_memory_megabytes

MEMINFO = """\
MemTotal:       16307192 kB
MemFree:         9876543 kB
MemAvailable:   14000000 kB
Buffers:          123456 kB
"""


class TestParseMemoryMegabytes(unittest.TestCase):
	def test_memtotal_kilobytes_to_megabytes(self) -> None:
		# 16307192 kB // 1024 = 15924 MB (integer division; the sub-MB remainder is
		# host overhead the memory reserve covers).
		self.assertEqual(parse_memory_megabytes(MEMINFO), 15924)

	def test_picks_memtotal_not_a_later_line(self) -> None:
		# MemFree/MemAvailable are also kB lines; only MemTotal is the physical total.
		self.assertNotEqual(parse_memory_megabytes(MEMINFO), 9876543 // 1024)

	def test_missing_memtotal_raises(self) -> None:
		with self.assertRaises(ValueError):
			parse_memory_megabytes("MemFree: 100 kB\n")


if __name__ == "__main__":
	unittest.main()
