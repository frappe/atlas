"""The `atlas` host CLI — one front door for the typed Task entry points.

A debug/break-glass face for an operator on a host: `atlas stop-vm
--virtual-machine-name <uuid>`, `atlas --help` lists every command. It is pure
convenience over code that already exists — each entry point under scripts/ is
already a typed argparse subcommand (TaskInputs.from_args), so this module only
*dispatches* to one; it adds no new logic and no new flags.

Design notes (see spec/03-bootstrapping.md § The Atlas interpreter and CLI):

- Commands are derived from the entry scripts on disk, not hardcoded, so a new
  Task entry point appears automatically. The four systemd hooks are excluded
  by construction: they read a positional uuid, declare no `command` ClassVar
  and no TaskInputs subclass, and are invoked only by firecracker-vm@.service —
  never by a person. We exclude them the same way scripts_catalog does, but from
  the filesystem (this module is pure-stdlib and must not import the
  Frappe-dependent catalog).

- Where the entry scripts live depends on how the CLI runs (see `_scripts_dir`):
  INSTALLED on a host this module sits in the venv's site-packages, so it reads
  the durable entry scripts at /var/lib/atlas/bin; in the dev/loose tree it reads
  the sibling scripts/ dir. Either way, dispatch is import-by-path — the entry
  scripts are <stem>.py with hyphens (not importable as modules) and the runner
  resolves them by path too. Each entry guards main() under
  `if __name__ == "__main__"`, so importing it does not run it; we re-point
  sys.argv and call main() ourselves.

- provision-vm / rebuild-vm ARE reachable here (the debug escape hatch) but the
  friendly `atlas vm …` grammar (a later PR) deliberately gives them no short
  verb — `atlas vm create` would be a lie (~20 of provision-vm's flags are
  controller-computed).
"""

from __future__ import annotations

import importlib.util
import os
import sys

from atlas.paths import BIN_DIRECTORY


def _scripts_dir() -> str:
	"""Where the entry scripts (`start-vm.py`, …) live, for dispatch by path.

	Two cases, because the CLI runs both ways:
	  - INSTALLED on a host: bootstrap `uv pip install`s this package into the Atlas
	    venv, so this module sits in the venv's site-packages — NOT beside the entry
	    scripts. The durable scripts live at BIN_DIRECTORY (/var/lib/atlas/bin),
	    where Server.bootstrap placed them; resolve there.
	  - DEV / loose tree: this file is scripts/lib/atlas/_cli.py and the entries are
	    two directories up at scripts/. The site-packages dir has no entry scripts,
	    so we fall back to the sibling layout when BIN_DIRECTORY isn't populated.
	"""
	if os.path.isdir(BIN_DIRECTORY) and any(name.endswith(".py") for name in os.listdir(BIN_DIRECTORY)):
		return BIN_DIRECTORY
	# scripts/lib/atlas/_cli.py → scripts/
	return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# The per-VM firecracker-vm@ hooks (vm-disk-up / vm-network-up / vm-restore /
# vm-network-down) were listed here so `atlas <verb>` never dispatched a
# positional-uuid hook as a Task. They are `/usr/local/bin/boat vm-* %i` verbs
# now, own no .py in scripts/, and so can no longer be stems — the exclusion set
# is empty. Kept as the seam a future scripts/ hook re-enters. (atlas-wake-trap.py
# is the one remaining systemd-hook file in scripts/; it is out of scope for this
# cutover and, as before, is not excluded here.)
# See spec/04-tasks.md § "Systemd hooks are Python too, but not Tasks".
_SYSTEMD_HOOKS: frozenset[str] = frozenset()


def _stems() -> dict[str, str]:
	"""Map subcommand name → absolute path of its <stem>.py entry script.

	A stem is a `.py` file directly in the scripts dir (see `_scripts_dir`) that is
	not a systemd hook and not private (_-prefixed). `.sh` scripts
	(reboot-server.sh) are not importable and are intentionally absent — they stay
	reachable as bare scripts."""
	scripts_dir = _scripts_dir()
	stems = {}
	for name in sorted(os.listdir(scripts_dir)):
		if not name.endswith(".py") or name.startswith("_") or name in _SYSTEMD_HOOKS:
			continue
		path = os.path.join(scripts_dir, name)
		if os.path.isfile(path):
			stems[name[: -len(".py")]] = path
	return stems


def _load(path: str):
	"""Import a scripts/<stem>.py file as a module without executing main()
	(the entry guards main() behind `if __name__ == "__main__"`)."""
	spec = importlib.util.spec_from_file_location("_atlas_entry", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _usage(stems: dict[str, str]) -> str:
	commands = "  ".join(sorted(stems))
	return (
		"usage: atlas <command> [--flags]\n\n"
		f"commands:\n  {commands}\n\n"
		"Run `atlas <command> --help` for a command's flags.\n"
		"VMs are addressed by UUID. To CREATE a VM, run it from the controller — "
		"there is no `atlas vm create`."
	)


def main(argv: list[str] | None = None) -> None:
	argv = sys.argv[1:] if argv is None else list(argv)
	stems = _stems()

	if not argv or argv[0] in ("-h", "--help"):
		print(_usage(stems))
		raise SystemExit(0 if argv else 2)

	command = argv[0]
	if command not in stems:
		print(f"atlas: unknown command {command!r}\n", file=sys.stderr)
		print(_usage(stems), file=sys.stderr)
		raise SystemExit(2)

	module = _load(stems[command])
	# Re-point argv to the bare-script form: the entry's main() runs its own
	# TaskInputs.from_args(), which parses argv[1:] and gives --help for free.
	sys.argv = [command, *argv[1:]]
	module.main()


if __name__ == "__main__":
	main()
