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

OPERATOR_VISIBLE: frozenset[str] = frozenset(
	{
		# Bootstrap and Reboot have dedicated buttons with confirmation guards on
		# the Server form; exposing the raw scripts in the Run Task picker
		# duplicates those flows without the guards. `sync-image.py` is the only
		# ad-hoc script the operator should reach for from here.
		"sync-image.py",
	}
)


# Per-script Run Task dialog metadata. The client renders the dialog purely
# from this — script names, intros, and field schemas all live here. Each
# entry is `{intro: str, fields: list[dict]}`; field dicts use Frappe Dialog
# field shapes (`fieldname`, `fieldtype`, `label`, `default`, `reqd`, ...).
SCRIPT_FORMS: dict[str, dict] = {
	"bootstrap-server.py": {
		"intro": "Idempotent. Safe to re-run on an Active server.",
		"fields": [
			{
				"fieldname": "FIRECRACKER_VERSION",
				"label": "Firecracker Version",
				"fieldtype": "Data",
				"default": "v1.15.1",
				"reqd": 1,
			},
			{
				"fieldname": "SSHPIPER_VERSION",
				"label": "SSHPiper Version",
				"fieldtype": "Data",
				"default": "v1.5.4",
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
	# reboot-server.sh stays a shell script (two lines; not worth porting).
	"reboot-server.sh": {
		"intro": "Reboots the host. SSH drops mid-Task; the Task may end Failure — that is normal.",
		"fields": [],
	},
	"sync-image.py": {
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
	"""Return the Run Task dialog metadata for `script`, or an empty form
	(no intro, no fields) for scripts that don't need any variables."""
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


# Systemd-invoked hooks live in scripts/ but are NOT Task-runnable: they take a
# positional VM uuid (passed by the unit's ExecStartPre/ExecStopPost as `%i`),
# not the --flag CLI contract a Task uses, and they import the durable package.
# Excluded from the catalog so the runner never executes them as a Task.
SYSTEMD_HOOKS: frozenset[str] = frozenset(
	{
		"vm-disk-up.py",
		"vm-network-up.py",
		"vm-network-down.py",
		"vm-restore.py",
	}
)

# Controller-only Tasks: they run on the Atlas controller via the local runner
# (atlas.atlas.local_task), NOT over SSH onto a Server host. `resolve()` must
# still find them, but they are not host SSH tasks, so they are excluded from
# `allowed_scripts()` (the host run-task gate) and the operator picker.
CONTROLLER_ONLY: frozenset[str] = frozenset(
	{
		"issue-cert.py",
		# Central-managed tunnel + management-plane firewall (spec/21-tunnel.md). These
		# run on the Atlas host via run_local_task, driven by the central_link API
		# during registration — never host SSH Tasks, never in the operator picker.
		"tunnel-up.py",
		"tunnel-down.py",
		"mgmt-firewall-apply.py",
		"mgmt-firewall-revert.py",
		"mgmt-firewall-confirm.py",
	}
)


def allowed_scripts() -> list[str]:
	"""Return the sorted list of task-runnable script filenames on a server host.

	Both `.py` (the typed CLI tasks) and `.sh` (the few remaining shell tasks,
	e.g. reboot-server.sh) are runnable. The systemd hooks and controller-only
	tasks are excluded — they are not host SSH Tasks (see SYSTEMD_HOOKS /
	CONTROLLER_ONLY)."""
	directory = scripts_directory()
	if not directory.is_dir():
		return []
	excluded = SYSTEMD_HOOKS | CONTROLLER_ONLY
	return sorted(
		entry.name
		for entry in directory.iterdir()
		if entry.is_file() and entry.suffix in (".py", ".sh") and entry.name not in excluded
	)


def operator_visible_scripts() -> list[str]:
	"""Subset of allowed_scripts() that the Run Task dialog should expose."""
	return sorted(name for name in allowed_scripts() if name in OPERATOR_VISIBLE)


# Production Task scripts are shipped durably to the host's /var/lib/atlas/bin by
# Server.bootstrap()/sync_scripts() — the same place the importable atlas package
# and the systemd hooks already live — and invoked there by the SSH runner with
# no per-Task scp (the dominant latency of a start/stop/snapshot Task). The dir
# equals runner.DURABLE_PACKAGE_DIRECTORY; the literal is repeated here so
# server.py and the runner agree on one location without importing each other.
DURABLE_SCRIPT_DIRECTORY = "/var/lib/atlas/bin"


def host_task_scripts() -> list[str]:
	"""Production Task scripts shipped durably to /var/lib/atlas/bin — exactly
	allowed_scripts(), every host SSH Task entry point. Bootstrap / sync_scripts
	upload these so the runner invokes them in place. e2e probe scripts live in
	the test-only directory, are not shipped durably, and keep the staging path."""
	return allowed_scripts()


def durable_remote_path(script: str) -> str | None:
	"""The /var/lib/atlas/bin path the runner invokes for a durably-shipped Task
	script, or None when the script isn't shipped durably (an e2e probe resolved
	from the test directory) — which the runner stages per Task instead."""
	if script in host_task_scripts():
		return f"{DURABLE_SCRIPT_DIRECTORY}/{script}"
	return None


def resolve(script: str) -> Path:
	"""Locate a script in either the production or e2e directory. Raises
	FileNotFoundError if not present in either."""
	for directory in _search_paths():
		candidate = directory / script
		if candidate.is_file():
			return candidate
	raise FileNotFoundError(f"Script not found in {[str(p) for p in _search_paths()]}: {script}")
