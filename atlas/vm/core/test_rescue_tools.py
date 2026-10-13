import importlib.util
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts/guest/rescue.py"
specification = importlib.util.spec_from_file_location("rescue_tools", SCRIPT)
rescue = importlib.util.module_from_spec(specification)
specification.loader.exec_module(rescue)


class TestRescueTools(TestCase):
	def test_existing_mount_must_match_requested_access(self):
		for read_only in (True, False):
			for options in ("ro,noload", "rw,relatime"):
				matches = read_only == options.startswith("ro")
				with (
					self.subTest(read_only=read_only, options=options),
					patch.object(rescue, "require_supported_filesystem"),
					patch.object(rescue, "require_writable"),
					patch.object(rescue, "original_disk_mounts", return_value=[str(rescue.ROOT)]),
					patch.object(rescue, "capture", return_value=options),
					patch.object(rescue, "run") as command,
					nullcontext()
					if matches
					else self.assertRaisesRegex(RuntimeError, "different access mode"),
				):
					rescue.mount_original(read_only)
				command.assert_not_called()

	def test_worker_records_completion_before_releasing_lock(self):
		with tempfile.TemporaryDirectory() as directory:
			state = Path(directory)
			open_path = Path.open

			@contextmanager
			def open_lock(path, *args, **kwargs):
				with open_path(path, *args, **kwargs) as stream:
					try:
						yield stream
					finally:
						if path.name == "operation.lock":
							self.assertEqual(json.loads((state / "result.json").read_text()), expected)

			for failure in (None, RuntimeError("repair failed")):
				expected = {"command": "fsck", "success": failure is None}
				if failure:
					expected["error"] = str(failure)
				(state / "result.json").unlink(missing_ok=True)
				with (
					self.subTest(failure=failure),
					patch.object(rescue, "STATE", state),
					patch.object(rescue.os, "geteuid", return_value=0),
					patch.object(rescue, "execute", side_effect=failure),
					patch.object(rescue.sys, "argv", ["atlas-rescue", "--worker", "fsck"]),
					patch.object(Path, "open", open_lock),
					self.assertRaises(SystemExit) if failure else nullcontext(),
				):
					rescue.main()

	def test_chroot_cleanup_attempts_every_mount_after_failure(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			unmounted = []

			def command(*args):
				if args[0] == "umount":
					unmounted.append(args[-1])
					if args[-1] == str(root / "run"):
						raise subprocess.CalledProcessError(1, args)

			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				patch.object(rescue, "confirm"),
				patch.object(rescue, "run", side_effect=command),
				patch.object(rescue.subprocess, "run"),
				self.assertRaisesRegex(RuntimeError, "Could not unmount"),
			):
				rescue.chroot()
			self.assertEqual(unmounted, [str(root / name) for name in ("run", "sys", "proc", "dev")])

	def test_export_refuses_parent_symlink_outside_original_disk(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "original"
			root.mkdir()
			outside = Path(directory) / "outside"
			(outside / "lib/mysql").mkdir(parents=True)
			(root / "var").symlink_to(outside, target_is_directory=True)
			destination = Path(directory) / "export"
			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				patch.object(rescue, "run") as run,
				self.assertRaisesRegex(RuntimeError, "escapes"),
			):
				rescue.export_recovery_files(str(destination))
			run.assert_not_called()
			self.assertFalse(json.loads((destination / "manifest.json").read_text())["complete"])

	def test_failed_export_remains_incomplete(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "original"
			(root / "var/lib/mysql").mkdir(parents=True)
			destination = Path(directory) / "export"
			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				patch.object(rescue, "run", side_effect=subprocess.CalledProcessError(23, "rsync")),
				self.assertRaises(subprocess.CalledProcessError),
			):
				rescue.export_recovery_files(str(destination))
			self.assertFalse(json.loads((destination / "manifest.json").read_text())["complete"])

	def test_inventory_reports_missing_components_without_config_secrets(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			site = root / "home/frappe/bench/sites/example.test"
			site.mkdir(parents=True)
			(site / "site_config.json").write_text(
				'{"db_password":"DO-NOT-REPORT","encryption_key":"SECRET"}'
			)
			(site / "private/files").mkdir(parents=True)
			(site / ".migrate").mkdir()
			(root / "var/lib/postgresql").mkdir(parents=True)
			report = rescue.discover_recovery_files(root)
			self.assertEqual(len(report["sites"]), 1)
			self.assertIn("public/files", report["sites"][0]["missing"])
			self.assertIn(".migrate", report["sites"][0]["present"])
			self.assertIn("var/lib/postgresql", report["paths"])
			self.assertNotIn("DO-NOT-REPORT", str(report))
			self.assertNotIn("SECRET", str(report))

	def test_unknown_and_read_only_access_refuse_mutation(self):
		for mode in ("unknown", "read_only"):
			with (
				patch.object(rescue, "access_mode", return_value=mode),
				self.assertRaisesRegex(RuntimeError, "Writes are disabled"),
			):
				rescue.require_writable()

	def test_host_enforced_read_only_overrides_metadata(self):
		with (
			patch.object(rescue, "access_mode", return_value="protected"),
			patch.object(rescue, "capture", return_value="1"),
			self.assertRaisesRegex(RuntimeError, "Writes are disabled"),
		):
			rescue.require_writable()

	def test_unsupported_layout_is_not_mounted(self):
		with (
			patch.object(
				rescue.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="crypto_LUKS")
			),
			patch.object(rescue, "run") as run,
			self.assertRaisesRegex(RuntimeError, "whole-disk ext4"),
		):
			rescue.mount_original(True)
		run.assert_not_called()

	def test_password_reset_keeps_secret_out_of_command_arguments(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "etc").mkdir()
			(root / "etc/passwd").write_text("alice:x:1000:1000::/home/alice:/bin/bash\n")
			(root / "etc/shadow").write_text("alice:oldhash:0:0:99999:7:::\n")
			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				patch.object(rescue, "confirm"),
				patch.object(rescue.getpass, "getpass", return_value="private-password"),
				patch.object(rescue, "run") as run,
			):
				rescue.reset_password("alice", False)
			self.assertNotIn("private-password", repr(run.call_args.args))
			self.assertEqual(run.call_args.kwargs["input"], "alice:private-password\n")

	def test_locked_account_requires_separate_unlock_choice(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "etc").mkdir()
			(root / "etc/passwd").write_text("alice:x:1000:1000::/home/alice:/bin/bash\n")
			(root / "etc/shadow").write_text("alice:!oldhash:0:0:99999:7:::\n")
			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				patch.object(rescue, "run") as run,
				self.assertRaisesRegex(RuntimeError, "locked"),
			):
				rescue.reset_password("alice", False)
			run.assert_not_called()

	def test_installed_symlink_cannot_escape_original_disk(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "original"
			root.mkdir()
			(root / "etc").symlink_to("/etc")
			with patch.object(rescue, "ROOT", root), self.assertRaisesRegex(RuntimeError, "escapes"):
				rescue.original_path("etc/passwd")

	def test_export_to_original_disk_is_refused(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				self.assertRaisesRegex(RuntimeError, "outside"),
			):
				rescue.export_recovery_files(str(root / "export"))

	def test_export_records_special_files_without_opening_them(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "original"
			root.mkdir()
			destination = Path(directory) / "export"

			def copy_files(*args):
				target = destination / "data/var/lib/mysql"
				target.mkdir(parents=True)
				(target / "table").write_bytes(b"database")
				os.mkfifo(target / "pipe")

			with (
				patch.object(rescue, "ROOT", root),
				patch.object(rescue, "mount_original"),
				patch.object(rescue, "discover_recovery_files", return_value={"paths": ["var/lib/mysql"]}),
				patch.object(rescue, "run", side_effect=copy_files),
			):
				rescue.export_recovery_files(str(destination))

			report = json.loads((destination / "manifest.json").read_text())
			self.assertTrue(report["complete"])
			self.assertEqual([entry["path"] for entry in report["files"]], ["data/var/lib/mysql/table"])
			self.assertEqual(report["special_files"][0]["path"], "data/var/lib/mysql/pipe")
			self.assertTrue(report["special_files"][0]["mode"].startswith("p"))

	def test_mount_inspection_distinguishes_absence_from_failure(self):
		for code, output, error in ((0, "/mnt/original\n", ""), (1, "", "")):
			with patch.object(
				rescue.subprocess,
				"run",
				return_value=SimpleNamespace(returncode=code, stdout=output, stderr=error),
			):
				self.assertEqual(rescue.original_disk_mounts(), output.strip().splitlines())
		for code, error in ((1, "cannot read mount table"), (2, ""), (-9, "")):
			with (
				self.subTest(code=code),
				patch.object(rescue, "require_supported_filesystem"),
				patch.object(
					rescue.subprocess,
					"run",
					return_value=SimpleNamespace(
						returncode=code,
						stdout="",
						stderr=error,
					),
				) as command,
				self.assertRaisesRegex(RuntimeError, "Cannot inspect"),
			):
				rescue.execute(SimpleNamespace(command="fsck", repair=True, worker=True))
			self.assertEqual(command.call_count, 1, "Failed inspection must not start filesystem repair")
