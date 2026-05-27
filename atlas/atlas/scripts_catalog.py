"""Catalog of scripts that can be invoked as Tasks on a Server.

`allowed_scripts()` lists every script the SSH runner will execute on a host.
`operator_visible_scripts()` is the subset that the desk's Run Task dialog
exposes — anything that should only run from a VM/Image controller is
deliberately excluded.

`resolve()` is the file-system lookup used by the SSH runner; it searches both
the production scripts directory and the e2e test-only directory, because e2e
probe scripts (which never appear in the picker) need to be findable too.
"""

import functools
from pathlib import Path

import frappe

OPERATOR_VISIBLE: frozenset[str] = frozenset({
	"bootstrap-server.sh",
	"reboot-server.sh",
	"sync-image.sh",
})


@functools.lru_cache(maxsize=1)
def _repo_root() -> Path:
	# Cached per-process. Tests that monkeypatch frappe.get_app_path must call
	# _repo_root.cache_clear().
	return Path(frappe.get_app_path("atlas", "..")).resolve()


def scripts_directory() -> Path:
	return _repo_root() / "scripts"


def e2e_scripts_directory() -> Path:
	return _repo_root() / "atlas" / "tests" / "e2e" / "scripts"


def _search_paths() -> list[Path]:
	return [scripts_directory(), e2e_scripts_directory()]


def allowed_scripts() -> list[str]:
	"""Return the sorted list of `.sh` filenames runnable on a server host."""
	directory = scripts_directory()
	if not directory.is_dir():
		return []
	return sorted(
		entry.name
		for entry in directory.iterdir()
		if entry.is_file() and entry.suffix == ".sh"
	)


def operator_visible_scripts() -> list[str]:
	"""Subset of allowed_scripts() that the Run Task dialog should expose."""
	return sorted(name for name in allowed_scripts() if name in OPERATOR_VISIBLE)


def resolve(script: str) -> Path:
	"""Locate a script in either the production or e2e directory. Raises
	FileNotFoundError if not present in either."""
	for directory in _search_paths():
		candidate = directory / script
		if candidate.is_file():
			return candidate
	raise FileNotFoundError(
		f"Script not found in {[str(p) for p in _search_paths()]}: {script}"
	)
