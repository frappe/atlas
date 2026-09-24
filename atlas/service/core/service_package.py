from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import frappe
from frappe import _

from atlas.atlas.core.artifacts import get_download_url, publish_public_file


@dataclass(frozen=True)
class ServicePackage:
	"""A service component that Atlas archives, publishes, and installs on a virtual machine."""

	name: str
	label: str
	setup_script: str

	@property
	def settings_file_field(self) -> str:
		"""Return the Atlas Settings field that links the published File."""
		return f"{self.name.replace('-', '_')}_package_file"

	@property
	def settings_hash_field(self) -> str:
		"""Return the Atlas Settings field that holds the archive digest."""
		return f"{self.name.replace('-', '_')}_package_hash"

	@property
	def component_path(self) -> Path:
		"""Return the component directory in the repository."""
		return Path(frappe.get_app_path("atlas")).parent / "services" / self.name

	def publish(self) -> None:
		"""Publish the package when its sources changed."""
		archive = self.build_archive()
		digest = hashlib.sha256(archive).hexdigest()
		if self.is_published(digest):
			print(f"atlas: {self.label} is current, skipped the build")
			return

		file_name = self.publish_archive(archive, digest)
		print(f"atlas: published {self.label} as File {file_name}")

	def publish_archive(self, archive: bytes, digest: str) -> str:
		"""Publish the archive and link it from Atlas Settings."""
		file_name = publish_public_file(f"{self.name}.tar", self.label, archive)
		frappe.db.set_single_value("Atlas Settings", self.settings_file_field, file_name)
		frappe.db.set_single_value("Atlas Settings", self.settings_hash_field, digest)
		return file_name

	def is_published(self, digest: str) -> bool:
		"""Report whether the linked archive has this digest."""
		settings = frappe.get_cached_doc("Atlas Settings")
		file_name = settings.get(self.settings_file_field)
		if not file_name or settings.get(self.settings_hash_field) != digest:
			return False

		return bool(frappe.db.exists("File", file_name))

	def get_install_environment(self) -> dict[str, str]:
		"""Return the install-service-package.sh variables for the published archive."""
		settings = frappe.get_single("Atlas Settings")
		file_name = settings.get(self.settings_file_field)
		digest = settings.get(self.settings_hash_field)
		if not file_name or not digest:
			frappe.throw(_("Atlas Settings holds no {0}. Run build-service-packages.").format(self.label))

		return {
			"PACKAGE_NAME": self.name,
			"PACKAGE_SETUP_SCRIPT": self.setup_script,
			"PACKAGE_DOWNLOAD_URL": get_download_url(file_name),
			"PACKAGE_SHA256": digest,
		}

	def build_archive(self) -> bytes:
		"""Return a reproducible archive of regular component files."""
		component = self.component_path
		buffer = io.BytesIO()
		with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
			for path in self.get_source_paths():
				content = path.read_bytes()
				info = tarfile.TarInfo(f"{self.name}/{path.relative_to(component)}")
				info.size = len(content)
				info.mtime = 0
				info.mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
				info.uid = info.gid = 0
				info.uname = info.gname = "root"
				archive.addfile(info, io.BytesIO(content))

		return buffer.getvalue()

	def get_source_paths(self) -> list[Path]:
		"""Return unignored component files in a stable order."""
		component = self.component_path
		result = subprocess.run(
			["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
			cwd=component,
			capture_output=True,
			check=False,
		)
		if result.returncode != 0:
			frappe.throw(
				_("Cannot list the {0} sources: {1}").format(
					self.label, result.stderr.decode(errors="replace")
				)
			)

		names = [name for name in result.stdout.decode().split("\0") if name]
		if any("\n" in name or "\r" in name for name in names):
			frappe.throw(_("{0} source names must not contain line breaks.").format(self.label))

		paths = [component / name for name in names]
		return sorted(path for path in paths if path.is_file() and not path.is_symlink())


HTTP_PROXY_PACKAGE = ServicePackage("http-proxy", "HTTP proxy package", "nginx/setup.sh")
IPV6_ROUTER_PACKAGE = ServicePackage("ipv6-router", "IPv6 router package", "setup.sh")
WG_GATEWAY_PACKAGE = ServicePackage("wg-gateway", "WireGuard gateway package", "setup.sh")
SERVICE_PACKAGES = (HTTP_PROXY_PACKAGE, IPV6_ROUTER_PACKAGE, WG_GATEWAY_PACKAGE)


def publish_service_packages() -> None:
	"""Publish each service package whose sources changed."""
	for package in SERVICE_PACKAGES:
		package.publish()
