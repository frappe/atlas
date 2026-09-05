from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from atlas.atlas.core import host_binaries
from atlas.atlas.core.host_binaries import HOST_BINARIES, version_fields


class TestHostBinaries(UnitTestCase):
	def test_version_fields_compare_numerically(self) -> None:
		"""A string compare would rank 1.9 above 1.26."""
		self.assertGreater(version_fields("1.26.2"), version_fields("1.9.0"))
		self.assertGreater(version_fields("1.26.2"), version_fields("1.26.1"))
		self.assertEqual(version_fields("1.26.2"), version_fields("1.26.2"))

	def test_version_fields_ignore_release_suffixes(self) -> None:
		self.assertEqual(version_fields("1.26rc1"), (1, 26))
		self.assertEqual(version_fields("invalid"), ())

	def test_file_name_comes_from_the_artifact(self) -> None:
		self.assertEqual(HOST_BINARIES[0].file_name, "metald-linux-amd64")

	def test_binary_download_url_prefers_the_configured_base(self) -> None:
		"""A host cannot reach the site URL of a local bench."""
		with (
			patch.object(host_binaries.frappe.db, "get_value", return_value="/files/metald-linux-amd64"),
			patch.object(host_binaries.frappe, "conf", SimpleNamespace(atlas_base_url="https://atlas.test/")),
		):
			url = host_binaries.get_binary_download_url("metald-file")

		self.assertEqual(url, "https://atlas.test/files/metald-linux-amd64")

	def test_binary_download_url_falls_back_to_the_site_url(self) -> None:
		with (
			patch.object(host_binaries.frappe.db, "get_value", return_value="/files/metald-linux-amd64"),
			patch.object(host_binaries.frappe, "conf", SimpleNamespace(atlas_base_url=None)),
			patch.object(host_binaries.frappe.utils, "get_url", return_value="http://atlas.localhost:8000"),
		):
			url = host_binaries.get_binary_download_url("metald-file")

		self.assertEqual(url, "http://atlas.localhost:8000/files/metald-linux-amd64")

	def test_find_host_binary_reads_the_command_line_key(self) -> None:
		self.assertEqual(host_binaries.find_host_binary("metald").label, "metald")
		self.assertEqual(host_binaries.find_host_binary("wg-mesh").label, "Atlas WG Mesh")
		with self.assertRaises(KeyError):
			host_binaries.find_host_binary("missing")

	def test_every_binary_names_distinct_settings_fields(self) -> None:
		fields = [binary.settings_field for binary in HOST_BINARIES]
		fields += [binary.source_hash_field for binary in HOST_BINARIES]
		fields += [binary.key for binary in HOST_BINARIES]

		self.assertEqual(len(fields), len(set(fields)))

	def test_build_output_is_not_a_source_input(self) -> None:
		"""A rebuild must not change the digest that decides whether to rebuild."""
		self.assertTrue(host_binaries.is_build_output(Path("dist/metald-linux-amd64")))
		self.assertTrue(host_binaries.is_build_output(Path("cli/atlas-wg-mesh.bpf.o")))
		self.assertFalse(host_binaries.is_build_output(Path("cli/main.go")))
		self.assertFalse(host_binaries.is_build_output(Path("bpf/bpf.c")))

	def test_ensure_build_environment_reports_every_missing_tool(self) -> None:
		with (
			patch.object(host_binaries.shutil, "which", return_value=None),
			patch.object(host_binaries, "INCLUDE_DIRECTORIES", ("/nonexistent",)),
			patch.object(host_binaries.frappe, "throw", side_effect=ValueError) as throw,
			self.assertRaises(ValueError),
		):
			host_binaries.ensure_build_environment()

		reported = throw.call_args.args[0]
		self.assertIn("make", reported)
		self.assertIn("clang", reported)
		self.assertIn("bpf/bpf_helpers.h", reported)

	def test_ensure_build_environment_passes_when_everything_is_present(self) -> None:
		with (
			patch.object(host_binaries.shutil, "which", return_value="/usr/bin/tool"),
			patch.object(host_binaries.Path, "exists", return_value=True),
			patch.object(host_binaries.frappe, "throw", side_effect=ValueError) as throw,
		):
			host_binaries.ensure_build_environment()

		throw.assert_not_called()

	def test_publish_skips_build_setup_when_every_binary_is_current(self) -> None:
		with (
			patch.object(host_binaries, "source_digest", return_value="current"),
			patch.object(host_binaries, "is_published", return_value=True),
			patch.object(host_binaries, "ensure_build_environment") as ensure_build_environment,
			patch.object(host_binaries, "ensure_go_toolchain") as ensure_go_toolchain,
		):
			host_binaries.publish_host_binaries()

		ensure_build_environment.assert_not_called()
		ensure_go_toolchain.assert_not_called()

	def test_has_required_go_version_reads_the_version_word(self) -> None:
		for output, expected in (
			("go version go1.26.2 linux/amd64", True),
			("go version go1.27.0 linux/amd64", True),
			("go version go1.25.9 linux/amd64", False),
			("go version weird output", False),
		):
			with patch.object(host_binaries.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
				self.assertEqual(host_binaries.has_required_go_version("go"), expected, output)

	def test_wg_mesh_needs_clang_and_its_headers(self) -> None:
		"""clang compiles the BPF object, so name what is missing before make runs."""
		mesh = next(binary for binary in HOST_BINARIES if "clang" in binary.required_commands)

		with (
			patch.object(host_binaries.shutil, "which", return_value=None),
			patch.object(host_binaries.subprocess, "run") as run,
			self.assertRaises(FileNotFoundError) as caught,
		):
			host_binaries.build_host_binary(mesh, "go")

		self.assertIn("clang", str(caught.exception))
		run.assert_not_called()

	def test_missing_build_requirements_reports_absent_headers(self) -> None:
		mesh = next(binary for binary in HOST_BINARIES if binary.required_headers)

		with (
			patch.object(host_binaries.shutil, "which", return_value="/usr/bin/clang"),
			patch.object(host_binaries, "INCLUDE_DIRECTORIES", ("/nonexistent",)),
		):
			missing = host_binaries.missing_build_requirements(mesh)

		self.assertEqual(missing, list(mesh.required_headers))
