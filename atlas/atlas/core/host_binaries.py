"""Build the Atlas host binaries and publish them as public site files."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import frappe
from frappe import _

# metal/go.mod needs 1.26.2 and services/wg-mesh/cli/go.mod needs 1.26.0.
GO_VERSION = "1.26.2"
GO_DOWNLOAD_URL = "https://go.dev/dl/go{version}.linux-{architecture}.tar.gz"
BUILD_TIMEOUT_SECONDS = 900
# Directories that hold the system headers a build includes.
INCLUDE_DIRECTORIES = (
	"/usr/include",
	f"/usr/include/{platform.machine()}-linux-gnu",
	"/usr/local/include",
)


@dataclass(frozen=True)
class HostBinary:
	"""One Go tool that Atlas builds from this repository and serves to hosts."""

	key: str
	label: str
	module_directory: str
	artifact: str
	settings_field: str
	source_hash_field: str
	required_commands: tuple[str, ...] = ("make",)
	required_headers: tuple[str, ...] = ()

	@property
	def file_name(self) -> str:
		return Path(self.artifact).name


HOST_BINARIES = (
	HostBinary(
		key="metald",
		label="metald",
		module_directory="metal",
		artifact="dist/metald-linux-amd64",
		settings_field="metald_binary_x86_64_file",
		source_hash_field="metald_source_hash",
	),
	HostBinary(
		key="wg-mesh",
		label="Atlas WG Mesh",
		module_directory="services/wg-mesh",
		artifact="dist/atlas-wg-mesh-linux-amd64",
		settings_field="wg_mesh_binary_x86_64_file",
		source_hash_field="wg_mesh_source_hash",
		# clang compiles the BPF object before the Go build. Its headers come
		# from libbpf-dev and linux-libc-dev on Debian, and from libbpf-devel
		# and kernel-headers on Fedora.
		required_commands=("make", "clang"),
		required_headers=("bpf/bpf_helpers.h", "bpf/bpf_endian.h", "linux/bpf.h", "asm/types.h"),
	),
)


def publish_host_binaries() -> None:
	"""Build every host binary and link it in Atlas Settings.

	This runs from `after_install` and `after_migrate`. A host that cannot build
	the binaries cannot provision a server, so a missing requirement fails here
	instead of at the first provision.
	"""
	pending_builds = []
	for binary in HOST_BINARIES:
		digest = source_digest(binary)
		if is_published(binary, digest):
			print(f"atlas: {binary.label} is current, skipped the build")
		else:
			pending_builds.append((binary, digest))

	if not pending_builds:
		return

	ensure_build_environment(tuple(binary for binary, _digest in pending_builds))
	go_binary = ensure_go_toolchain()

	for binary, digest in pending_builds:
		file_name = publish_host_binary(binary, go_binary, digest)
		print(f"atlas: published {binary.label} as File {file_name}")


def publish_host_binary(binary: HostBinary, go_binary: str, digest: str) -> str:
	"""Build one binary, attach it as a File, and link it in Atlas Settings."""
	artifact = build_host_binary(binary, go_binary)
	file_name = publish_binary_file(binary, artifact.read_bytes())
	frappe.db.set_single_value("Atlas Settings", binary.settings_field, file_name)
	frappe.db.set_single_value("Atlas Settings", binary.source_hash_field, digest)
	return file_name


def get_binary_download_url(file_name: str) -> str:
	"""Return the URL a host uses to download one built binary.

	`atlas_base_url` in the site configuration names an address that a host can
	reach, which the site's own URL is not during local development.
	"""
	file_url = frappe.db.get_value("File", file_name, "file_url")
	base_url = frappe.conf.atlas_base_url or frappe.utils.get_url()
	return f"{base_url.rstrip('/')}{file_url}"


def find_host_binary(key: str) -> HostBinary:
	"""Return the host binary that one command line names."""
	for binary in HOST_BINARIES:
		if binary.key == key:
			return binary
	raise KeyError(key)


def ensure_build_environment(binaries: tuple[HostBinary, ...] = HOST_BINARIES) -> None:
	"""Fail with every missing build requirement, not just the first one."""
	missing = {
		binary.label: requirements
		for binary in binaries
		if (requirements := missing_build_requirements(binary))
	}
	if not missing:
		return

	report = "; ".join(f"{label} needs {', '.join(items)}" for label, items in missing.items())
	frappe.throw(_("Atlas cannot build the host binaries: {0}.").format(report))


def is_published(binary: HostBinary, digest: str) -> bool:
	"""Report whether the linked File already matches the current source."""
	settings = frappe.get_cached_doc("Atlas Settings")
	file_name = settings.get(binary.settings_field)
	if not file_name or settings.get(binary.source_hash_field) != digest:
		return False
	return bool(frappe.db.exists("File", file_name))


def source_digest(binary: HostBinary) -> str:
	"""Return one digest of every source file that changes the build output."""
	module_path = repository_path() / binary.module_directory
	digest = hashlib.sha256()
	digest.update(GO_VERSION.encode())

	for path in sorted(module_path.rglob("*")):
		if not path.is_file() or is_build_output(path.relative_to(module_path)):
			continue
		digest.update(str(path.relative_to(module_path)).encode())
		digest.update(path.read_bytes())
	return digest.hexdigest()


def is_build_output(relative_path: Path) -> bool:
	"""Report whether a path is produced by the build and not an input."""
	return relative_path.parts[0] == "dist" or relative_path.suffix == ".o"


def repository_path() -> Path:
	"""Return the repository root that holds the Go modules."""
	return Path(frappe.get_app_path("atlas")).parent


def toolchain_path() -> Path:
	"""Return the directory that holds the downloaded Go toolchain."""
	return Path(frappe.get_site_path("private", "files", "toolchain"))


def ensure_go_toolchain() -> str:
	"""Return a Go binary that is new enough, downloading one when needed."""
	system_go = shutil.which("go")
	if system_go and has_required_go_version(system_go):
		return system_go

	downloaded = toolchain_path() / "go" / "bin" / "go"
	if downloaded.exists() and has_required_go_version(str(downloaded)):
		return str(downloaded)

	return download_go_toolchain()


def has_required_go_version(go_binary: str) -> bool:
	"""Report whether a Go binary satisfies the version the modules need."""
	try:
		output = subprocess.run(
			[go_binary, "version"], capture_output=True, text=True, timeout=60, check=True
		).stdout
	except OSError, subprocess.SubprocessError:
		return False

	# "go version go1.26.2 linux/amd64"
	for word in output.split():
		if word.startswith("go1."):
			return version_fields(word.removeprefix("go")) >= version_fields(GO_VERSION)
	return False


def version_fields(version: str) -> tuple[int, ...]:
	"""Return the comparable numeric fields of a Go version string."""
	match = re.match(r"\d+(?:\.\d+){1,2}", version)
	if not match:
		return ()
	return tuple(int(field) for field in match.group().split("."))


def download_go_toolchain() -> str:
	"""Download and extract the Go toolchain, then return its binary path."""
	architecture = "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64"
	url = GO_DOWNLOAD_URL.format(version=GO_VERSION, architecture=architecture)

	destination = toolchain_path()
	destination.mkdir(parents=True, exist_ok=True)
	archive = destination / f"go{GO_VERSION}.tar.gz"

	print(f"atlas: downloading the Go {GO_VERSION} toolchain")
	urllib.request.urlretrieve(url, archive)
	shutil.rmtree(destination / "go", ignore_errors=True)
	with tarfile.open(archive) as bundle:
		bundle.extractall(destination, filter="data")
	archive.unlink(missing_ok=True)

	go_binary = destination / "go" / "bin" / "go"
	if not go_binary.exists():
		raise FileNotFoundError(_("The Go archive did not contain {0}.").format(go_binary))
	return str(go_binary)


def missing_build_requirements(binary: HostBinary) -> list[str]:
	"""Return the commands and headers that one build needs and cannot find."""
	missing = [command for command in binary.required_commands if not shutil.which(command)]
	missing += [
		header
		for header in binary.required_headers
		if not any(Path(directory, header).exists() for directory in INCLUDE_DIRECTORIES)
	]
	return missing


def build_host_binary(binary: HostBinary, go_binary: str) -> Path:
	"""Build one host binary with `make build` and return its artifact path."""
	missing = missing_build_requirements(binary)
	if missing:
		raise FileNotFoundError(_("{0} cannot build without {1}.").format(binary.label, ", ".join(missing)))

	module_path = repository_path() / binary.module_directory
	environment = {
		"PATH": f"{Path(go_binary).parent}:{os.environ.get('PATH', '')}",
		"HOME": os.environ.get("HOME", ""),
		"GOFLAGS": "-buildvcs=false",
	}
	result = subprocess.run(
		["make", "build"],
		cwd=module_path,
		capture_output=True,
		text=True,
		timeout=BUILD_TIMEOUT_SECONDS,
		env=environment,
	)
	if result.returncode != 0:
		raise RuntimeError(f"make build failed in {module_path}: {result.stderr.strip()}")

	artifact = module_path / binary.artifact
	if not artifact.exists():
		raise FileNotFoundError(_("The build did not produce {0}.").format(artifact))
	return artifact


def publish_binary_file(binary: HostBinary, content: bytes) -> str:
	"""Insert a public File for one build and return its document name.

	Every build gets its own File, so the earlier binaries stay downloadable and
	a server that is still provisioning keeps the URL it started with.
	"""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": binary.file_name,
			"attached_to_doctype": "Atlas Settings",
			"attached_to_name": "Atlas Settings",
			"is_private": 0,
			"content": content,
		}
	).insert(ignore_permissions=True)

	print(f"atlas: {binary.label} sha256 {hashlib.sha256(content).hexdigest()}")
	return file_doc.name
