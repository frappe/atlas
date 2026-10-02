from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.service.core.service_package import HTTP_PROXY_PACKAGE, ServicePackage


class TestServicePackage(UnitTestCase):
	def build_component(self) -> Path:
		"""Create a component tree in a repository, with ignored and tracked files."""
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		component = Path(directory.name) / "http-proxy"
		component.mkdir()

		shared = Path(directory.name) / "control-cluster"
		shared.mkdir()
		(shared / "pyproject.toml").write_text("[project]\n")
		self.run_git(shared, "init", "--quiet")

		(component / "nginx").mkdir()
		(component / "control").mkdir()
		(component / ".gitignore").write_text(".venv/\n.mypy_cache/\n*.ext4\n")
		(component / "nginx" / "setup.sh").write_text("#!/usr/bin/env bash\n")
		(component / "nginx" / "setup.sh").chmod(0o755)
		(component / "control" / "main.py").write_text("app = None\n")

		(component / ".venv" / "bin").mkdir(parents=True)
		(component / ".venv" / "bin" / "python").write_text("junk\n")
		(component / ".mypy_cache").mkdir()
		(component / ".mypy_cache" / "cache.db").write_text("junk\n")
		(component / "proxy.ext4").write_text("junk\n")
		(component / "escape").symlink_to("/etc/passwd")

		self.run_git(component, "init", "--quiet")
		self.run_git(component, "add", ".gitignore", "nginx/setup.sh")

		patched = patch.object(
			ServicePackage, "component_path", new_callable=PropertyMock, return_value=component
		)
		self.addCleanup(patched.stop)
		patched.start()
		return component

	def run_git(self, component: Path, *arguments: str) -> None:
		subprocess.run(["git", *arguments], cwd=component, check=True, capture_output=True)

	def archive_members(self) -> list[tarfile.TarInfo]:
		with tarfile.open(fileobj=io.BytesIO(HTTP_PROXY_PACKAGE.build_archive())) as archive:
			return sorted(archive.getmembers(), key=lambda member: member.name)

	def archived_names(self) -> list[str]:
		return [member.name for member in self.archive_members()]

	# A tracked file and an untracked file that no rule ignores both belong.
	def test_the_archive_holds_the_sources_below_one_directory(self) -> None:
		self.build_component()

		self.assertEqual(
			self.archived_names(),
			[
				"http-proxy/.gitignore",
				"http-proxy/control-cluster/pyproject.toml",
				"http-proxy/control/main.py",
				"http-proxy/nginx/setup.sh",
			],
		)

	def test_the_archive_follows_gitignore(self) -> None:
		self.build_component()

		names = self.archived_names()

		self.assertFalse([name for name in names if ".venv" in name or ".mypy_cache" in name])
		self.assertNotIn("http-proxy/proxy.ext4", names)

	# A link could point the host at a file outside the unpack directory.
	def test_the_archive_leaves_out_a_symbolic_link(self) -> None:
		self.build_component()

		for member in self.archive_members():
			self.assertTrue(member.isfile(), member.name)

	def test_a_source_name_with_a_line_break_is_refused(self) -> None:
		component = self.build_component()
		(component / "unsafe\nname").write_text("content\n")

		with self.assertRaisesRegex(frappe.ValidationError, "must not contain line breaks"):
			HTTP_PROXY_PACKAGE.build_archive()

	def test_every_name_stays_below_the_archive_root(self) -> None:
		self.build_component()

		for name in self.archived_names():
			self.assertTrue(name.startswith("http-proxy/"), name)
			self.assertNotIn("..", Path(name).parts)

	def test_the_entries_carry_no_local_timestamp_or_owner(self) -> None:
		self.build_component()

		for member in self.archive_members():
			self.assertEqual(member.mtime, 0, member.name)
			self.assertEqual((member.uid, member.gid), (0, 0), member.name)
			self.assertEqual(member.uname, "root", member.name)

	def test_an_executable_file_keeps_its_mode(self) -> None:
		self.build_component()

		modes = {member.name: member.mode for member in self.archive_members()}

		self.assertEqual(modes["http-proxy/nginx/setup.sh"], 0o755)
		self.assertEqual(modes["http-proxy/control/main.py"], 0o644)

	# An unchanged tree must not publish a second File on every migrate.
	def test_an_unchanged_tree_gives_the_same_bytes(self) -> None:
		self.build_component()

		self.assertEqual(HTTP_PROXY_PACKAGE.build_archive(), HTTP_PROXY_PACKAGE.build_archive())

	def test_the_digest_follows_a_changed_source_file(self) -> None:
		component = self.build_component()
		before = hashlib.sha256(HTTP_PROXY_PACKAGE.build_archive()).hexdigest()

		(component / "nginx" / "setup.sh").write_text("#!/usr/bin/env bash\necho changed\n")

		self.assertNotEqual(before, hashlib.sha256(HTTP_PROXY_PACKAGE.build_archive()).hexdigest())

	def test_the_digest_ignores_an_ignored_file(self) -> None:
		component = self.build_component()
		before = hashlib.sha256(HTTP_PROXY_PACKAGE.build_archive()).hexdigest()

		(component / "proxy.ext4").write_text("a different build output\n")

		self.assertEqual(before, hashlib.sha256(HTTP_PROXY_PACKAGE.build_archive()).hexdigest())

	def test_the_digest_follows_a_changed_shared_component(self) -> None:
		component = self.build_component()
		before = hashlib.sha256(HTTP_PROXY_PACKAGE.build_archive()).hexdigest()

		(component.parent / "control-cluster" / "pyproject.toml").write_text("[project]\nname = 'changed'\n")

		self.assertNotEqual(before, hashlib.sha256(HTTP_PROXY_PACKAGE.build_archive()).hexdigest())
