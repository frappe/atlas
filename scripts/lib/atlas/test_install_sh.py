"""Unit tests for scripts/install.sh — the POSIX-sh successor to
bootstrap-server.py's ensure_atlas_env(). Stdlib-only — run with
`python3 -m unittest atlas.test_install_sh` from scripts/lib.

install.sh creates the host's Atlas venv + `atlas` console script and runs the
deep sanity gate; it is the carve-out's replacement (the controller runs it over
SSH BEFORE the bootstrap Task, so bootstrap-server then runs as a normal
`atlas <verb>` on the venv like every other verb). install.sh hardcodes the
canonical /var/lib/atlas paths (it must — the systemd units reference the same
literals), so it can't be redirected at a temp root without editing it; these
tests therefore pin it WITHOUT a host two ways: it is valid POSIX sh, and it
issues the same uv/venv/install/gate steps the Python it replaced did — the
structural mirror of test_bootstrap.py's TestEnsureAtlasEnv. The live, end-to-end
gate (a real uv venv on a real host) is the e2e _verify_verbs check.
"""

import os
import shutil
import subprocess
import unittest

# scripts/lib/atlas/test_install_sh.py → scripts/install.sh
_INSTALL = os.path.join(
	os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
	"install.sh",
)


class TestInstallSh(unittest.TestCase):
	def setUp(self):
		with open(_INSTALL) as handle:
			self.text = handle.read()

	def test_is_valid_posix_sh(self):
		# `sh -n` parses without executing — catches a syntax slip on any host /bin/sh.
		sh = shutil.which("sh")
		if not sh:
			self.skipTest("no POSIX sh on this runner")
		result = subprocess.run([sh, "-n", _INSTALL], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_aborts_on_any_failure(self):
		# `set -e` (or -eu) is load-bearing: a broken step must abort the install,
		# not press on and leave a half-built venv the units would then point at.
		self.assertRegex(self.text, r"(?m)^set -eu?\b")

	def test_pins_uv_and_python_versions(self):
		# install.sh is the SINGLE SOURCE OF TRUTH for the pinned versions now (they
		# moved out of bootstrap-server.py). PY_VERSION must be a python-build-
		# standalone build uv can fetch; UV_VERSION is embedded in the install URL.
		self.assertIn('UV_VERSION="0.9.30"', self.text)
		self.assertIn('PY_VERSION="3.14.3"', self.text)

	def test_installs_pinned_uv_to_the_fixed_dir(self):
		# The one genuine network fetch: the pinned uv into the single /var/lib/atlas/uv
		# root, no PATH/profile edits.
		self.assertIn("astral.sh/uv/${UV_VERSION}/install.sh", self.text)
		self.assertIn("UV_INSTALL_DIR=${UV_DIR}", self.text)
		self.assertIn("UV_UNMANAGED_INSTALL=1", self.text)

	def test_creates_venv_on_the_controlled_python(self):
		self.assertIn('venv --python "${PY_VERSION}" "${ATLAS_VENV}"', self.text)

	def test_pip_installs_the_package_from_the_durable_tree(self):
		# Installs from the directory the controller already scp'd — install.sh is
		# NOT a code-transport mechanism; the package is already at BIN_DIRECTORY.
		self.assertIn("VIRTUAL_ENV=${ATLAS_VENV}", self.text)
		self.assertIn('pip install --reinstall "${BIN_DIRECTORY}"', self.text)

	def test_exposes_the_console_script_on_path(self):
		self.assertIn('ln -sfn "${ATLAS_CLI}" /usr/local/bin/atlas', self.text)

	def test_deep_sanity_gate_exercises_lvm_import_hook_compile_and_cli(self):
		# (a) the atlas-pool.service inline import — the largest module, the likeliest
		#     stdlib gap on a fresh interpreter — on the venv python.
		self.assertIn("from atlas.lvm import ThinPool", self.text)
		# (b) all four firecracker-vm@.service boot hooks py_compile'd on the venv python.
		self.assertIn("py_compile", self.text)
		for hook in ("vm-disk-up.py", "vm-network-up.py", "vm-network-down.py", "vm-restore.py"):
			self.assertIn(hook, self.text)
		# (c) the `atlas` console script dispatches.
		self.assertIn('"${ATLAS_CLI}" --help', self.text)

	def test_version_mismatch_aborts(self):
		# A venv on the WRONG CPython must fail the install loudly (exit 1) — a unit
		# pointing at a mismatched interpreter is never reached.
		self.assertIn("expected ${PY_VERSION}", self.text)
		self.assertIn("exit 1", self.text)

	def test_paths_match_the_durable_layout(self):
		# The literals are repeated from paths.py (the trees don't share imports);
		# pin the venv root so a drift from paths.ATLAS_VENV is caught here.
		self.assertIn('ATLAS_VENV="${ATLAS_ROOT}/venv"', self.text)
		self.assertIn('BIN_DIRECTORY="${ATLAS_ROOT}/bin"', self.text)


if __name__ == "__main__":
	unittest.main()
