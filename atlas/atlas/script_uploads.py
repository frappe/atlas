"""Per-script sidecar uploads.

Some scripts need supporting files on the server before they run. The Server
bootstrap is special: its uploads are durable state (helper scripts + systemd
unit) placed by `Server.bootstrap()` directly, not through this map.

The map below is consulted by `ssh.py::_run_remote_script()` before each
script invocation. Paths in the value tuples are (local_relative_to_repo_root,
remote_absolute).
"""

# prepare-rootfs.sh is a sourced shell library (not a standalone Task). The
# scripts that lay down a per-VM rootfs source it by relative path, so it must
# land in the staging directory next to them.
_PREPARE_ROOTFS = ("scripts/lib/prepare-rootfs.sh", "/tmp/atlas/prepare-rootfs.sh")

SCRIPT_UPLOADS: dict[str, list[tuple[str, str]]] = {
	"sync-image.sh": [
		("scripts/guest/atlas-network.service", "/tmp/atlas/atlas-network.service"),
	],
	"provision-vm.sh": [_PREPARE_ROOTFS],
	"rebuild-vm.sh": [_PREPARE_ROOTFS],
}


def files_to_upload(script: str) -> list[tuple[str, str]]:
	return SCRIPT_UPLOADS.get(script, [])
