"""Unit tests for the §7 break-glass ergonomics on the two host entry points
(`sync-image`, `provision-vm`). Stdlib-only — run with
`python3 -m unittest atlas.test_entry_ergonomics` from scripts/lib.

These pin the additive defaults (a hand run can omit the constant-ish flags) and
the enriched `--help` (the derived flags name their atlas.networking source
function). The controller still passes the full flag set; the defaults must never
change that — so we also assert an explicit value overrides the default.
"""

import argparse
import io
import unittest

from atlas import _cli


def _load(stem: str):
	return _cli._load(_cli._stems()[stem])


def _inputs_cls(module, command: str):
	from atlas._task import TaskInputs

	return next(
		value
		for value in vars(module).values()
		if isinstance(value, type)
		and issubclass(value, TaskInputs)
		and getattr(value, "command", "") == command
	)


# The required flags an operator always supplies for each verb (the flat image /
# VM data), so a from_args call exercises just the DEFAULTED flag's absence.
_SYNC_REQUIRED = [
	"--image-name",
	"img",
	"--kernel-url",
	"https://k",
	"--kernel-filename",
	"vmlinux",
	"--kernel-sha256",
	"deadbeef",
	"--rootfs-url",
	"https://r",
	"--rootfs-filename",
	"root.ext4",
	"--rootfs-sha256",
	"cafebabe",
	"--default-disk-gb",
	"10",
]

_PROVISION_REQUIRED = [
	"--virtual-machine-name",
	"u",
	"--image-name",
	"img",
	"--kernel-filename",
	"vmlinux",
	"--rootfs-filename",
	"root.ext4",
	"--vcpus",
	"1",
	"--memory-mb",
	"512",
	"--disk-gb",
	"10",
	"--ssh-public-key",
	"key",
	"--mac-address",
	"06:00:00:00:00:01",
	"--tap-device",
	"atlas-x",
	"--virtual-machine-ipv6",
	"2a03::2",
	"--ipv4-host-cidr",
	"100.64.0.1/30",
	"--ipv4-guest-cidr",
	"100.64.0.2/30",
	"--ipv4-gateway",
	"100.64.0.1",
	"--atlas-fc-uid",
	"1000",
	"--atlas-netns",
	"ns",
	"--host-veth",
	"hv",
	"--namespace-veth",
	"nv",
	"--cgroup-arg",
	"memory.max=1",
]


class TestSyncImageGuestNetworkUnitDefault(unittest.TestCase):
	def setUp(self):
		self.module = _load("sync-image")
		self.cls = _inputs_cls(self.module, "sync-image")

	def test_parses_without_guest_network_unit(self):
		# The flag is no longer required — a hand run omits it and gets the staged path.
		inputs = self.cls.from_args(_SYNC_REQUIRED)
		self.assertEqual(inputs.guest_network_unit, self.module.STAGED_GUEST_NETWORK_UNIT)
		self.assertEqual(inputs.guest_network_unit, "/tmp/atlas/atlas-network.service")

	def test_explicit_value_overrides_default(self):
		# The controller (and an operator pointing at a real file) still wins.
		inputs = self.cls.from_args([*_SYNC_REQUIRED, "--guest-network-unit", "/real/unit.service"])
		self.assertEqual(inputs.guest_network_unit, "/real/unit.service")

	def test_help_documents_the_sidecar_default(self):
		help_text = self.cls.build_parser().format_help()
		self.assertIn("--guest-network-unit", help_text)
		self.assertIn("/tmp/atlas/atlas-network.service", help_text)


class TestProvisionResourceArgDefault(unittest.TestCase):
	def setUp(self):
		self.module = _load("provision-vm")
		self.cls = _inputs_cls(self.module, "provision-vm")

	def test_parses_without_resource_arg(self):
		# resource_arg is optional now (effectively the constant no-file=1024).
		inputs = self.cls.from_args(_PROVISION_REQUIRED)
		self.assertEqual(inputs.resource_arg, [])

	def test_jailer_launch_falls_back_to_the_constant(self):
		# An empty resource_arg must still bound descriptors — the launcher falls back
		# to DEFAULT_RESOURCE_ARGS rather than emitting no --resource-limit at all.
		inputs = self.cls.from_args(_PROVISION_REQUIRED)
		paths = self.module.VirtualMachinePaths(inputs.virtual_machine_name)
		launch = self.module._jailer_launch(inputs, paths)
		self.assertIn("--resource-limit no-file=1024", launch)

	def test_explicit_resource_arg_is_used_verbatim(self):
		inputs = self.cls.from_args([*_PROVISION_REQUIRED, "--resource-arg", "no-file=4096"])
		self.assertEqual(inputs.resource_arg, ["no-file=4096"])
		paths = self.module.VirtualMachinePaths(inputs.virtual_machine_name)
		launch = self.module._jailer_launch(inputs, paths)
		self.assertIn("--resource-limit no-file=4096", launch)
		# The default must not leak in alongside the explicit value.
		self.assertNotIn("no-file=1024", launch)

	def test_cgroup_arg_stays_required(self):
		# Dropping --cgroup-arg must fail loud (an empty cgroup set would un-bound the
		# VM); argparse exits 2 on a missing required flag.
		without_cgroup = _PROVISION_REQUIRED[:-2]  # strip the trailing --cgroup-arg pair
		err = io.StringIO()
		import contextlib

		with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
			self.cls.from_args(without_cgroup)
		self.assertEqual(raised.exception.code, 2)
		self.assertIn("--cgroup-arg", err.getvalue())

	def test_help_names_each_derived_flag_source_function(self):
		help_text = self.cls.build_parser().format_help()
		# Every derived flag names the atlas.networking function that computes it —
		# the break-glass recipe.
		self.assertIn("derive_mac", help_text)
		self.assertIn("derive_tap", help_text)
		self.assertIn("derive_uid", help_text)
		self.assertIn("derive_netns", help_text)
		self.assertIn("derive_veth_pair", help_text)
		self.assertIn("derive_ipv4_link", help_text)
		self.assertIn("allocate_ipv6", help_text)
		self.assertIn("cgroup_args", help_text)


class TestParserHelpIsWellFormed(unittest.TestCase):
	"""Sanity: both parsers build and format without raising (a bad metadata dict
	or a default-after-required slip would surface here)."""

	def test_both_parsers_format(self):
		for stem, command in (("sync-image", "sync-image"), ("provision-vm", "provision-vm")):
			module = _load(stem)
			cls = _inputs_cls(module, command)
			parser = cls.build_parser()
			self.assertIsInstance(parser, argparse.ArgumentParser)
			self.assertTrue(parser.format_help())


if __name__ == "__main__":
	unittest.main()
