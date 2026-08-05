"""Unit tests for the two SOURCE-side migration entry scripts (spec/24).

Run with bare `python3 -m unittest atlas.test_migration_source_scripts` from
scripts/lib, like test_sleep_vm.py: no Frappe, no site, no host. Each test loads
the entry script by path and replaces the module's `run` / `run_ok` / `shell` /
`ThinPool` globals, so `main()` runs end-to-end and every host poke is recorded
instead of executed.

Both properties under test are ones whose failure is silent on the host and only
visible to the customer:

  - `migration-cleanup-source.py` must NOT re-run `vm-network-down.py` on a
    keep-address migration. That script deletes the source's proxy-NDP entry for
    the VM's /128 and sweeps every `inet atlas forward` rule mentioning it — the
    exact rules `migration-source-forward.py` installed at cutover — so running
    it there takes down the migrated VM's public ingress while Atlas reports the
    migration `Done` (egress keeps working; the field symptom).
  - `migration-source-autostart.py` must use a PLAIN `systemctl disable`. `--now`
    would stop a unit the phase machine may still want up, and `mask` would break
    the spec/24 §3 rollback ("just restarts the intact source VM").
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2]

UUID = "5d0943c8-4e43-48ad-b652-3f181e22fc4d"


def _load(stem: str):
	if str(_SCRIPTS_DIR / "lib") not in sys.path:
		sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
	spec = importlib.util.spec_from_file_location(stem.replace("-", "_"), _SCRIPTS_DIR / f"{stem}.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class _FakeVolume:
	"""An LV whose only interesting act is being removed."""

	def __init__(self, removed: list, name: str) -> None:
		self._removed = removed
		self.name = name

	def remove(self) -> None:
		self._removed.append(self.name)


class _FakeThinPool:
	def __init__(self, removed: list) -> None:
		self._removed = removed

	def from_device(self, device_path: str) -> _FakeVolume:
		return _FakeVolume(self._removed, device_path.rsplit("/", 1)[-1])

	def vm_disk(self, uuid: str) -> _FakeVolume:
		return _FakeVolume(self._removed, f"atlas-vm-{uuid}")

	def data_disk(self, uuid: str) -> _FakeVolume:
		return _FakeVolume(self._removed, f"atlas-data-{uuid}")


class _HostRecorder:
	"""Stands in for atlas._run's run / run_ok / shell inside a loaded entry script."""

	def __init__(self) -> None:
		self.commands: list[str] = []
		self.removed: list[str] = []

	def install(self, module) -> None:
		module.run = self.run
		module.run_ok = self.run_ok
		if hasattr(module, "shell"):
			module.shell = self.shell
		if hasattr(module, "ThinPool"):
			module.ThinPool = lambda: _FakeThinPool(self.removed)

	def _record(self, template: str, args: tuple) -> str:
		rendered = template.format(*args) if "{}" in template else template
		self.commands.append(rendered)
		return rendered

	def run(self, template, *args, **kwargs) -> str:
		self._record(template, args)
		return ""

	def run_ok(self, template, *args, **kwargs) -> bool:
		# True for every probe: the worst case for a teardown guard is a host where
		# every artifact it might tear down is still present.
		self._record(template, args)
		return True

	def shell(self, command, **kwargs) -> str:
		self.commands.append(command)
		return ""

	def issued(self, needle: str) -> list[str]:
		return [command for command in self.commands if needle in command]


class TestCleanupSourceKeepAddress(unittest.TestCase):
	def _cleanup(self, argv: list[str]) -> _HostRecorder:
		module = _load("migration-cleanup-source")
		host = _HostRecorder()
		host.install(module)
		original_argv = sys.argv
		try:
			sys.argv = ["migration-cleanup-source", *argv]
			module.main()
		finally:
			sys.argv = original_argv
		return host

	def test_keep_address_spares_the_permanent_forward(self) -> None:
		# The whole defect: this teardown deletes the proxy-NDP entry + the /128's
		# nft forward rules that ARE the migrated VM's delivery path (spec/24 §2.9.4).
		host = self._cleanup(["--virtual-machine-name", UUID, "--keep-address", "1"])
		self.assertEqual(host.issued("vm-network-down.py"), [])

	def test_keep_address_still_destroys_the_stale_source_copy(self) -> None:
		# Only the networking step is spared — the disk copy must still go, or the
		# source host keeps a full second copy of the guest's disk forever.
		host = self._cleanup(["--virtual-machine-name", UUID, "--keep-address", "1"])
		self.assertTrue(host.issued(f"disable --now firecracker-vm@{UUID}.service"))
		self.assertTrue(host.issued(f"rm -rf /var/lib/atlas/virtual-machines/{UUID}"))
		self.assertIn(f"atlas-vm-{UUID}", host.removed)
		self.assertIn(f"atlas-data-{UUID}", host.removed)
		self.assertIn(f"atlas-snap-{UUID}-migrate", host.removed)

	def test_change_address_tears_the_networking_down(self) -> None:
		# The other branch is unchanged: a change-address migration leaves nothing
		# behind on the source, so the defensive re-run still happens.
		host = self._cleanup(["--virtual-machine-name", UUID, "--keep-address", "0"])
		self.assertEqual(len(host.issued("vm-network-down.py")), 1)

	def test_teardown_is_the_default(self) -> None:
		# An omitted flag must not silently mean "keep" — the change-address path is
		# the default and it tears down.
		host = self._cleanup(["--virtual-machine-name", UUID])
		self.assertEqual(len(host.issued("vm-network-down.py")), 1)


class TestSourceAutostart(unittest.TestCase):
	def _autostart(self, argv: list[str]) -> _HostRecorder:
		module = _load("migration-source-autostart")
		host = _HostRecorder()
		host.install(module)
		original_argv = sys.argv
		try:
			sys.argv = ["migration-source-autostart", *argv]
			module.main()
		finally:
			sys.argv = original_argv
		return host

	def test_default_disables_the_unit(self) -> None:
		host = self._autostart(["--virtual-machine-name", UUID])
		self.assertEqual(host.commands, [f"sudo systemctl disable firecracker-vm@{UUID}.service"])

	def test_disable_is_never_now_and_never_mask(self) -> None:
		# `--now` would stop a unit the phase machine may still want running, and a
		# mask would break the spec/24 §3 rollback (an explicit `systemctl start`).
		host = self._autostart(["--virtual-machine-name", UUID, "--enabled", "0"])
		self.assertEqual(host.issued("--now"), [])
		self.assertEqual(host.issued("mask"), [])

	def test_enabled_one_restores_autostart(self) -> None:
		# The inverse an abandoned migration runs so the resurrected source survives
		# its host's next reboot.
		host = self._autostart(["--virtual-machine-name", UUID, "--enabled", "1"])
		self.assertEqual(host.commands, [f"sudo systemctl enable firecracker-vm@{UUID}.service"])


if __name__ == "__main__":
	unittest.main()
