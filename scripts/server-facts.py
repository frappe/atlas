#!/usr/bin/env python3
# Report the host's capacity facts so the controller can (re-)stamp them without a
# full re-bootstrap — the Refresh Capacity button. Read-only and idempotent: it
# measures the live host (CPU threads, RAM, the LVM thin pool's size and fullness)
# and emits one ServerFactsResult line. The same three totals bootstrap stamps
# (atlas.hostfacts), plus the live pool_data_percent bootstrap leaves to this Task
# (it is ~0 on a freshly-created pool and drifts as VMs write).

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs, TaskResult
from atlas.hostfacts import host_capacity_facts


@dataclass(frozen=True)
class ServerFactsInputs(TaskInputs):
	"""Measure and report the host's capacity facts. Takes no arguments — the host
	measures itself."""

	command: typing.ClassVar[str] = "server-facts"


@dataclass(frozen=True)
class ServerFactsResult(TaskResult):
	vcpus_total: int
	memory_megabytes_total: int
	pool_disk_gigabytes_total: int
	pool_data_percent: float


def main() -> None:
	ServerFactsInputs.from_args()
	ServerFactsResult(**host_capacity_facts()).emit()


if __name__ == "__main__":
	main()
