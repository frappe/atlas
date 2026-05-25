"""Catalog of scripts that can be invoked as Tasks on a Server.

A whitelist over `scripts/*.sh`. Excludes `scripts/guest/` (guest unit files,
not runnable on the host) and `scripts/systemd/` (systemd units, not scripts).
"""

from pathlib import Path

from atlas.atlas.ssh import SCRIPTS_DIRECTORY


def allowed_scripts() -> list[str]:
	"""Return the sorted list of `.sh` filenames runnable on a server host."""
	if not SCRIPTS_DIRECTORY.is_dir():
		return []
	return sorted(
		entry.name
		for entry in SCRIPTS_DIRECTORY.iterdir()
		if entry.is_file() and entry.suffix == ".sh"
	)


def script_path(script: str) -> Path:
	"""Resolve a script name to its absolute path, asserting it is allowed."""
	if script not in allowed_scripts():
		raise ValueError(f"Unknown script: {script}")
	return SCRIPTS_DIRECTORY / script
