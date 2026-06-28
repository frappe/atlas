"""Guards the packaging contract of the Atlas host scripts.

These are not behaviour tests — they pin the manifest invariants that keep the
host install working (see spec/03-bootstrapping.md):

  - the floor contract is declared: requires-python pins the lowest version the
    code runs on. With the uv-managed venv (a pinned CPython 3.14) every host
    Python verb runs as `atlas <verb>` under that venv — nothing host-side runs on
    stock Ubuntu python anymore — so the honest floor is 3.14, not the old 3.12
    except-tuple boundary. CI runs `compileall` on that floor; this test only
    asserts the contract exists and is the floor.
  - the dev manifest (scripts/pyproject.toml) and the host manifest
    (scripts/host-pyproject.toml, `uv pip install`ed on a host) stay in lockstep:
    same name, console entry, and floor. They differ ONLY in the wheel package
    root (lib/atlas vs atlas), reflecting the dev tree vs the flat durable host
    tree at /var/lib/atlas/bin.

`dependencies` is empty today (the code is stdlib-only), but that is no longer a
guarded invariant — a real dependency is fine; uv resolves it at install.

Run with bare `python3 -m unittest atlas.test_packaging` from scripts/lib.
"""

import os

# tomllib is stdlib since 3.11; the floor is 3.14, so this import is always safe.
import tomllib
import unittest

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYPROJECT = os.path.join(_SCRIPTS_DIR, "pyproject.toml")
_HOST_PYPROJECT = os.path.join(_SCRIPTS_DIR, "host-pyproject.toml")


def _load(path):
	with open(path, "rb") as handle:
		return tomllib.load(handle)


class TestPackagingInvariants(unittest.TestCase):
	def setUp(self):
		self.meta = _load(_PYPROJECT)

	def test_requires_python_floor_declared(self):
		# The floor is >=3.14 — the version of the uv-managed Atlas venv that every
		# host Python verb runs under. Pin the exact string so a silent change trips
		# this test and forces a deliberate decision (editing this assertion IS that
		# decision).
		self.assertEqual(self.meta["project"]["requires-python"], ">=3.14")

	def test_console_entry_points_at_the_cli(self):
		self.assertEqual(self.meta["project"]["scripts"]["atlas"], "atlas._cli:main")


class TestHostManifestInLockstep(unittest.TestCase):
	"""The host manifest is what `uv pip install` actually consumes on a host;
	if it drifts from the dev one, a host gets a different package."""

	def setUp(self):
		self.dev = _load(_PYPROJECT)
		self.host = _load(_HOST_PYPROJECT)

	def test_name_console_entry_and_floor_match(self):
		for key in ("name", "requires-python"):
			self.assertEqual(self.host["project"][key], self.dev["project"][key], key)
		self.assertEqual(self.host["project"]["scripts"]["atlas"], self.dev["project"]["scripts"]["atlas"])

	def test_host_wheel_package_root_is_flat_atlas(self):
		# The durable host tree is /var/lib/atlas/bin/atlas/*.py — flat, not nested
		# under lib/. The host manifest must package `atlas`, not `lib/atlas`.
		self.assertEqual(self.host["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"], ["atlas"])
		self.assertEqual(self.dev["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"], ["lib/atlas"])


if __name__ == "__main__":
	unittest.main()
