"""Derived host layout for a migration — the dm-clone device name, the per-VM
NBD ports/slots, and disk-size rounding. Every value here is a PURE function of
the VM UUID (or a byte count): the controller and every host script agree from
the UUID with no allocator and no stored state.

Extracted from migration.py so the saga file carries the state machine, not the
addressing arithmetic. migration.py re-imports the names it uses, so
`migration.clone_device_path(...)` etc. still resolve.
"""

from __future__ import annotations

import uuid


def clone_device_path(virtual_machine: str) -> str:
	"""The dm-clone read-through device for a migrated VM's root disk (spec/24 §0).
	Boot-then-hydrate boots the guest on this device; CollapseClone reloads its table
	to a linear map onto the plain LV. Named identically on the host
	(migration-clone-target's CLONE_DEV), a pure function of the UUID."""
	return f"/dev/mapper/atlas-vm-{virtual_machine}-clone"


def _bytes_to_gib_ceil(size_bytes: int) -> int:
	"""Round a byte size UP to whole GiB — the target base LV must be at least the
	source's size (a smaller thin LV would truncate the copy)."""
	gib = 1024**3
	return (size_bytes + gib - 1) // gib


def nbd_port(virtual_machine: str) -> int:
	"""A stable per-VM TCP port so concurrent migrations on one source host never
	collide. Derived like the other UUID-keyed values (tap/mac/uid)."""
	return 10000 + (int(uuid.UUID(virtual_machine).hex[:4], 16) % 5000)


# Each migration's TARGET side needs a contiguous block of nbd CLIENT devices:
# root disk, data disk, base-image ship, base-image-dir tar — 4 slots. Hosts ship
# 16 nbd devices (nbds_max=16), so a per-VM base slot of (uuid % 4) * 4 fans four
# concurrent migrations across /dev/nbd0-15 with no overlap. WITHOUT this the disk
# clone hardcoded /dev/nbd0 & /dev/nbd1, so a second migration to the same target
# latched onto the first's live nbd0 (wrong size → dm-clone "Invalid argument") —
# found on a real double-migration to f2 (2026-07-02). Derived (not allocated) so
# the controller and every host script agree from the UUID with no stored state.
NBD_SLOTS_PER_MIGRATION = 4
MAX_CONCURRENT_TARGET_MIGRATIONS = 4  # 4 * 4 = 16 = nbds_max


def nbd_base_slot(virtual_machine: str) -> int:
	"""The first of this VM's 4 contiguous nbd client slots on the TARGET host:
	base+0 root, base+1 data, base+2 base-image, base+3 image-dir tar. A pure
	function of the UUID (like nbd_port), so clone/cutover/base-ship all name the
	same devices with no allocator."""
	index = int(uuid.UUID(virtual_machine).hex[4:8], 16) % MAX_CONCURRENT_TARGET_MIGRATIONS
	return index * NBD_SLOTS_PER_MIGRATION
