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
	# What `Server.bootstrap()` sends, as `boat bootstrap --firecracker-version …
	# --architecture …` (BOAT_ONLY_VERBS). Keyed on the live `bootstrap` verb — the
	# Python `bootstrap-server` oracle is deleted, so there is no `.py` an operator
	# could pick by hand.
	"bootstrap": {
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


# Systemd-invoked scripts live in scripts/ but are NOT Task-runnable: the per-VM
# hooks take a positional VM uuid (passed by the unit's ExecStartPre/ExecStopPost as
# `%i`), and atlas-wake-trap is an always-on daemon (no args) — neither speaks the
# --flag CLI contract a Task uses, and all import the durable package. Excluded from
# the catalog (by verb) so the runner never executes them as a Task.
SYSTEMD_HOOKS: frozenset[str] = frozenset(
	{
		"vm-disk-up",
		"vm-network-up",
		"vm-network-down",
		"vm-restore",
		"atlas-wake-trap",
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

	The file-derived verbs — both Python (the typed CLI tasks) and shell (the few
	remaining, e.g. reboot-server) — UNION the BOAT_ONLY verbs, which the boat
	binary implements with no file in scripts/ (see BOAT_ONLY_VERBS). A BOAT_ONLY
	verb is runnable even though nothing on disk carries it, so the run-task gate
	must admit it; it simply ships no durable file (see host_task_scripts). The
	systemd hooks and controller-only tasks are excluded — they are not host SSH
	Tasks (see SYSTEMD_HOOKS / CONTROLLER_ONLY)."""
	excluded = SYSTEMD_HOOKS | CONTROLLER_ONLY
	verbs = set(_verbs_in(scripts_directory())) | set(BOAT_ONLY_VERBS)
	return sorted(verb for verb in verbs if verb not in excluded)


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
	if verb in BOAT_ONLY_VERBS:
		# No file on disk to ask — the boat binary IS the implementation (see
		# BOAT_ONLY_VERBS). The runner reads this to pick a command SHAPE, and a
		# boat verb's shape is the flag-taking one: `<entry> <verb> --kebab-flag
		# value`, the same line `boat snapshot-vm` gets. "python" is that shape's
		# name here for historical reasons; it never meant "run an interpreter".
		return "python"
	return "shell" if file_for(verb).endswith(".sh") else "python"


# Host verbs the `boat` binary implements (spec/33-boat.md, WO-6, item 9). The
# runner invokes each as `boat <verb> --flags` instead of `atlas <verb> --flags`:
# `boat` takes the same `--kebab-case` flags the Python TaskInputs declared and
# prints the same one `ATLAS_RESULT=` line the controller parses back, so the seam
# is the first word of the command and nothing downstream can tell the difference.
#
# The set is split by whether the Python oracle is still on disk. A verb starts in
# _PORTED_VERBS — boat implements it, its scripts/*.py is still present as the
# conformance oracle (runnable by hand as `atlas <verb>`) — and MOVES to
# BOAT_ONLY_VERBS the moment that .py is deleted (the Boat-verb Python deletion).
# Once moved there is no file to read, so kind() cannot sniff a suffix, file_for()
# raises, and durable_remote_path() ships nothing. BOAT_VERBS is their union and
# does not change as a verb crosses over — runs_on_boat() gates on the union.
#
# NOT in either set, deliberately:
#   - the CONTROLLER_ONLY verbs. `issue-cert`, `tunnel-*` and `mgmt-firewall-*`
#     run on the Atlas controller through run_local_task, not on a Server host,
#     and the controller is not a Boat host.
#   - the SYSTEMD_HOOKS. Those are invoked by the per-VM unit with `%i`, never
#     as a Task, so their cutover is in the unit template rather than here.
#   - the `migration-*` verbs. Boat serves most of those over the daemon's phase
#     RPCs (run_boat_migration_phase), so routing them is a change to migration.py's
#     transport, not the first word; two (source-autostart, forward-down) remain
#     controller-side over SSH and keep their .py.
_PORTED_VERBS: frozenset[str] = frozenset(
	{
		# provision-vm + the five later ports (firewall/tunnel/traffic/wake/export).
		# The boat binary implements each as a native verb taking the same
		# --kebab-flags and printing the same ATLAS_RESULT= line, so the runner swaps
		# only the first word. provision-vm creates LVs and boots a guest, so its
		# differential is a real guest boot on a host, not goldens alone.
		"provision-vm",
	}
)

# Verbs the boat binary implements and NOTHING in scripts/ does — a verb lands
# here once its `.py` oracle is deleted (see the header above), plus `bootstrap`.
# For these there is no file to look at: `kind()` cannot read a suffix,
# `file_for()` raises, `durable_remote_path()` ships nothing, and they are runnable
# only because `allowed_scripts()` unions this set in.
#
# `bootstrap` is a rename rather than a straight port. The Task was
# `bootstrap-server` (scripts/bootstrap-server.py, now deleted); the host prep
# Atlas drives is `boat bootstrap --firecracker-version … --architecture …`, the
# same two flags and the same ATLAS_RESULT= line. The verb had to change because
# the runner renders `<entry> <verb>` and `boat bootstrap-server` is not a command
# boat has (spec/33-boat.md §4, WO-1b). `bootstrap-server` keeps a SCRIPT_LABELS /
# RETRYABLE entry and a Fake result builder for historical Task rows, but is no
# longer a runnable verb — its .py is gone and it is not in allowed_scripts().
BOAT_ONLY_VERBS: frozenset[str] = frozenset(
	{
		"bootstrap",
		"snapshot-vm",
		"snapshot-stop-vm",
		"warm-snapshot-vm",
		"delete-snapshot-vm",
		"upload-snapshot-s3",
		"restore-snapshot-s3",
		# sync-image keeps its per-Task sidecar (script_uploads.SCRIPT_SIDECARS
		# bakes the guest atlas-network.service); that upload is independent of the
		# deleted .py — Boat reads the sidecar from the same staged path.
		"sync-image",
		"promote-snapshot-image",
		"regenerate-host-keys-vm",
		"reset-server",
		"export-cleanup-source",
		"vm-tunnel",
		"poll-vm-traffic",
		"probe-woken-vms",
		"firewall-apply",
	}
)

BOAT_VERBS: frozenset[str] = _PORTED_VERBS | BOAT_ONLY_VERBS


def runs_on_boat(verb: str) -> bool:
	"""True iff the host runs this verb as `boat <verb>`."""
	return verb in BOAT_VERBS


# Production Task scripts are shipped durably to the host's /var/lib/atlas/bin by
# Server.bootstrap()/sync_scripts() — the same place the importable atlas package
# and the systemd hooks already live. Python verbs are then invoked as
# `atlas <verb>` (the pip-installed console script); shell verbs run in place by
# path. Either way no per-Task scp (the dominant latency of a start/stop/snapshot
# Task). The literal is repeated here (and in server.py / install.sh) so each tree
# agrees on one location without importing the others.
DURABLE_SCRIPT_DIRECTORY = "/var/lib/atlas/bin"


def host_task_scripts() -> list[str]:
	"""Production Task verbs shipped durably to /var/lib/atlas/bin: the FILE-derived
	host SSH Tasks only. A BOAT_ONLY verb ships NO file — the boat binary is its
	implementation, so there is nothing to upload for it — which is why this is the
	file-derived subset of allowed_scripts() rather than all of it. Bootstrap /
	sync_scripts upload the FILES (verb→file_for) so the runner invokes them in
	place. e2e probe scripts live in the test-only directory, are not shipped
	durably, and keep the staging path."""
	excluded = SYSTEMD_HOOKS | CONTROLLER_ONLY
	return sorted(verb for verb in _verbs_in(scripts_directory()) if verb not in excluded)


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
