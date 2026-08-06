#!/usr/bin/env python3
# Promote a baked snapshot LV into a same-server base image, so new VMs can
# provision from it via the ordinary `image` field instead of cloning a one-off
# snapshot. Same-server scope: the bytes never leave the host — snapshot LV ->
# base-image LV is a local `dd` (no bucket, no SSH copy). Idempotent: re-running
# with an existing target is a no-op. Pure host op — no jail interaction.
#
# Two things make a synced image provisionable, and this script materializes both
# for a promoted image so it looks exactly like a from-URL image to provision-vm.py:
#
#   1. The rootfs base LV `atlas-image-<IMAGE_NAME>`, dd'd from the snapshot LV
#      (the actual disk bytes a per-VM disk CoW-snapshots from).
#   2. The on-disk image directory /var/lib/atlas/images/<IMAGE_NAME>/ holding:
#      - the KERNEL, hard-linked from the SOURCE image's already-present vmlinux
#        (free: same filesystem, byte-for-byte — provision-vm.py hard-links it on
#        again into each jail). The promoted image reuses its source's kernel; no
#        kernel export, nothing downloaded.
#      - a rootfs PRESENCE SENTINEL file named <ROOTFS_FILENAME>. provision-vm.py
#        only `os.path.isfile()`-probes the rootfs file (step 0); the real bytes
#        come from the base LV via _resolve_origin. So for a local image the
#        "rootfs file" is a sentinel and the LV is the truth.
#
# Warm vs cold is identical here — a disk-LV dd either way; the warm-reject rule
# lives in the controller (VirtualMachineSnapshot.promote_to_image), so this
# script never sees a warm snapshot's memory pair. Invoked as a CLI:
#   promote-snapshot-image.py --snapshot-rootfs-path /dev/atlas/atlas-snap-<uuid> \
#       --image-name golden-bench-v1 --disk-gigabytes 28 \
#       --rootfs-filename atlas-image-golden-bench-v1 \
#       --source-image ubuntu-24.04 --kernel-filename vmlinux-6.1.141

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, install_file, run
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool
from atlas.paths import image_directory


@dataclass(frozen=True)
class PromoteSnapshotInputs(TaskInputs):
	"""Promote a snapshot LV into a same-server base image (rootfs LV + image dir)."""

	command: typing.ClassVar[str] = "promote-snapshot-image"
	snapshot_rootfs_path: str  # the snapshot's /dev/atlas/<name> device path (the dd source)
	image_name: str  # the base image name; the LV becomes atlas-image-<image_name>
	disk_gigabytes: int  # size of the promoted base image LV (the snapshot's disk size)
	rootfs_filename: str  # the new image's rootfs filename (presence sentinel in the image dir)
	source_image: str  # the snapshot's source image name (where its kernel already lives)
	kernel_filename: str  # the kernel filename to reuse (present in the source image's dir)


@dataclass(frozen=True)
class PromoteSnapshotResult(TaskResult):
	image_lv: str  # the created (or existing) atlas-image-<name> LV name
	size_bytes: int  # the promoted base image LV's byte size


def main() -> None:
	inputs = PromoteSnapshotInputs.from_args()
	pool = ThinPool()

	# 1. The rootfs base LV: dd the snapshot LV into a read-only atlas-image-<name>
	#    LV. A standalone thin volume (own bytes, no origin) so it outlives the
	#    snapshot it was dd'd from. Idempotent — a no-op if the target LV exists.
	source = pool.from_device(inputs.snapshot_rootfs_path)
	image_lv = pool.import_base_image_from_lv(source, inputs.image_name, inputs.disk_gigabytes)

	# 2. The on-disk image directory, so provision-vm.py finds a kernel + a rootfs
	#    presence file exactly as it would for a synced image.
	image_dir = image_directory(inputs.image_name)
	install_directory(image_dir, mode="0700")

	# 2a. Hard-link the SOURCE image's kernel into this image's directory. Same
	#     filesystem (/var/lib/atlas), so `ln` always works; the byte-identical
	#     vmlinux is shared, not copied. `ln -f` is idempotent on a re-run.
	source_kernel = f"{image_directory(inputs.source_image)}/{inputs.kernel_filename}"
	dest_kernel = f"{image_dir}/{inputs.kernel_filename}"
	if not os.path.isfile(source_kernel):
		sys.exit(
			f"source image kernel not found: {source_kernel}; the snapshot's source "
			f"image '{inputs.source_image}' must be synced to this server first"
		)
	run("sudo ln -f {} {}", source_kernel, dest_kernel)

	# 2b. The rootfs presence sentinel. provision-vm.py only stat-probes this file;
	#     the disk bytes are the base LV (1). An empty file documents "the rootfs
	#     for this image is the LV of the same name", and satisfies the probe.
	install_file(
		f"# Local image promoted from snapshot {source.name}. Rootfs is the LVM thin "
		f"volume {image_lv.name}, not this file (provision reads the LV).\n",
		f"{image_dir}/{inputs.rootfs_filename}",
		mode="0644",
	)

	PromoteSnapshotResult(image_lv=image_lv.name, size_bytes=image_lv.size_bytes).emit()
	print(f"Promoted {source.name} to base image {image_lv.name} ({image_dir}).")


if __name__ == "__main__":
	main()
