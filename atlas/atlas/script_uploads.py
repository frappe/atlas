"""Per-script sidecar uploads.

Every Python task imports the shared `atlas` package (lvm, paths, rootfs, _run,
_task, …). The entry point adds `<staging>/lib` to sys.path and does
`import atlas`, so the package must land at `<staging>/lib/atlas/*.py` next to
the staged script. `package_uploads()` computes that file list from disk, so a
new module in `scripts/lib/atlas/` is staged automatically — no map to update.

A few scripts need extra sidecars (sync-image needs the guest network unit it
bakes into the image); those stay in SCRIPT_SIDECARS.

The Server bootstrap is special: its uploads are DURABLE state (the package
under /var/lib/atlas/bin + systemd units) placed by `Server.bootstrap()`
directly, not through this module — see server.py.

Consumed by `_ssh/runner.py::_run_remote_script()` before each invocation.
Tuples are (local_relative_to_repo_root, remote_absolute).
"""

from pathlib import Path

from atlas.atlas import scripts_catalog

# Where the package lands remotely so `import atlas` resolves: the entry point's
# sys.path shim adds `<staging>/lib`, so the package is `<staging>/lib/atlas/`.
_REMOTE_PACKAGE_DIR = "/tmp/atlas/lib/atlas"

# Extra per-script sidecars beyond the shared package. sync-image bakes the guest
# atlas-network.service into the ext4 it builds, so it needs that file staged.
SCRIPT_SIDECARS: dict[str, list[tuple[str, str]]] = {
	"sync-image.py": [
		("scripts/guest/atlas-network.service", "/tmp/atlas/atlas-network.service"),
	],
}


def _package_files() -> list[tuple[str, str]]:
	"""Every .py under scripts/lib/atlas/, mapped to its remote staging path.
	Computed from disk so a new lib module is picked up with no edit here. The
	test-only test_*.py files are skipped — they never run on a host."""
	local_dir = scripts_catalog.scripts_directory() / "lib" / "atlas"
	uploads: list[tuple[str, str]] = []
	for entry in sorted(local_dir.glob("*.py")):
		if entry.name.startswith("test_"):
			continue
		local = str(Path("scripts") / "lib" / "atlas" / entry.name)
		uploads.append((local, f"{_REMOTE_PACKAGE_DIR}/{entry.name}"))
	return uploads


def files_to_upload(script: str) -> list[tuple[str, str]]:
	"""Sidecar files to stage before `script` runs. Python tasks get the full
	`atlas` package plus any per-script sidecars; the remaining shell tasks
	(reboot-server.sh) get nothing."""
	if not script.endswith(".py"):
		return SCRIPT_SIDECARS.get(script, [])
	return _package_files() + SCRIPT_SIDECARS.get(script, [])
