#!/usr/bin/env python3
"""Install Pilot, set up Atlas, and build the guest images. Runs in the VM as root."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("ATLAS_VM_CONFIG", "/root/atlas-vm.toml"))
SETUP_GRANT = "/etc/sudoers.d/{user}-atlas-setup"
IMAGE_BUILDER_GRANT = "/etc/sudoers.d/{user}-atlas-image-builder"


class SetupError(Exception):
	"""An error the operator must act on."""


def step(message: str) -> None:
	print(f"==> {message}", flush=True)


def run(command: list[str], **keywords) -> subprocess.CompletedProcess:
	return subprocess.run(command, check=True, **keywords)


@dataclass
class Configuration:
	"""What the VM needs to build the bench, the site, and the images."""

	site: str
	password: str
	admin_domain: str
	bench_name: str = "atlas"
	bench_user: str = "frappe"
	pilot_branch: str = "develop"
	letsencrypt_email: str = ""
	atlas_repository: str = "https://github.com/frappe/atlas"
	atlas_branch: str = "develop"
	python_version: str = "3.14"
	images: list[tuple[str, str, bool]] = field(default_factory=list)

	@classmethod
	def read(cls, path: Path) -> Configuration:
		if not path.is_file():
			raise SetupError(f"no configuration at {path}")
		document = tomllib.loads(path.read_text())
		pilot = document.get("pilot", {})
		atlas = document.get("atlas", {})

		site = pilot.get("site", "")
		password = pilot.get("password", "")
		if not site or not password:
			raise SetupError(f"{path} needs pilot.site and pilot.password")

		images = [
			(str(image.get("version", "24.04")), image.get("architecture", "amd64"), bool(image.get("minimal")))
			for image in document.get("image", [{"version": "24.04"}])
		]
		return cls(
			site=site,
			password=password,
			admin_domain=pilot.get("admin_domain", f"admin.{site}"),
			bench_user=pilot.get("user", "frappe"),
			pilot_branch=pilot.get("branch", "develop"),
			letsencrypt_email=pilot.get("letsencrypt_email", ""),
			atlas_repository=atlas.get("repository", "https://github.com/frappe/atlas"),
			atlas_branch=atlas.get("branch", "develop"),
			python_version=str(document.get("vm", {}).get("python_version", "3.14")),
			images=images,
		)

	@property
	def pilot_install_url(self) -> str:
		return f"https://raw.githubusercontent.com/frappe/pilot/{self.pilot_branch}/install.sh"

	@property
	def pilot_home(self) -> Path:
		return Path(f"/home/{self.bench_user}/pilot")

	@property
	def bench_path(self) -> Path:
		return self.pilot_home / "benches" / self.bench_name

	@property
	def image_builder_path(self) -> Path:
		return self.bench_path / "apps/atlas/atlas/vm/scripts/build_ubuntu_server_image.sh"


class Setup:
	"""Every stage the VM runs, in order."""

	def __init__(self, configuration: Configuration) -> None:
		self.configuration = configuration
		self.setup_grant = SETUP_GRANT.format(user=configuration.bench_user)
		self.image_builder_grant = IMAGE_BUILDER_GRANT.format(user=configuration.bench_user)

	def as_bench(self, command: str) -> None:
		run(["su", "-", self.configuration.bench_user, "-c", command])

	def bench_output(self, command: str) -> str:
		result = run(
			["su", "-", self.configuration.bench_user, "-c", command], capture_output=True, text=True
		)
		return result.stdout.strip()

	def pilot(self, arguments: str) -> None:
		self.as_bench(f"pilot {arguments} --bench {self.configuration.bench_name}")

	def install_grant(self, path: str, content: str) -> None:
		with tempfile.NamedTemporaryFile("w", delete=False) as staged:
			staged.write(content + "\n")
		if subprocess.run(["visudo", "-cf", staged.name], stdout=subprocess.DEVNULL).returncode != 0:
			raise SetupError(f"the sudo grant for {path} is malformed")
		run(["install", "-m", "440", staged.name, path])
		os.unlink(staged.name)

	def install_packages(self) -> None:
		step("stage 1: packages")
		environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
		run(["apt-get", "update", "-qq"], env=environment)
		run(
			["apt-get", "install", "-y", "-qq", "wget", "curl", "git", "make", "clang",
			 "libbpf-dev", "linux-libc-dev", "squashfs-tools", "zstd", "e2fsprogs"],
			env=environment,
		)

	def install_host_dependencies(self) -> None:
		step(f"stage 2: host dependencies and the {self.configuration.bench_user} user")
		run(["wget", "-qO", "/root/pilot-install.sh", self.configuration.pilot_install_url])
		run(["sh", "/root/pilot-install.sh", "--user", self.configuration.bench_user])

	def grant_setup_sudo(self) -> None:
		# Pilot calls sudo with no terminal. The grant lives only as long as this run.
		step(f"stage 3: sudo for {self.configuration.bench_user} during this run")
		self.install_grant(self.setup_grant, f"{self.configuration.bench_user} ALL=(ALL) NOPASSWD: ALL")

	def install_python(self) -> None:
		# Ubuntu 24.04 ships Python 3.12, which cannot parse the Atlas sources. Keep
		# the system python3 for apt and give the bench user its own interpreter.
		version = self.configuration.python_version
		step(f"stage 4: python {version} for {self.configuration.bench_user}")
		self.as_bench("command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh")
		self.as_bench(f"uv python install {version}")
		self.as_bench(f'mkdir -p ~/.local/bin && ln -sf "$(uv python find {version})" ~/.local/bin/python3')
		reported = self.bench_output("python3 --version")
		if version not in reported:
			raise SetupError(
				f"{self.configuration.bench_user} runs {reported};"
				f" Pilot needs Python {version} to read the Atlas sources"
			)
		step(f"{self.configuration.bench_user} runs {reported}")

	def install_pilot(self) -> None:
		step(f"stage 5: Pilot from branch {self.configuration.pilot_branch}")
		self.as_bench(
			f"PILOT_DEV=1 PILOT_BRANCH={self.configuration.pilot_branch}"
			f" bash -c 'curl -fsSL {self.configuration.pilot_install_url} | bash'"
		)

	def create_bench(self) -> None:
		# Guard each step on what it produces, so a stopped run continues here.
		configuration = self.configuration
		step(f"stage 6: bench {configuration.bench_name}")
		password = shlex.quote(configuration.password)
		if not (configuration.bench_path / "bench.toml").is_file():
			self.as_bench(
				f"pilot new {configuration.bench_name} --admin-password {password}"
				f" --admin-domain {configuration.admin_domain} --database mariadb"
			)
		if not os.access(configuration.bench_path / "env/bin/python", os.X_OK):
			self.pilot("init")

	def create_site(self) -> None:
		configuration = self.configuration
		step(f"stage 7: site {configuration.site}")
		site_config = configuration.bench_path / "sites" / configuration.site / "site_config.json"
		if not site_config.is_file():
			self.pilot(f"new-site {configuration.site} --admin-password {shlex.quote(configuration.password)}")

	def get_atlas(self) -> None:
		# Pilot skips an app that is already there, so this stage is safe to repeat.
		configuration = self.configuration
		step(f"stage 8: the atlas app from {configuration.atlas_branch}")
		self.pilot(f"get-app {configuration.atlas_repository} --branch {configuration.atlas_branch}")

	def setup_production(self) -> None:
		configuration = self.configuration
		step("stage 9: production")
		arguments = f"setup production --admin-domain {configuration.admin_domain}"
		if configuration.letsencrypt_email:
			arguments += f" --tls --letsencrypt-email {configuration.letsencrypt_email}"
		self.pilot(arguments)

		# Production setup does not enable nginx, and the VM must serve after a reboot.
		run(["systemctl", "enable", "nginx"])
		run(["systemctl", "reload", "nginx"])
		self.pilot("build --force")

	def install_atlas(self) -> None:
		# Production runs Redis and the workers, which an install hook can reach.
		configuration = self.configuration
		step(f"stage 10: install atlas on {configuration.site}")
		self.pilot(f"install-app {configuration.site} atlas")

	def grant_image_builder_sudo(self) -> None:
		# The image builder runs its root file system build through sudo on every build.
		step("stage 11: sudo grant for the image builder")
		self.install_grant(
			self.image_builder_grant,
			f"{self.configuration.bench_user} ALL=(ALL) NOPASSWD: {self.configuration.image_builder_path} *",
		)

	def build_images(self) -> None:
		# Bootstrap has no object storage credentials yet, so every image is a site file.
		configuration = self.configuration
		step(f"stage 12: guest images ({len(configuration.images)})")
		for version, architecture, minimal in configuration.images:
			step(f"image {version} {architecture} {'minimal' if minimal else 'server'}")
			arguments = f"--version {version} --architecture {architecture} --storage site-file"
			if minimal:
				arguments += " --minimal"
			self.as_bench(
				f"pilot --bench {configuration.bench_name} --site {configuration.site}"
				f" build-ubuntu-base-image {arguments}"
			)

	def run(self) -> None:
		self.install_packages()
		self.install_host_dependencies()
		self.grant_setup_sudo()
		try:
			self.install_python()
			self.install_pilot()
			self.create_bench()
			self.create_site()
			self.get_atlas()
			self.setup_production()
			self.install_atlas()
		finally:
			Path(self.setup_grant).unlink(missing_ok=True)
		self.grant_image_builder_sudo()
		self.build_images()
		step(f"ATLAS_SITE_READY {self.configuration.site}")


def remove_pilot_installation(configuration: Configuration) -> None:
	"""The user units outlive the directory, so stop and remove them first."""
	print(f"This deletes {configuration.pilot_home} with every bench, site, and database in it.")
	print("Certificates in /etc/letsencrypt stay, so a new setup reuses them.")
	if input("Type y to continue: ").strip() != "y":
		raise SetupError("stopped")

	user = configuration.bench_user
	units = f"'{configuration.bench_name}.target' '{configuration.bench_name}-*' 'pilot-*'"
	unit_directory = Path(f"/home/{user}/.config/systemd/user")
	identifier = subprocess.run(["id", "-u", user], capture_output=True, text=True)
	if identifier.returncode == 0:
		user_id = identifier.stdout.strip()
		environment = (
			f"XDG_RUNTIME_DIR=/run/user/{user_id}"
			f" DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{user_id}/bus"
		)
		for action in (f"stop {units}", f"disable {units}", "daemon-reload", "reset-failed"):
			subprocess.run(
				["su", "-", user, "-c", f"{environment} systemctl --user {action}"],
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
			)
		for pattern in (f"{configuration.bench_name}*", "pilot-*"):
			for unit in list(unit_directory.glob(pattern)) + list(
				(unit_directory / "default.target.wants").glob(pattern)
			):
				unit.unlink(missing_ok=True)

	shutil.rmtree(configuration.pilot_home, ignore_errors=True)
	for grant in Path("/etc/sudoers.d").glob(f"{user}-*"):
		grant.unlink(missing_ok=True)


def main() -> int:
	parser = argparse.ArgumentParser(prog="setup.py", description=__doc__)
	parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="configuration file to read")
	parser.add_argument("--force", action="store_true", help="delete the Pilot installation first")
	arguments = parser.parse_args()

	try:
		if os.geteuid() != 0:
			raise SetupError("run this as root")
		configuration = Configuration.read(arguments.config)
		if arguments.force:
			remove_pilot_installation(configuration)
		Setup(configuration).run()
	except SetupError as error:
		print(f"error: {error}", file=sys.stderr)
		return 1
	except subprocess.CalledProcessError as error:
		print(f"error: command failed: {shlex.join(str(part) for part in error.cmd)}", file=sys.stderr)
		return error.returncode
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
