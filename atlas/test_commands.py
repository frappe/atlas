from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from frappe.tests import UnitTestCase
from frappe.utils.bench_helper import CliCtxObj

from atlas import commands


def command_context(*sites: str) -> CliCtxObj:
	return CliCtxObj(sites=list(sites), force=False, profile=False, verbose=False)


class TestConfigureAtlasCommand(UnitTestCase):
	def test_configuration_comes_from_stdin_and_site_is_destroyed(self) -> None:
		configuration = MagicMock()
		with (
			patch.object(commands.AtlasSetupConfiguration, "from_dict", return_value=configuration) as load,
			patch.object(commands, "AtlasSetup") as setup,
			patch.object(commands.frappe, "init") as initialize,
			patch.object(commands.frappe, "connect") as connect,
			patch.object(commands.frappe, "destroy") as destroy,
		):
			result = CliRunner().invoke(
				commands.configure_atlas,
				input='{"region_name": "par-1"}',
				obj=command_context("test.local"),
			)

		self.assertEqual(result.exit_code, 0, result.output)
		load.assert_called_once_with({"region_name": "par-1"})
		initialize.assert_called_once_with("test.local")
		connect.assert_called_once_with()
		setup.assert_called_once_with(configuration)
		setup.return_value.run.assert_called_once_with()
		destroy.assert_called_once_with()

	def test_site_is_destroyed_after_setup_failure(self) -> None:
		with (
			patch.object(commands.AtlasSetupConfiguration, "from_dict", return_value=MagicMock()),
			patch.object(commands, "AtlasSetup") as setup,
			patch.object(commands.frappe, "init"),
			patch.object(commands.frappe, "connect"),
			patch.object(commands.frappe, "destroy") as destroy,
		):
			setup.return_value.run.side_effect = RuntimeError("setup failed")
			result = CliRunner().invoke(
				commands.configure_atlas,
				input="{}",
				obj=command_context("test.local"),
			)

		self.assertIsInstance(result.exception, RuntimeError)
		destroy.assert_called_once_with()


class TestBuildUbuntuBaseImageCommand(UnitTestCase):
	def test_skip_existing_publishes_only_to_missing_sites(self) -> None:
		image_path = Path("/tmp/image.raw")
		kernel_path = Path("/tmp/vmlinux")
		with (
			patch.object(
				commands,
				"is_image_available",
				side_effect=lambda site, *_: site == "existing.local",
			),
			patch.object(commands, "build_ubuntu_image", return_value=(image_path, kernel_path)) as build,
			patch.object(commands, "publish_ubuntu_image") as publish,
			patch.object(commands.frappe, "init") as initialize,
			patch.object(commands.frappe, "connect"),
			patch.object(commands.frappe.db, "commit"),
			patch.object(commands.frappe, "destroy") as destroy,
		):
			result = CliRunner().invoke(
				commands.build_ubuntu_base_image,
				["--version", "24.04", "--storage", "site-file", "--skip-existing"],
				obj=command_context("existing.local", "missing.local"),
			)

		self.assertEqual(result.exit_code, 0, result.output)
		build.assert_called_once_with("24.04", "amd64", False, Path("dist"))
		initialize.assert_called_once_with("missing.local")
		publish.assert_called_once_with(
			"ubuntu-24.04",
			"24.04",
			"amd64",
			image_path,
			kernel_path,
			"Site File",
			purpose="base",
		)
		destroy.assert_called_once_with()


class TestBuildUbuntuRescueImageCommand(UnitTestCase):
	def test_rescue_command_builds_once_and_publishes_with_its_own_purpose(self) -> None:
		with (
			patch.object(commands, "is_image_available", side_effect=[True, False]) as available,
			patch.object(
				commands, "build_ubuntu_rescue_image", return_value=(Path("rootfs"), Path("kernel"))
			) as build,
			patch.object(commands, "_publish_image_to_sites") as publish,
		):
			result = CliRunner().invoke(
				commands.build_ubuntu_rescue_image_command,
				["--skip-existing"],
				obj=command_context("existing.local", "new.local"),
			)
		self.assertEqual(result.exit_code, 0, result.output)
		self.assertEqual(available.call_args.args, ("new.local", "ubuntu-24.04-rescue", "amd64", "rescue"))
		build.assert_called_once_with(Path("dist"))
		publish.assert_called_once_with(
			["new.local"],
			"ubuntu-24.04-rescue",
			"24.04",
			"amd64",
			Path("rootfs"),
			Path("kernel"),
			"object-storage",
			"rescue",
		)

	def test_failed_rescue_build_does_not_publish(self) -> None:
		with (
			patch.object(commands, "build_ubuntu_rescue_image", side_effect=RuntimeError("build failed")),
			patch.object(commands, "_publish_image_to_sites") as publish,
		):
			result = CliRunner().invoke(
				commands.build_ubuntu_rescue_image_command, [], obj=command_context("test.local")
			)
		self.assertIsInstance(result.exception, RuntimeError)
		publish.assert_not_called()
