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
			False,
			purpose="base",
		)
		destroy.assert_called_once_with()


class TestBuildUbuntuRescueImageCommand(UnitTestCase):
	def test_rescue_builder_is_registered_with_frappe(self) -> None:
		self.assertIn(commands.build_ubuntu_rescue_image_command, commands.commands)

	def test_existing_build_configures_only_a_missing_default(self) -> None:
		for current, count in ((None, 1), ("selected-image", 1), (None, 0), (None, 2)):
			with (
				self.subTest(current=current, count=count),
				patch.object(commands, "is_image_available", return_value=True),
				patch.object(commands, "build_ubuntu_rescue_image") as build,
				patch.object(commands.frappe, "init"),
				patch.object(commands.frappe, "connect"),
				patch.object(commands.frappe, "destroy") as destroy,
				patch.object(commands.frappe, "get_doc") as get_settings,
				patch.object(commands, "get_available_ubuntu_images") as available,
				patch.object(commands.frappe, "db", create=True) as database,
			):
				settings = get_settings.return_value
				settings.rescue_virtual_machine_image = current
				image = MagicMock()
				image.name = "built-image"
				available.return_value = [image] * count
				result = CliRunner().invoke(
					commands.build_ubuntu_rescue_image_command,
					["--skip-existing", "--set-default-if-missing"],
					obj=command_context("test.local"),
				)
				self.assertEqual(result.exit_code, 2 if not current and count != 1 else 0, result.output)
				get_settings.assert_called_once_with("Atlas Settings", for_update=True)
				build.assert_not_called()
				destroy.assert_called_once()
				if not current and count != 1:
					database.set_single_value.assert_not_called()
					database.commit.assert_not_called()
				elif current:
					database.set_single_value.assert_not_called()
					available.assert_not_called()
				else:
					image.validate_rescue_image.assert_called_once()
					database.set_single_value.assert_called_once_with(
						"Atlas Settings", "rescue_virtual_machine_image", "built-image"
					)
					database.commit.assert_called_once()

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
