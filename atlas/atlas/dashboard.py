"""Ship the read-only host dashboard onto a Server during bootstrap.

The dashboard (a sibling tree, `dashboard/`) is a Vite/Vue SPA plus one
stdlib-only backend (`dashboard/backend/server.py`) that serves the built
assets and a live `/api/state`. On the host it runs under systemd **socket
activation** — systemd owns the listening socket and starts the server on the
first request — so it costs nothing while idle.

This module is the controller half of the ship: it (best-effort) BUILDS the SPA
on the controller and returns the upload manifest + the host-side enable
command. `Server.bootstrap` folds these into the same upload → install → enable
sweep it already runs for the atlas package.

Two rules the caller relies on:

  - **Fail silently.** A missing `npm`, absent node_modules, or a build error
    must never fail a Server bootstrap — the dashboard is a convenience, not a
    dependency. Every entry point here degrades to "ship nothing" and logs, it
    never raises.
  - **Never ship stale assets.** `dist/` is a gitignored build artifact, so an
    old copy left in the tree is the real hazard. We ship assets ONLY from a
    build this call just ran successfully; if the build fails we ship nothing,
    even if a stale `dist/` is sitting there. The freshly-built assets are
    self-consistent with the `server.py` shipped alongside them.
"""

import shutil
import subprocess
from pathlib import Path

import frappe

from atlas.atlas import scripts_catalog

# Host layout. The dashboard is self-contained under one root, served by systemd
# socket activation on the port baked into the .socket unit (see the unit files).
REMOTE_ROOT = "/opt/atlas-dashboard"
SOCKET_UNIT = "atlas-dashboard.socket"

# npm build budget. A cold build (no cache) compiles Vue + Tailwind; a warm one
# is seconds. Generous so a slow controller doesn't spuriously "fail" the build
# and silently drop the dashboard — but bounded so a wedged npm can't hang boot.
BUILD_TIMEOUT_SECONDS = 300


def dashboard_directory() -> Path:
	"""The `dashboard/` tree — sibling of `scripts/` at the repo root."""
	return scripts_catalog.scripts_directory().parent / "dashboard"


def build_dashboard() -> Path | None:
	"""Build the SPA on the controller; return the fresh `dist/` dir, or None.

	Best-effort and silent: any reason the build can't run or doesn't finish
	clean (no npm, no package.json, no node_modules, non-zero exit, timeout)
	returns None so the caller ships no assets rather than failing bootstrap.

	On success the returned `dist/` was produced by THIS call, so it is never
	stale relative to the source tree — the freshness guarantee the caller needs.
	"""
	directory = dashboard_directory()
	npm = shutil.which("npm")
	if npm is None:
		frappe.logger("atlas").info("dashboard build skipped: npm not on PATH")
		return None
	if not (directory / "package.json").exists():
		frappe.logger("atlas").info(f"dashboard build skipped: no package.json in {directory}")
		return None
	if not (directory / "node_modules").exists():
		# `npm ci`/`install` is the developer's job — we don't mutate the tree's
		# deps from a bootstrap. Without them, degrade to shipping nothing.
		frappe.logger("atlas").info("dashboard build skipped: node_modules absent (run npm install)")
		return None

	dist = directory / "dist"
	# Remove any prior dist first so a build that fails mid-way can't leave a
	# half-written tree that then looks shippable. Only a clean rebuild ships.
	if dist.exists():
		shutil.rmtree(dist, ignore_errors=True)

	try:
		result = subprocess.run(
			[npm, "run", "build"],
			cwd=str(directory),
			capture_output=True,
			text=True,
			timeout=BUILD_TIMEOUT_SECONDS,
			check=False,
		)
	except (subprocess.TimeoutExpired, OSError) as exception:
		frappe.logger("atlas").warning(f"dashboard build failed to run: {exception}")
		return None

	if result.returncode != 0:
		frappe.logger("atlas").warning(
			f"dashboard build exited {result.returncode}; shipping no assets. "
			f"stderr tail: {result.stderr[-500:]}"
		)
		return None
	if not (dist / "index.html").exists():
		frappe.logger("atlas").warning("dashboard build finished but produced no dist/index.html")
		return None
	return dist


def dashboard_uploads() -> list[tuple[str, str]]:
	"""(local, remote) pairs to scp the dashboard onto a host: freshly-built
	assets, the stdlib backend, and the two systemd units. Empty if the build
	could not be produced — the caller then ships nothing and does not enable
	the unit, so a host simply has no dashboard until the next bootstrap that
	can build it.

	Built assets and `server.py` travel together so the shipped pair is always
	self-consistent; the units are static files under the tree."""
	dist = build_dashboard()
	if dist is None:
		return []

	directory = dashboard_directory()
	backend = directory / "backend"
	systemd = backend / "systemd"

	uploads: list[tuple[str, str]] = [
		(str(backend / "server.py"), f"{REMOTE_ROOT}/server.py"),
		(str(systemd / "atlas-dashboard.socket"), "/etc/systemd/system/atlas-dashboard.socket"),
		(str(systemd / "atlas-dashboard.service"), "/etc/systemd/system/atlas-dashboard.service"),
	]
	# Every file under the built dist/, preserving its path beneath dist/ →
	# REMOTE_ROOT/dist/. upload_files() mkdir -p's each remote parent, so nested
	# assets (dist/assets/*) land correctly.
	for entry in sorted(dist.rglob("*")):
		if entry.is_file():
			relative = entry.relative_to(dist)
			uploads.append((str(entry), f"{REMOTE_ROOT}/dist/{relative.as_posix()}"))
	return uploads


def enable_command() -> str:
	"""The host-side shell line that activates the socket unit after the upload:
	reload systemd (new unit files), enable + start the SOCKET (not the service —
	socket activation starts the service on demand). Idempotent, so a re-bootstrap
	just re-converges. `systemctl enable --now …socket` both enables at boot and
	starts the listener now."""
	return f"systemctl daemon-reload && systemctl enable --now {SOCKET_UNIT}"
