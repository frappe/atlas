#!/usr/bin/env python3
# Source side of a VM migration (spec/24), LOCAL-BASE-IMAGE ship: export the VM's
# read-only base image LV over NBD so the target can hydrate a copy of it.
#
# WHY THIS EXISTS: a base image that was promoted from a snapshot (`is_local`) has
# no rootfs URL, so `sync-image` cannot place it on the target — it lives only on
# the host it was promoted on (spec/08-images.md). A VM on such an image could not
# migrate: `migration-clone-target` pre-flight fails with "base image LV not on
# target". This ships the local base the same way the disk itself is shipped — an
# NBD export the target flattens into a fresh local LV (spec/24 §5) — so the target
# gains the base LV + kernel it needs at cutover.
#
# The base LV is READ-ONLY and immutable, so — unlike the VM disk — we export it
# DIRECTLY, with no thin snapshot in between.
#
# TWO exports, so the target gains BOTH artifacts a base image needs (the rootfs
# LV *and* the on-disk image directory that holds the kernel), over the single
# plain-TCP NBD channel — no host-to-host SSH (deferred, §2.1) and no new HTTP
# surface:
#   - port nbd_port+2: the base rootfs LV (block device).
#   - port nbd_port+3: a FILE-backed export of a tar of the image directory
#     (kernel + rootfs sentinel). qemu-nbd can serve a plain file, so the small
#     metadata tar rides the same channel with no extra LV to create or clean up.
# (root disk = nbd_port, data disk = nbd_port+1, so +2/+3 never collide with the
# disk export already running for the same migration.)
#
# STAGE 1 transport: plain TCP, qemu-nbd bound to the source's PUBLIC IPv4 — same
# UNENCRYPTED get-it-working-first path as migration-export-source (§2.1).
#
# Idempotent: re-running returns the pids of already-serving qemu-nbd processes and
# re-uses the staged tar.
#
# Inputs:
#   image_name    - base image name; the LV is atlas-image-<image_name>
#   nbd_port      - the migration's base NBD port (controller passes disk port + 2;
#                   the image-dir tar is served on nbd_port + 1, i.e. disk_port + 3)
#   bind_address  - address qemu-nbd binds (the source's public IPv4)
#
# Emits ATLAS_RESULT={"nbd_port": N, "nbd_pid": P, "base_size_bytes": B,
#                     "meta_port": N+1, "meta_pid": P2, "meta_size_bytes": B2}

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool
from atlas.paths import image_directory

RUN_DIRECTORY = "/var/lib/atlas/run"

# The staged image-directory tarball, keyed by image name so concurrent base ships
# of different images never clash. Lives in RUN_DIRECTORY (transient; cleanup removes).
META_TAR = "{run}/migrate-base-meta-{image}.tar"


@dataclass(frozen=True)
class ExportBaseInputs(TaskInputs):
	"""Serve a VM's read-only base image LV + image-directory tar over NBD for a
	migration's target to clone from (local-image path only)."""

	command: typing.ClassVar[str] = "migration-export-base"
	image_name: str
	nbd_port: int
	bind_address: str


@dataclass(frozen=True)
class ExportBaseResult(TaskResult):
	nbd_port: int
	nbd_pid: int
	base_size_bytes: int
	meta_port: int
	meta_pid: int
	meta_size_bytes: int


def main() -> None:
	inputs = ExportBaseInputs.from_args()
	pool = ThinPool()

	base = pool.base_image(inputs.image_name)
	if not base.exists:
		sys.exit(
			f"base image LV not on source: {base.name}; this host is not where "
			f"{inputs.image_name!r} was promoted — nothing to ship"
		)
	image_dir = image_directory(inputs.image_name)
	if not os.path.isdir(image_dir):
		sys.exit(f"image directory {image_dir} missing on source; cannot ship the kernel")
	# Activate so qemu-nbd opens a live block device (a skip-flagged LV would be a
	# missing node). Read-only export: the base is the source of truth and immutable.
	base.activate()

	run("sudo mkdir -p {}", RUN_DIRECTORY)

	# 1. The rootfs LV over NBD (block export).
	nbd_pid = _ensure_nbd_export(base.device_path, inputs.bind_address, inputs.nbd_port)

	# 2. The image directory (kernel + rootfs sentinel) tarred to a file, served
	#    file-backed over NBD on the next port. Stage the tar idempotently.
	meta_port = inputs.nbd_port + 1
	tar_path = META_TAR.format(run=RUN_DIRECTORY, image=inputs.image_name)
	# Stage the tar ONLY when nothing is serving it yet — re-tarring the file under a
	# live qemu-nbd would corrupt in-flight reads. -C so paths inside the tar are
	# relative to the image dir (the target extracts straight into its image_directory).
	if not _already_serving(meta_port):
		run("sudo tar -cf {} -C {} .", tar_path, image_dir)
	meta_size = int(run("sudo stat -c %s {}", tar_path).strip())
	meta_pid = _ensure_nbd_export(tar_path, inputs.bind_address, meta_port)

	ExportBaseResult(
		nbd_port=inputs.nbd_port,
		nbd_pid=nbd_pid,
		base_size_bytes=base.size_bytes,
		meta_port=meta_port,
		meta_pid=meta_pid,
		meta_size_bytes=meta_size,
	).emit()
	print(
		f"Exported base {base.name} on {inputs.bind_address}:{inputs.nbd_port} "
		f"and image-dir tar on :{meta_port}."
	)


def _already_serving(port: int) -> bool:
	"""True if a qemu-nbd is already listening on `port` (idempotency probe)."""
	listening = run("sudo bash -c {}", f"ss -ltn 'sport = :{port}' || true").strip()
	return f":{port}" in listening


def _ensure_nbd_export(device: str, bind_address: str, port: int) -> int:
	"""Serve `device` (a block device OR a plain file) read-only over NBD on
	bind_address:port. Returns the pid. Idempotent: if a qemu-nbd already holds the
	port, return its pid rather than start a second one (EADDRINUSE). Mirrors
	migration-export-source exactly."""
	pidfile = _pidfile(port)
	if _already_serving(port):
		if run_ok("sudo test -f {}", pidfile):
			return int(run("sudo cat {}", pidfile).strip())
		return 0
	# systemd-run detaches from the SSH session (a bare `&` dies on session close —
	# verified on the real hosts); qemu-nbd's --fork returns once the socket is ready.
	run(
		"sudo qemu-nbd --persistent --read-only --cache=none --bind={} --port={} --pid-file={} --fork {}",
		bind_address,
		str(port),
		pidfile,
		device,
	)
	return int(run("sudo cat {}", pidfile).strip())


def _pidfile(port: int) -> str:
	return f"{RUN_DIRECTORY}/migrate-nbd-{port}.pid"


if __name__ == "__main__":
	main()
