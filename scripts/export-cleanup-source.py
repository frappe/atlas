#!/usr/bin/env python3
# Source side of a base-image EXPORT (spec/08-images.md § two origins; the standalone
# form of the migration base ship, spec/24 §5.1): after the target has hydrated and
# collapsed the base into its own local LV, tear down the source's NBD exports.
#
# Unlike migration-cleanup-source this is base-ONLY — there is no VM, no snapshot, no
# unit/disk teardown. The base LV is the source's own immutable image and is NEVER
# removed; we only stop the two qemu-nbd processes the export started and drop the
# staged image-directory tar:
#   - nbd_port     - the base rootfs LV export (block).
#   - nbd_port + 1 - the image-dir tar export (file-backed).
#
# Idempotent + best-effort: a re-entry after a partial cleanup just finishes the rest,
# and killing an already-dead export is a harmless no-op (the pidfile is gone).
#
# Inputs:
#   image_name  - base image name (keys the staged tar filename).
#   nbd_port    - the export's base NBD port (tar on nbd_port + 1).

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs

RUN_DIRECTORY = "/var/lib/atlas/run"

# Same key migration-export-base staged the tar under, so cleanup removes exactly the
# file this image's export created (not another concurrent export's).
META_TAR = "{run}/migrate-base-meta-{image}.tar"


@dataclass(frozen=True)
class ExportCleanupInputs(TaskInputs):
	"""Stop a base-image export's NBD servers and drop its staged tar."""

	command: typing.ClassVar[str] = "export-cleanup-source"
	image_name: str
	nbd_port: int = 0


def main() -> None:
	inputs = ExportCleanupInputs.from_args()

	# Kill both exports by pidfile-per-port (the pid the fork wrote), same mechanism as
	# migration-cleanup-source's _kill_nbd. nbd_port 0 means the export never started —
	# nothing to stop.
	if inputs.nbd_port:
		_kill_nbd(inputs.nbd_port)
		_kill_nbd(inputs.nbd_port + 1)  # the image-dir tar export
	tar_path = META_TAR.format(run=RUN_DIRECTORY, image=inputs.image_name)
	run("sudo rm -f {}", tar_path, check=False)

	print(f"Cleaned up base-image export of {inputs.image_name} (NBD ports {inputs.nbd_port}, +1).")


def _kill_nbd(port: int) -> None:
	"""Stop the qemu-nbd serving `port` via its pidfile. Best-effort: a missing pidfile
	(already stopped) is a no-op. Mirrors migration-cleanup-source._kill_nbd's
	pidfile path (migrate-nbd-<port>.pid), which migration-export-base wrote."""
	pidfile = f"{RUN_DIRECTORY}/migrate-nbd-{port}.pid"
	if run_ok("sudo test -f {}", pidfile):
		filepid = run("sudo cat {}", pidfile, check=False).strip()
		if filepid:
			run("sudo kill {}", filepid, check=False)
		run("sudo rm -f {}", pidfile, check=False)


if __name__ == "__main__":
	main()
