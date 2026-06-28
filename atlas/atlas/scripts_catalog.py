"""Catalog of scripts that can be invoked as Tasks on a Server — the single
authority on a *verb* (a `Task.script` value like `provision-vm`).

A Task references a **verb**, not a filename: `Task.script = "provision-vm"`,
executed on the host as `atlas provision-vm --flags`. The on-disk file keeps its
extension (`provision-vm.py`, `reboot-server.sh`); only the Task identifier drops
it. This module is the seam that maps between the two:

  - `allowed_scripts()` lists every verb the SSH runner will execute on a host.
  - `operator_visible_scripts()` is the subset the desk's Run Task dialog exposes;
    anything that should only run from a VM/Image controller is excluded.
  - `file_for(verb)` → the basename on disk (`provision-vm.py`).
  - `kind(verb)` → `"python"` | `"shell"`, derived from that file's extension —
    the runner asks this instead of sniffing a `.py`/`.sh` suffix off `Task.script`.
  - `resolve(verb)` is the file-system lookup the runner uses; it searches both the
    production scripts directory and the e2e test-only directory, because e2e probe
    scripts (which never appear in the picker) need to be findable by verb too.
"""

import functools
from pathlib import Path

import frappe

OPERATOR_VISIBLE: frozenset[str] = frozenset(
	{
		# Bootstrap and Reboot have dedicated buttons with confirmation guards on
		# the Server form; exposing the raw verbs in the Run Task picker
		# duplicates those flows without the guards. `sync-image` is the only
		# ad-hoc verb the operator should reach for from here.
		"sync-image",
	}
)


# Per-verb Run Task dialog metadata. The client renders the dialog purely
# from this — verb names, intros, and field schemas all live here. Each
# entry is `{intro: str, fields: list[dict]}`; field dicts use Frappe Dialog
# field shapes (`fieldname`, `fieldtype`, `label`, `default`, `reqd`, ...).
SCRIPT_FORMS: dict[str, dict] = {
	"bootstrap-server": {
		"intro": "Idempotent. Safe to re-run on an Active server.",
		"fields": [
			{
				"fieldname": "FIRECRACKER_VERSION",
				"label": "Firecracker Version",
				"fieldtype": "Data",
				"default": "v1.16.0",
				"reqd": 1,
			},
			{
				"fieldname": "ARCHITECTURE",
				"label": "Architecture",
				"fieldtype": "Select",
				"options": "x86_64\naarch64",
				"default": "x86_64",
				"reqd": 1,
			},
		],
	},
	# reboot-server stays a shell verb (reboot-server.sh; two lines, not worth porting).
	"reboot-server": {
		"intro": "Reboots the host. SSH drops mid-Task; the Task may end Failure — that is normal.",
		"fields": [],
	},
	"sync-image": {
		"intro": "Downloads kernel + rootfs from the image URLs onto the server.",
		"fields": [
			{
				"fieldname": "IMAGE_NAME",
				"label": "Image",
				"fieldtype": "Link",
				"options": "Virtual Machine Image",
				"reqd": 1,
				"only_select": 1,
				"filters": {"is_active": 1},
			},
		],
	},
}


def script_form(script: str) -> dict:
	"""Return the Run Task dialog metadata for `script` (a verb), or an empty form
	(no intro, no fields) for verbs that don't need any variables."""
	return SCRIPT_FORMS.get(script, {"intro": "", "fields": []})


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


# A Task file is a `.py` (typed CLI verb) or `.sh` (the few shell verbs) directly
# in scripts/. The catalog keys everything on the verb (the stem); this is the
# file filter that decides what's a Task at all.
_TASK_SUFFIXES: frozenset[str] = frozenset({".py", ".sh"})


# Systemd-invoked hooks live in scripts/ but are NOT Task-runnable: they take a
# positional VM uuid (passed by the unit's ExecStartPre/ExecStopPost as `%i`),
# not the --flag CLI contract a Task uses, and they import the durable package.
# Excluded from the catalog (by verb) so the runner never executes them as a Task.
SYSTEMD_HOOKS: frozenset[str] = frozenset(
	{
		"vm-disk-up",
		"vm-network-up",
		"vm-network-down",
		"vm-restore",
	}
)

# Controller-only Tasks: they run on the Atlas controller via the local runner
# (atlas.atlas.local_task), NOT over SSH onto a Server host. `resolve()` must
# still find them, but they are not host SSH tasks, so they are excluded from
# `allowed_scripts()` (the host run-task gate) and the operator picker.
CONTROLLER_ONLY: frozenset[str] = frozenset(
	{
		"issue-cert",
		# Central-managed tunnel + management-plane firewall (spec/21-tunnel.md). These
		# run on the Atlas host via run_local_task, driven by the central_link API
		# during registration — never host SSH Tasks, never in the operator picker.
		"tunnel-up",
		"tunnel-down",
		"mgmt-firewall-apply",
		"mgmt-firewall-revert",
		"mgmt-firewall-confirm",
	}
)


def _verbs_in(directory: Path) -> dict[str, str]:
	"""Map verb (stem) → basename for every Task file in `directory`.

	Raises if two files share a stem (a `.py` and `.sh` of the same name) — that
	would make `file_for`/`kind` ambiguous; it must not happen."""
	verbs: dict[str, str] = {}
	if not directory.is_dir():
		return verbs
	for entry in sorted(directory.iterdir()):
		if not (entry.is_file() and entry.suffix in _TASK_SUFFIXES):
			continue
		verb = entry.stem
		if verb in verbs:
			raise AssertionError(
				f"ambiguous verb {verb!r}: both {verbs[verb]} and {entry.name} in {directory}"
			)
		verbs[verb] = entry.name
	return verbs


def allowed_scripts() -> list[str]:
	"""Return the sorted list of task-runnable *verbs* on a server host.

	Both Python verbs (the typed CLI tasks) and shell verbs (the few remaining,
	e.g. reboot-server) are runnable. The systemd hooks and controller-only tasks
	are excluded — they are not host SSH Tasks (see SYSTEMD_HOOKS /
	CONTROLLER_ONLY)."""
	excluded = SYSTEMD_HOOKS | CONTROLLER_ONLY
	return sorted(verb for verb in _verbs_in(scripts_directory()) if verb not in excluded)


def operator_visible_scripts() -> list[str]:
	"""Subset of allowed_scripts() (verbs) that the Run Task dialog should expose."""
	return sorted(verb for verb in allowed_scripts() if verb in OPERATOR_VISIBLE)


def file_for(verb: str) -> str:
	"""The on-disk basename for a verb (`provision-vm` → `provision-vm.py`,
	`reboot-server` → `reboot-server.sh`). Searches the production then the e2e
	directory (e2e probes are addressed by verb too). Raises FileNotFoundError if
	no file has that stem."""
	for directory in _search_paths():
		name = _verbs_in(directory).get(verb)
		if name is not None:
			return name
	raise FileNotFoundError(f"No script file for verb {verb!r} in {[str(p) for p in _search_paths()]}")


def kind(verb: str) -> str:
	"""`"shell"` iff the verb's file is a `.sh` (only reboot-server today), else
	`"python"`. This replaces every `.endswith(".py")` suffix-sniff downstream."""
	return "shell" if file_for(verb).endswith(".sh") else "python"


# Production Task scripts are shipped durably to the host's /var/lib/atlas/bin by
# Server.bootstrap()/sync_scripts() — the same place the importable atlas package
# and the systemd hooks already live. Python verbs are then invoked as
# `atlas <verb>` (the pip-installed console script); shell verbs run in place by
# path. Either way no per-Task scp (the dominant latency of a start/stop/snapshot
# Task). The dir equals runner.DURABLE_PACKAGE_DIRECTORY; the literal is repeated
# here so server.py and the runner agree on one location without importing each
# other.
DURABLE_SCRIPT_DIRECTORY = "/var/lib/atlas/bin"


def host_task_scripts() -> list[str]:
	"""Production Task verbs shipped durably to /var/lib/atlas/bin — exactly
	allowed_scripts(), every host SSH Task entry point. Bootstrap / sync_scripts
	upload the FILES (verb→file_for) so the runner invokes them in place. e2e probe
	scripts live in the test-only directory, are not shipped durably, and keep the
	staging path."""
	return allowed_scripts()


def durable_remote_path(verb: str) -> str | None:
	"""The /var/lib/atlas/bin path of the FILE for a durably-shipped Task verb
	(the file keeps its suffix on the host disk), or None when the verb isn't
	shipped durably (an e2e probe resolved from the test directory) — which the
	runner stages per Task instead."""
	if verb in host_task_scripts():
		return f"{DURABLE_SCRIPT_DIRECTORY}/{file_for(verb)}"
	return None


def resolve(verb: str) -> Path:
	"""Locate a verb's file in either the production or e2e directory. Raises
	FileNotFoundError if not present in either."""
	for directory in _search_paths():
		name = _verbs_in(directory).get(verb)
		if name is not None:
			return directory / name
	raise FileNotFoundError(f"Script not found in {[str(p) for p in _search_paths()]}: {verb}")
