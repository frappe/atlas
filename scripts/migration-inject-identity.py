#!/usr/bin/env python3
# Target side of a boot-then-hydrate migration (spec/24 §0), INJECT-IDENTITY phase:
# inject the VM's identity THROUGH the dm-clone device before the guest boots on it.
#
# In boot-then-hydrate the guest boots on the clone read-through and hydrates while
# serving. Identity injection must happen BEFORE boot, and it must write through the
# CLONE device — the plain atlas-vm-<uuid> LV mounts BUSY under a live clone
# (host-verified 2026-07-02, spec/24 §0.4), and writes through the clone land on the
# dest and count toward hydration. Host keys are PRESERVED (the disk moved wholesale;
# its SSH identity must survive the move), same contract as provision-vm's
# preserve_host_keys / rebuild.
#
# Idempotent: inject_identity rewrites the same files; a re-entry is a cheap no-op in
# effect (identical content). Mounts + unmounts the clone (the context manager
# guarantees teardown on any failure).
#
# Inputs:
#   virtual_machine_name  - UUID
#   clone_device          - /dev/mapper/atlas-vm-<uuid>-clone (the read-through)
#   virtual_machine_ipv6  - the VM's address on the target (kept or newly allocated)
#   ipv4_guest_cidr       - guest side of the per-VM NAT44 /30
#   ipv4_gateway          - the guest's v4 gateway (host side of the /30, no mask)
#   ssh_public_key        - injected into the rootfs authorized_keys
#   data_disk_mount_at    - in-guest data mount point (empty = no data fstab line)
#   routing_base_url      - spec/18 in-guest routing client base URL (empty = none)

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run_ok
from atlas._task import TaskInputs
from atlas.rootfs import Identity, inject_identity


@dataclass(frozen=True)
class InjectInputs(TaskInputs):
	"""Inject a migrated VM's identity through its dm-clone before boot."""

	command: typing.ClassVar[str] = "migration-inject-identity"
	virtual_machine_name: str
	clone_device: str
	virtual_machine_ipv6: str
	ipv4_guest_cidr: str
	ipv4_gateway: str
	ssh_public_key: str
	data_disk_mount_at: str = ""
	routing_base_url: str = ""


def main() -> None:
	inputs = InjectInputs.from_args()

	# Inject through the clone when it exists (a boot-then-hydrate migration is live —
	# the plain LV is busy under it and cannot be mounted). If the clone is already
	# gone (the disk converged to the plain LV, e.g. a collapse-forward retry after a
	# stop), fall back to the plain LV, which is now directly mountable. Both carry the
	# same identity write; only the mountable view differs.
	device = inputs.clone_device
	if not run_ok("sudo dmsetup info {}", os.path.basename(inputs.clone_device)):
		device = f"/dev/atlas/atlas-vm-{inputs.virtual_machine_name}"
		if not run_ok("sudo test -b {}", device):
			sys.exit(
				f"neither clone {inputs.clone_device} nor plain LV {device} present; "
				"run migration-clone-target first (spec/24 §0)"
			)

	inject_identity(
		device,
		Identity(
			uuid=inputs.virtual_machine_name,
			ipv6_address=inputs.virtual_machine_ipv6,
			ssh_public_key=inputs.ssh_public_key,
			ipv4_guest_cidr=inputs.ipv4_guest_cidr,
			ipv4_gateway=inputs.ipv4_gateway,
			data_disk_mount_at=inputs.data_disk_mount_at,
			routing_base_url=inputs.routing_base_url,
		),
		# The disk moved wholesale from the source; its SSH host keys ARE the VM's
		# identity and must not change across hosts (clients' known_hosts).
		regenerate_host_keys=False,
	)

	print(f"Injected identity for {inputs.virtual_machine_name} via {device}.")


if __name__ == "__main__":
	main()
