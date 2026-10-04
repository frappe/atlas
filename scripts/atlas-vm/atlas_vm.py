#!/usr/bin/env python3
"""Manage the Atlas VM on one bare metal host. One VM per host."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import time
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "atlas-pilot-vm.service"
UNIT_FILE = Path("/etc/systemd/system") / SERVICE_NAME
INSTALLED_PATH = Path("/usr/local/bin/atlas-vm")
BASE_DIRECTORY = Path(os.environ.get("BASE_DIRECTORY", "/var/lib/atlas-vm"))
CONFIG_FILE = BASE_DIRECTORY / "atlas-vm.toml"

# Ubuntu 24.04 server cloud image, the same release the guest image builder uses.
ROOTFS_URL = (
	"https://cloud-images.ubuntu.com/releases/noble/release-20260518/"
	"ubuntu-24.04-server-cloudimg-amd64.squashfs"
)
ROOTFS_SHA256 = "bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74"
# Firecracker boots only the uncompressed ELF kernel inside the Ubuntu vmlinuz.
KERNEL_URL = (
	"https://cloud-images.ubuntu.com/releases/noble/release-20260518/"
	"unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic"
)
KERNEL_SHA256 = "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6"
BOOT_ARGUMENTS = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw"
VM_NAME = "atlas"
BENCH_NAME = "atlas"
FIRECRACKER_VERSION = "v1.16.1"
HOST_ADDRESS = "172.16.100.1"
VM_ADDRESS = "172.16.100.2"
FORWARD_PORTS = (80, 443)
WARPGATE_SSH_PORT = 2223
GATEWAY_PORT = 51821
GATEWAY_DEVELOPER_ADDRESS = "172.16.100.3"
SCALEWAY_ZONES = {
	"fr-par-1",
	"fr-par-2",
	"fr-par-3",
	"nl-ams-1",
	"nl-ams-2",
	"nl-ams-3",
	"pl-waw-1",
	"pl-waw-2",
	"pl-waw-3",
}
SERVER_PROVIDERS = ("Scaleway", "AWS")
# Scaleway private networks accept /20 through /29. An AWS VPC accepts /16 through /28.
PRIVATE_NETWORK_PREFIXES = {"Scaleway": (20, 29), "AWS": (16, 28)}
PROVIDER_TABLES = {"Scaleway": "scaleway", "AWS": "aws"}
PRIVATE_IPV4_NETWORKS = tuple(
	ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
REQUIRED_COMMANDS = (
	"curl",
	"ip",
	"iptables",
	"unsquashfs",
	"mkfs.ext4",
	"truncate",
	"ssh",
	"scp",
	"ssh-keygen",
	"zstd",
)
SSH_OPTIONS = (
	"-o",
	"StrictHostKeyChecking=no",
	"-o",
	"UserKnownHostsFile=/dev/null",
	"-o",
	"LogLevel=ERROR",
	"-o",
	"ConnectTimeout=5",
)


class AtlasVmError(Exception):
	"""An error the operator must act on."""


USE_COLOR = sys.stdout.isatty()
# curl redraws its bar with a carriage return, which a log or a pipe keeps as
# one line for each redraw.
CURL_PROGRESS = ["--progress-bar"] if USE_COLOR else ["--silent", "--show-error"]


def paint(text: str, code: str) -> str:
	return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def step(message: str) -> None:
	print(f"{paint('==>', '1;36')} {message}", flush=True)


def detail(message: str) -> None:
	print(f"    {paint(message, '2')}", flush=True)


def warn(message: str) -> None:
	print(f"{paint('!!!', '1;33')} {message}", flush=True)


def guest(line: str) -> None:
	print(f"{paint('  |', '2')} {line}", flush=True)


def run(command: list[str], **keywords) -> subprocess.CompletedProcess:
	return subprocess.run(command, check=True, **keywords)


@dataclass
class Settings:
	"""The configuration this host runs. `create` copies the file, and every later command reads the copy."""

	path: Path
	site: str = ""
	bench_user: str = "frappe"
	vcpu_count: int = 4
	memory_mib: int = 8192
	disk_gib: int = 24
	ssh_port: int = 2222
	setup_script_url: str = ""
	import_server_id: str = ""
	import_storage_pool_device: str = ""
	private_network_cidr: str = ""
	developer_public_key: str = ""
	has_warpgate: bool = False

	@classmethod
	def read(cls, path: Path) -> Settings:
		if not path.is_file():
			raise AtlasVmError(f"no configuration at {path}; run: atlas-vm create --config FILE")
		document = tomllib.loads(path.read_text())
		vm = document.get("vm", {})
		pilot = document.get("pilot", {})
		atlas = document.get("atlas", {})
		validate_configuration(document, path)
		validate_images(document.get("image", []), path)

		repository = atlas.get("repository", "https://github.com/frappe/atlas").removesuffix(".git")
		raw = repository.replace("github.com", "raw.githubusercontent.com")
		branch = atlas.get("branch", "develop")
		return cls(
			path=path,
			site=pilot.get("site", ""),
			bench_user=pilot.get("user", "frappe"),
			vcpu_count=int(vm.get("vcpu_count", 4)),
			memory_mib=int(vm.get("memory_mib", 8192)),
			disk_gib=int(vm.get("disk_gib", 24)),
			ssh_port=int(vm.get("ssh_port", 2222)),
			setup_script_url=f"{raw}/{branch}/scripts/atlas-vm/setup.py",
			import_server_id=atlas.get("import_server_id", ""),
			import_storage_pool_device=atlas.get("import_storage_pool_device", ""),
			has_warpgate="warpgate" in atlas,
			private_network_cidr=atlas.get("private_network_cidr", ""),
			developer_public_key=document.get("gateway", {}).get("developer_public_key", ""),
		)

	def update_sizes(self, changes: dict[str, int]) -> None:
		"""Rewrite the [vm] values in place, so the rest of the file keeps its comments."""
		lines = self.path.read_text().splitlines()
		for key, value in changes.items():
			for index, line in enumerate(lines):
				if line.split("=", 1)[0].strip() == key:
					lines[index] = f"{key} = {value}"
					break
			else:
				lines.insert(lines.index("[vm]") + 1, f"{key} = {value}")
			setattr(self, key, value)
		self.path.write_text("\n".join(lines) + "\n")


def validate_configuration(document: dict, path: Path) -> None:
	"""Reject an incomplete deployment configuration before the VM changes."""
	if "gateway" in document:
		_validate_keys(document, {"vm", "gateway", "atlas"}, path, "")
		_validate_vm_configuration(document.get("vm", {}), path)
		_required_string(document["gateway"], "developer_public_key", path, "gateway")
		_required_string(_required_table(document, "atlas", path), "private_network_cidr", path, "atlas")
		return
	_validate_keys(document, {"vm", "pilot", "atlas", "image"}, path, "")
	_validate_vm_configuration(document.get("vm", {}), path)
	_validate_pilot_configuration(_required_table(document, "pilot", path), path)
	_validate_atlas_configuration(_required_table(document, "atlas", path), path)


def _validate_vm_configuration(vm: object, path: Path) -> None:
	if not isinstance(vm, dict):
		raise AtlasVmError(f"{path}: vm must be a table")
	_validate_keys(vm, {"vcpu_count", "memory_mib", "disk_gib", "ssh_port"}, path, "vm")
	for key in ("vcpu_count", "memory_mib", "disk_gib", "ssh_port"):
		value = vm.get(key)
		if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
			raise AtlasVmError(f"{path}: vm.{key} must be a positive integer")
	if vm.get("ssh_port", 2222) > 65_535:
		raise AtlasVmError(f"{path}: vm.ssh_port must be at most 65535")


def _validate_pilot_configuration(pilot: dict, path: Path) -> None:
	_validate_keys(pilot, {"site", "admin_domain", "user", "letsencrypt_email"}, path, "pilot")
	for key in ("site", "letsencrypt_email"):
		_required_string(pilot, key, path, "pilot")
	for key in ("admin_domain", "user"):
		if key in pilot:
			_required_string(pilot, key, path, "pilot")


def _validate_atlas_configuration(atlas: dict, path: Path) -> None:
	_validate_keys(
		atlas,
		{
			"repository",
			"branch",
			"base_url",
			"server_provider",
			"dns_provider",
			"region_name",
			"region_id",
			"wildcard_domain",
			"private_network_cidr",
			"private_network_mtu",
			"central_jwks_url",
			"import_server_id",
			"import_storage_pool_device",
			"vm_scheduling",
			"scaleway",
			"aws",
			"route53",
			"letsencrypt",
			"warpgate",
		},
		path,
		"atlas",
	)
	for key in ("server_provider", "dns_provider", "region_name", "wildcard_domain"):
		_required_string(atlas, key, path, "atlas")
	for key in ("private_network_cidr", "private_network_mtu", "central_jwks_url"):
		if key not in atlas:
			raise AtlasVmError(f"{path}: atlas.{key} is required")
	for key in (
		"repository",
		"branch",
		"base_url",
		"central_jwks_url",
		"import_server_id",
		"import_storage_pool_device",
	):
		if key in atlas and not isinstance(atlas[key], str):
			raise AtlasVmError(f"{path}: atlas.{key} must be a string")
	provider = atlas["server_provider"]
	if provider not in SERVER_PROVIDERS:
		raise AtlasVmError(f"{path}: atlas.server_provider must be one of {', '.join(SERVER_PROVIDERS)}")
	if atlas["dns_provider"] != "Route53":
		raise AtlasVmError(f"{path}: atlas.dns_provider must be Route53")
	if atlas["wildcard_domain"].startswith("*.") or "." not in atlas["wildcard_domain"]:
		raise AtlasVmError(f"{path}: atlas.wildcard_domain must be a domain without '*.'")
	_validate_network_configuration(atlas, path, provider)
	_validate_vm_scheduling_configuration(_required_table(atlas, "vm_scheduling", path, "atlas"), path)
	_validate_provider_configuration(atlas, path, provider)
	_validate_route53_configuration(_required_table(atlas, "route53", path, "atlas"), path)
	_validate_letsencrypt_configuration(_required_table(atlas, "letsencrypt", path, "atlas"), path)
	if "warpgate" in atlas:
		warpgate = _required_table(atlas, "warpgate", path, "atlas")
		_validate_keys(warpgate, {"issuer_url", "client_id", "client_secret"}, path, "atlas.warpgate")
		for key in ("issuer_url", "client_id", "client_secret"):
			_required_string(warpgate, key, path, "atlas.warpgate")


def _validate_vm_scheduling_configuration(scheduling: dict, path: Path) -> None:
	_validate_keys(
		scheduling,
		{
			"use_dedicated_sleepy_vm_hosts",
			"placement_strategy",
			"sleepy_vm_overcommit_factor",
			"auto_spawn_metal_server",
			"default_metal_machine_size",
			"default_metal_machine_image",
		},
		path,
		"atlas.vm_scheduling",
	)
	_required_string(scheduling, "placement_strategy", path, "atlas.vm_scheduling")
	for key in ("use_dedicated_sleepy_vm_hosts", "auto_spawn_metal_server"):
		if not isinstance(scheduling.get(key), bool):
			raise AtlasVmError(f"{path}: atlas.vm_scheduling.{key} must be true or false")

	factor = scheduling.get("sleepy_vm_overcommit_factor")
	if (
		not isinstance(factor, (int, float))
		or isinstance(factor, bool)
		or not math.isfinite(factor)
		or factor < 1
	):
		raise AtlasVmError(
			f"{path}: atlas.vm_scheduling.sleepy_vm_overcommit_factor must be a finite number of at least 1"
		)

	for key in ("default_metal_machine_size", "default_metal_machine_image"):
		value = scheduling.get(key)
		if not isinstance(value, str):
			raise AtlasVmError(f"{path}: atlas.vm_scheduling.{key} must be a string")
		if scheduling["auto_spawn_metal_server"] and not value.strip():
			raise AtlasVmError(
				f"{path}: atlas.vm_scheduling.{key} is required when auto_spawn_metal_server is true"
			)


def _validate_provider_configuration(atlas: dict, path: Path, provider: str) -> None:
	"""Validate the table of the selected provider."""
	table = PROVIDER_TABLES[provider]
	values = _required_table(atlas, table, path, "atlas")
	if provider == "Scaleway":
		_validate_scaleway_configuration(values, path)
	else:
		_validate_aws_configuration(values, path)


def _validate_network_configuration(atlas: dict, path: Path, provider: str) -> None:
	region_id = _required_integer(atlas, "region_id", path, "atlas")
	if not 0 <= region_id <= 65_535:
		raise AtlasVmError(f"{path}: atlas.region_id must be from 0 through 65535")
	private_network_mtu = _required_integer(atlas, "private_network_mtu", path, "atlas")
	if private_network_mtu <= 0:
		raise AtlasVmError(f"{path}: atlas.private_network_mtu must be a positive integer")
	try:
		network = ipaddress.ip_network(atlas.get("private_network_cidr", "10.1.0.0/20"), strict=False)
	except ValueError as error:
		raise AtlasVmError(f"{path}: atlas.private_network_cidr is invalid: {error}") from error
	minimum, maximum = PRIVATE_NETWORK_PREFIXES[provider]
	if (
		network.version != 4
		or not any(network.subnet_of(item) for item in PRIVATE_IPV4_NETWORKS)
		or not minimum <= network.prefixlen <= maximum
	):
		raise AtlasVmError(
			f"{path}: atlas.private_network_cidr must be an IPv4 network from "
			f"/{minimum} through /{maximum} for {provider}"
		)


def _validate_scaleway_configuration(scaleway: dict, path: Path) -> None:
	_validate_keys(
		scaleway,
		{"organization_id", "project_id", "zone", "machine_billing_cycle", "access_key", "secret_key"},
		path,
		"atlas.scaleway",
	)
	for key in ("organization_id", "project_id", "zone", "machine_billing_cycle", "access_key", "secret_key"):
		_required_string(scaleway, key, path, "atlas.scaleway")
	if scaleway["zone"] not in SCALEWAY_ZONES:
		raise AtlasVmError(f"{path}: atlas.scaleway.zone is not supported")
	if scaleway["machine_billing_cycle"] not in {"Hourly", "Monthly"}:
		raise AtlasVmError(f"{path}: atlas.scaleway.machine_billing_cycle must be Hourly or Monthly")


def _validate_aws_configuration(aws: dict, path: Path) -> None:
	keys = ("region", "availability_zone", "access_key_id", "secret_access_key")
	_validate_keys(aws, set(keys), path, "atlas.aws")
	for key in keys:
		_required_string(aws, key, path, "atlas.aws")
	if not re.fullmatch(rf"{re.escape(aws['region'])}[a-z]", aws["availability_zone"]):
		raise AtlasVmError(f"{path}: atlas.aws.availability_zone is not in atlas.aws.region")


def _validate_route53_configuration(route53: dict, path: Path) -> None:
	_validate_keys(route53, {"access_key_id", "secret_access_key"}, path, "atlas.route53")
	for key in ("access_key_id", "secret_access_key"):
		_required_string(route53, key, path, "atlas.route53")


def _validate_letsencrypt_configuration(letsencrypt: dict, path: Path) -> None:
	_validate_keys(letsencrypt, {"email", "staging", "auto_renew"}, path, "atlas.letsencrypt")
	_required_string(letsencrypt, "email", path, "atlas.letsencrypt")
	for key in ("staging", "auto_renew"):
		if key not in letsencrypt:
			raise AtlasVmError(f"{path}: atlas.letsencrypt.{key} is required")
		if not isinstance(letsencrypt[key], bool):
			raise AtlasVmError(f"{path}: atlas.letsencrypt.{key} must be true or false")


def _validate_keys(values: dict, allowed: set[str], path: Path, prefix: str) -> None:
	for key in values.keys() - allowed:
		name = f"{prefix}.{key}" if prefix else key
		raise AtlasVmError(f"{path}: unknown configuration key {name}")


def _required_table(values: dict, key: str, path: Path, prefix: str = "") -> dict:
	name = f"{prefix}.{key}" if prefix else key
	table = values.get(key)
	if not isinstance(table, dict):
		raise AtlasVmError(f"{path}: {name} must be a table")
	return table


def _required_string(values: dict, key: str, path: Path, prefix: str) -> str:
	value = values.get(key)
	if not isinstance(value, str) or not value.strip():
		raise AtlasVmError(f"{path}: {prefix}.{key} must be a non-empty string")
	return value


def _required_integer(values: dict, key: str, path: Path, prefix: str) -> int:
	value = values.get(key)
	if not isinstance(value, int) or isinstance(value, bool):
		raise AtlasVmError(f"{path}: {prefix}.{key} must be an integer")
	return value


def validate_images(images: list[dict], path: Path) -> None:
	"""Refuse an image the builder cannot make, before the VM boots."""
	if not isinstance(images, list):
		raise AtlasVmError(f"{path}: image must be an array of tables")
	for image in images:
		_validate_image(image, path)


def _validate_image(image: object, path: Path) -> None:
	if not isinstance(image, dict):
		raise AtlasVmError(f"{path}: each image must be a table")
	_validate_keys(image, {"version", "architecture", "minimal"}, path, "image")
	version = image.get("version", "24.04")
	architecture = image.get("architecture", "amd64")
	minimal = image.get("minimal", False)
	if not isinstance(version, str):
		raise AtlasVmError(f"{path}: image.version must be a string")
	if not isinstance(architecture, str):
		raise AtlasVmError(f"{path}: image.architecture must be a string")
	if not isinstance(minimal, bool):
		raise AtlasVmError(f"{path}: image.minimal must be true or false")
	if version not in ("22.04", "24.04"):
		raise AtlasVmError(f"{path} asks for Ubuntu {version}; only 22.04 and 24.04 are supported")
	if architecture != "amd64":
		raise AtlasVmError(f"{path} asks for {architecture}; only amd64 is supported")
	if minimal and version != "24.04":
		raise AtlasVmError(f"{path} asks for a minimal Ubuntu {version}; only 24.04 has one")


def generate_password(length: int = 24) -> str:
	"""One password with every character class, which Frappe and Pilot both accept."""
	classes = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*-_=+")
	alphabet = "".join(classes)
	while True:
		password = "".join(secrets.choice(alphabet) for _ in range(length))
		if all(any(character in group for character in password) for group in classes):
			return password


def find_host_key() -> Path:
	"""The VM trusts the key this host already uses for Secure Shell. A new host gets a root key."""
	homes = [Path("/root")]
	if os.environ.get("SUDO_USER"):
		homes.append(Path("/home") / os.environ["SUDO_USER"])
	for home in homes:
		for name in ("id_ed25519", "id_rsa"):
			candidate = home / ".ssh" / name
			if candidate.is_file() and candidate.with_suffix(".pub").is_file():
				return candidate
	key = Path("/root/.ssh/id_ed25519")
	key.parent.mkdir(mode=0o700, exist_ok=True)
	run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
	return key


class GuestImage:
	"""The Ubuntu root file system the VM boots."""

	def __init__(self, settings: Settings, paths: "Paths", public_key: str) -> None:
		self.settings = settings
		self.paths = paths
		self.public_key = public_key

	def build(self) -> None:
		squashfs = self.paths.downloads / "ubuntu.squashfs"
		download(ROOTFS_URL, squashfs, ROOTFS_SHA256)

		extracted = self.paths.vm_directory / "rootfs"
		shutil.rmtree(extracted, ignore_errors=True)
		step("extract the root file system")
		run(["unsquashfs", "-q", "-d", str(extracted), str(squashfs)])

		self.prepare(extracted)

		step(f"create a {self.settings.disk_gib}G ext4 image")
		staged = self.paths.rootfs_image.with_suffix(".part")
		run(["truncate", "-s", f"{self.settings.disk_gib}G", str(staged)])
		run(["mkfs.ext4", "-q", "-F", "-d", str(extracted), str(staged)])
		staged.rename(self.paths.rootfs_image)
		shutil.rmtree(extracted, ignore_errors=True)

	def prepare(self, root: Path) -> None:
		"""Cloud-init has no data source here, so the image carries its own network and keys."""
		(root / "etc/cloud/cloud-init.disabled").touch()

		write_file(
			root / "etc/systemd/network/10-atlas-vm.network",
			f"""[Match]
Name=eth0

[Network]
Address={VM_ADDRESS}/24
Gateway={HOST_ADDRESS}
DNS=1.1.1.1
""",
		)

		enabled_units = {
			"multi-user.target.wants/systemd-networkd.service": "/lib/systemd/system/systemd-networkd.service",
			"multi-user.target.wants/systemd-resolved.service": "/lib/systemd/system/systemd-resolved.service",
			"getty.target.wants/serial-getty@ttyS0.service": "/lib/systemd/system/serial-getty@.service",
		}
		for link, target in enabled_units.items():
			path = root / "etc/systemd/system" / link
			path.parent.mkdir(parents=True, exist_ok=True)
			path.unlink(missing_ok=True)
			path.symlink_to(target)

		write_file(root / "etc/hostname", f"{VM_NAME}\n")
		with (root / "etc/hosts").open("a") as hosts:
			hosts.write(f"127.0.1.1 {VM_NAME}\n")

		authorized_keys = root / "root/.ssh/authorized_keys"
		write_file(authorized_keys, self.public_key, mode=0o600)
		authorized_keys.parent.chmod(0o700)
		write_file(root / "etc/ssh/sshd_config.d/90-atlas-vm.conf", "PermitRootLogin prohibit-password\n")
		run(["ssh-keygen", "-A", "-f", str(root)], stdout=subprocess.DEVNULL)


@dataclass
class Paths:
	"""Where the host keeps the VM and the shared downloads."""

	base: Path

	@property
	def downloads(self) -> Path:
		return self.base / "downloads"

	@property
	def firecracker_binary(self) -> Path:
		return self.base / "bin/firecracker"

	@property
	def kernel_image(self) -> Path:
		return self.downloads / "vmlinux-6.8.0-117"

	@property
	def vm_directory(self) -> Path:
		return self.base / VM_NAME

	@property
	def rootfs_image(self) -> Path:
		return self.vm_directory / "rootfs.ext4"

	@property
	def configuration(self) -> Path:
		return self.vm_directory / "firecracker.json"

	@property
	def network_script(self) -> Path:
		return self.vm_directory / "network"

	@property
	def api_socket(self) -> Path:
		return self.vm_directory / "firecracker.socket"

	@property
	def console_log(self) -> Path:
		return self.vm_directory / "console.log"

	@property
	def setup_log(self) -> Path:
		return self.vm_directory / "setup.log"


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)
	path.chmod(mode)


def download(url: str, path: Path, checksum: str = "") -> None:
	if path.exists():
		return
	step(f"download {path.name}")
	staged = path.with_suffix(path.suffix + ".part")
	run(["curl", "-fL", *CURL_PROGRESS, "-o", str(staged), url])
	if checksum:
		digest = hashlib.sha256(staged.read_bytes()).hexdigest()
		if digest != checksum:
			staged.unlink()
			raise AtlasVmError(f"{path.name} does not match its checksum")
	staged.rename(path)
	size = path.stat().st_size
	detail(f"{size / 1024**2:.0f} MiB" if size >= 1024**2 else f"{size / 1024:.0f} KiB")


def extract_kernel(vmlinuz: Path, path: Path) -> None:
	if path.exists():
		return
	data = vmlinuz.read_bytes()
	staged = path.with_suffix(".part")
	with staged.open("wb") as output:
		# zstd rejects the trailing bzImage data after it writes the kernel.
		subprocess.run(
			["zstd", "-cdq"],
			input=data[data.find(b"\x28\xb5\x2f\xfd") :],
			stdout=output,
			stderr=subprocess.DEVNULL,
		)
	if staged.read_bytes()[:4] != b"\x7fELF":
		staged.unlink()
		raise AtlasVmError(f"{vmlinuz.name} holds no ELF kernel")
	staged.rename(path)


class ConsoleReader:
	"""Follow the guest console. The kept lines explain a boot that never reaches Secure Shell."""

	def __init__(self, path: Path, echo: bool = False) -> None:
		self.path = path
		self.echo = echo
		self.position = path.stat().st_size if path.exists() else 0
		self.recent: deque[str] = deque(maxlen=40)

	def drain(self) -> None:
		if not self.path.exists():
			return
		with self.path.open("rb") as console:
			console.seek(self.position)
			data = console.read()
			self.position = console.tell()
		for line in data.decode("utf-8", "replace").splitlines():
			if self.echo:
				guest(line)
			else:
				self.recent.append(line)

	def report(self) -> None:
		"""Print the last console lines, unless they were printed already."""
		self.drain()
		if self.echo:
			return
		for line in self.recent:
			guest(line)


GATEWAY_SCRIPT = r"""set -eu
interface=atlas-gateway
# The guest root file system has no modules for its kernel. WireGuard needs them.
packages="wireguard-tools linux-modules-$(uname -r)"
if ! dpkg -s $packages >/dev/null 2>&1; then
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $packages >/dev/null
fi
install -d -m 700 /etc/wireguard
[ -f /etc/wireguard/$interface.key ] || (umask 077 && wg genkey > /etc/wireguard/$interface.key)
config="[Interface]
ListenPort = $LISTEN_PORT
PostUp = wg set %i private-key /etc/wireguard/$interface.key

[Peer]
PublicKey = $DEVELOPER_PUBLIC_KEY
AllowedIPs = $DEVELOPER_ADDRESS/32"
if [ "$(cat /etc/wireguard/$interface.conf 2>/dev/null || true)" != "$config" ]; then
	(umask 077 && printf '%s\n' "$config" > /etc/wireguard/$interface.conf)
	systemctl restart wg-quick@$interface
fi
systemctl enable --quiet wg-quick@$interface
systemctl is-active --quiet wg-quick@$interface || systemctl restart wg-quick@$interface
# The developer address is in the guest subnet, so the host masquerade carries it.
printf 'net.ipv4.ip_forward = 1\nnet.ipv4.conf.eth0.proxy_arp = 1\n' > /etc/sysctl.d/99-atlas-gateway.conf
sysctl -q -p /etc/sysctl.d/99-atlas-gateway.conf
wg show $interface public-key
"""


NETWORK_SCRIPT_BODY = r"""
# Each forward matches the host address, because a match on the port alone also
# captures traffic that another guest sends through this host to a remote server.
rules() {
	local uplink uplink_address pair host_port guest_port
	uplink=$(ip -4 route show default | awk 'NR == 1 { print $5 }')
	[[ -n $uplink ]] || { echo "this host has no IPv4 default route" >&2; exit 1; }
	uplink_address=$(ip -4 -o addr show dev "$uplink" | awk 'NR == 1 { split($4, field, "/"); print field[1] }')
	[[ -n $uplink_address ]] || { echo "$uplink has no IPv4 address" >&2; exit 1; }

	echo "-t nat -A POSTROUTING -s $vm_address/32 -o $uplink -j MASQUERADE"
	# The guest subnet reaches the provider private network behind this host.
	if [[ -n $private_network_cidr ]]; then
		echo "-t nat -I POSTROUTING -s ${host_address%.*}.0/24 -d $private_network_cidr -j MASQUERADE"
	fi
	for pair in $udp_forwards; do
		echo "-t nat -A PREROUTING -d $uplink_address -p udp --dport ${pair%%:*} -j DNAT --to-destination $vm_address:${pair##*:}"
	done
	echo "-t nat -A POSTROUTING -s 127.0.0.0/8 -d $vm_address -j SNAT --to-source $host_address"
	echo "-t filter -I FORWARD -i $tap_device -j ACCEPT"
	echo "-t filter -I FORWARD -o $tap_device -j ACCEPT"
	for pair in $forwards; do
		host_port=${pair%%:*}
		guest_port=${pair##*:}
		echo "-t nat -A PREROUTING -d $uplink_address -p tcp --dport $host_port -j DNAT --to-destination $vm_address:$guest_port"
		echo "-t nat -A OUTPUT -d $uplink_address -p tcp --dport $host_port -j DNAT --to-destination $vm_address:$guest_port"
		echo "-t nat -A OUTPUT -d $host_address -p tcp --dport $host_port -j DNAT --to-destination $vm_address:$guest_port"
		echo "-t nat -A OUTPUT -d 127.0.0.1 -p tcp --dport $host_port -j DNAT --to-destination $vm_address:$guest_port"
	done
}

start() {
	stop

	ip tuntap add dev "$tap_device" mode tap
	ip address add "$host_address/24" dev "$tap_device"
	ip link set "$tap_device" up

	sysctl -q -w net.ipv4.ip_forward=1
	# The kernel drops the DNAT source 127.0.0.1 unless the tap device allows it.
	sysctl -q -w "net.ipv4.conf.$tap_device.route_localnet=1"

	local rule
	while read -r rule; do
		# shellcheck disable=SC2086
		iptables -w 5 $rule
	done < <(rules)
}

stop() {
	local rule
	while read -r rule; do
		rule=${rule/ -A / -D }
		rule=${rule/ -I / -D }
		# Delete every copy an earlier run left. A stop that never ends blocks systemd.
		for _ in $(seq 20); do
			# shellcheck disable=SC2086
			iptables -w 5 $rule 2>/dev/null || break
		done
	done < <(rules)

	ip link del "$tap_device" 2>/dev/null || true
}

case "${1:-}" in
	start) start ;;
	stop) stop ;;
	*) echo "usage: network start|stop" >&2; exit 2 ;;
esac
"""


class VirtualMachine:
	"""The VM this host runs, owned by one systemd unit."""

	def __init__(self, settings: Settings) -> None:
		self.settings = settings
		self.paths = Paths(BASE_DIRECTORY)
		self.private_key = find_host_key()

	@property
	def tap_device(self) -> str:
		return f"tap-{VM_NAME[:11]}"

	@property
	def is_running(self) -> bool:
		return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE_NAME]).returncode == 0

	@property
	def is_created(self) -> bool:
		return self.paths.configuration.exists()

	@property
	def has_current_kernel(self) -> bool:
		configuration = json.loads(self.paths.configuration.read_text())
		return configuration["boot-source"]["kernel_image_path"] == str(self.paths.kernel_image)

	@property
	def port_forwards(self) -> list[tuple[int, int]]:
		ports = () if self.settings.developer_public_key else FORWARD_PORTS
		if self.settings.has_warpgate:
			ports = (*ports, WARPGATE_SSH_PORT)
		return [(self.settings.ssh_port, 22)] + [(port, port) for port in ports]

	def ssh_arguments(self, command: list[str] | None = None) -> list[str]:
		arguments = [
			"ssh",
			"-i",
			str(self.private_key),
			"-p",
			str(self.settings.ssh_port),
			*SSH_OPTIONS,
			f"root@{HOST_ADDRESS}",
		]
		return arguments + (command or [])

	def copy_to_guest(self, source: Path, target: str) -> None:
		"""scp spells the port -P, so it cannot borrow the ssh arguments."""
		run(
			[
				"scp",
				"-i",
				str(self.private_key),
				"-P",
				str(self.settings.ssh_port),
				*SSH_OPTIONS,
				str(source),
				f"root@{HOST_ADDRESS}:{target}",
			],
			stdout=subprocess.DEVNULL,
		)

	def write_in_guest(self, target: str, content: str) -> None:
		run(self.ssh_arguments([f"install -m 600 /dev/stdin {target}"]), input=content, text=True)

	def run_as_bench(self, command: str) -> int:
		return self.run_in_guest(f"su - {self.settings.bench_user} -c {shlex.quote(command)}")

	def run_in_guest(self, command: str) -> int:
		return subprocess.run(self.ssh_arguments([command])).returncode

	def write_network_script(self) -> None:
		forwards = " ".join(f"{host}:{guest}" for host, guest in self.port_forwards)
		cidr = self.settings.private_network_cidr
		header = f"""#!/usr/bin/env bash
# Host network for the {VM_NAME} VM. Generated by atlas-vm.
set -euo pipefail

tap_device={self.tap_device}
host_address={HOST_ADDRESS}
vm_address={VM_ADDRESS}
forwards="{forwards}"
udp_forwards="{f"{GATEWAY_PORT}:{GATEWAY_PORT}" if self.settings.developer_public_key else ""}"
private_network_cidr="{cidr}"
"""
		write_file(self.paths.network_script, header + NETWORK_SCRIPT_BODY, mode=0o755)

	def write_configuration(self) -> None:
		configuration = {
			"boot-source": {
				"kernel_image_path": str(self.paths.kernel_image),
				"boot_args": BOOT_ARGUMENTS,
			},
			"drives": [
				{
					"drive_id": "rootfs",
					"path_on_host": str(self.paths.rootfs_image),
					"is_root_device": True,
					"is_read_only": False,
				}
			],
			"machine-config": {
				"vcpu_count": self.settings.vcpu_count,
				"mem_size_mib": self.settings.memory_mib,
			},
			"network-interfaces": [
				{
					"iface_id": "eth0",
					"host_dev_name": self.tap_device,
					"guest_mac": "06:00:00:00:00:02",
				}
			],
		}
		write_file(self.paths.configuration, json.dumps(configuration, indent=2) + "\n")

	def write_unit(self) -> None:
		write_file(
			UNIT_FILE,
			f"""[Unit]
Description=Atlas Pilot VM ({VM_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStartPre=/bin/rm -f {self.paths.api_socket}
ExecStartPre={self.paths.network_script} start
ExecStart={self.paths.firecracker_binary} --api-sock {self.paths.api_socket} --config-file {self.paths.configuration}
# The guest reboots on Ctrl+Alt+Del, and Firecracker exits on a guest reboot.
ExecStop=/bin/sh -c "curl -fsS --max-time 5 --unix-socket {self.paths.api_socket} -X PUT -H 'Content-Type: application/json' -d '{{\\"action_type\\": \\"SendCtrlAltDel\\"}}' http://localhost/actions && while kill -0 ${{MAINPID}} 2>/dev/null; do sleep 1; done"
ExecStopPost={self.paths.network_script} stop
TimeoutStopSec=120
StandardOutput=append:{self.paths.console_log}
StandardError=append:{self.paths.console_log}
Restart=no

[Install]
WantedBy=multi-user.target
""",
		)
		run(["systemctl", "daemon-reload"])
		run(["systemctl", "enable", "--quiet", SERVICE_NAME])

	def install_firecracker(self) -> None:
		if self.paths.firecracker_binary.exists():
			return
		version = FIRECRACKER_VERSION
		archive = self.paths.downloads / "firecracker.tgz"
		step(f"install firecracker {version}")
		run(
			[
				"curl",
				"-fL",
				*CURL_PROGRESS,
				"-o",
				str(archive),
				f"https://github.com/firecracker-microvm/firecracker/releases/download/{version}/firecracker-{version}-x86_64.tgz",
			]
		)
		run(["tar", "-xzf", str(archive), "-C", str(self.paths.downloads)])
		released = self.paths.downloads / f"release-{version}-x86_64" / f"firecracker-{version}-x86_64"
		self.paths.firecracker_binary.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy(released, self.paths.firecracker_binary)
		self.paths.firecracker_binary.chmod(0o755)

	def start(self, verbose: bool = False) -> None:
		self.paths.api_socket.unlink(missing_ok=True)
		subprocess.run(["systemctl", "reset-failed", SERVICE_NAME], stderr=subprocess.DEVNULL)
		console = ConsoleReader(self.paths.console_log, echo=verbose)
		step("start the VM")
		detail(f"console: {self.paths.console_log}")
		run(["systemctl", "start", SERVICE_NAME])
		step(f"wait for Secure Shell on port {self.settings.ssh_port}")
		self.wait_for_ssh(console)

	def stop(self) -> None:
		run(["systemctl", "stop", SERVICE_NAME])

	def wait_for_ssh(self, console: ConsoleReader, seconds: int = 180) -> None:
		started = time.monotonic()
		deadline = started + seconds
		reported = 0.0
		while time.monotonic() < deadline:
			console.drain()
			if not self.is_running:
				console.report()
				raise AtlasVmError(f"the VM stopped; read {self.paths.console_log}")
			probe = subprocess.run(
				self.ssh_arguments(["true"]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
			)
			elapsed = time.monotonic() - started
			if probe.returncode == 0:
				detail(f"answered after {int(elapsed)}s")
				return
			if elapsed - reported >= 20:
				reported = elapsed
				detail(f"still waiting after {int(elapsed)}s")
			time.sleep(2)
		console.report()
		raise AtlasVmError(
			f"no Secure Shell answer on port {self.settings.ssh_port} after {seconds}s;"
			f" read {self.paths.console_log}"
		)

	def run_setup(self, script: Path | None = None) -> None:
		"""Run setup.py from the repository, or a local copy while it is unpushed."""
		step("run setup.py in the VM")
		if script:
			if not script.is_file():
				raise AtlasVmError(f"no setup script at {script}")
			detail(f"from {script}")
			self.copy_to_guest(script, "/root/setup.py")
		else:
			url = self.settings.setup_script_url
			detail(url)
			self.run_in_guest("rm -f /root/setup.py")
			if self.run_in_guest(f"curl -fsS --show-error -L -o /root/setup.py '{url}'") != 0:
				raise AtlasVmError(f"could not download {url} in the VM")
		command = "python3 /root/setup.py"
		detail(f"log: {self.paths.setup_log}")

		# The file holds the site password, so the guest keeps it for this run only.
		self.write_in_guest("/root/atlas-vm.toml", self.settings.path.read_text())
		try:
			with (
				subprocess.Popen(
					self.ssh_arguments([command]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
				) as process,
				self.paths.setup_log.open("w") as log,
			):
				for line in process.stdout:
					print(line, end="", flush=True)
					log.write(line)
		finally:
			self.run_in_guest("rm -f /root/atlas-vm.toml /root/setup.py")
		if process.returncode != 0:
			raise AtlasVmError(f"setup.py failed; read {self.paths.setup_log}")

	def setup_gateway(self) -> None:
		"""Relay the developer WireGuard link to the provider private network."""
		step("set up the development gateway")
		environment = f"DEVELOPER_PUBLIC_KEY={shlex.quote(self.settings.developer_public_key)} LISTEN_PORT={GATEWAY_PORT} DEVELOPER_ADDRESS={GATEWAY_DEVELOPER_ADDRESS}"
		result = subprocess.run(
			self.ssh_arguments([f"{environment} bash -s"]),
			input=GATEWAY_SCRIPT,
			capture_output=True,
			text=True,
		)
		if result.returncode != 0:
			raise AtlasVmError(f"gateway setup failed: {result.stdout[-500:]}{result.stderr[-500:]}")
		print(f"gateway public key: {result.stdout.strip().splitlines()[-1]}")

	def import_host(self) -> None:
		"""Add this host to Atlas as a Metal Server. Atlas connects to it with the key of its bench user."""
		settings = self.settings
		if not settings.import_server_id:
			return

		step(f"import this host as Metal Server {settings.import_server_id}")
		result = subprocess.run(
			self.ssh_arguments([f"ssh-keygen -y -f /home/{settings.bench_user}/.ssh/id_ed25519"]),
			capture_output=True,
			text=True,
		)
		if result.returncode != 0:
			raise AtlasVmError(f"could not read the Atlas SSH key in the VM: {result.stderr.strip()}")
		authorize_root_key(result.stdout.strip())

		command = f"pilot frappe --site {settings.site} import-metal-server {shlex.quote(settings.import_server_id)}"
		if settings.import_storage_pool_device:
			command += f" --storage-pool-device {shlex.quote(settings.import_storage_pool_device)}"
		if self.run_as_bench(f"{command} --bench {BENCH_NAME}") != 0:
			raise AtlasVmError("Atlas could not import this host")


DESTROY_WARNING = """DANGER: this deletes the VM in {directory}. The bench, every site, every
database, and every file in the guest disk are gone for ever. There is no
snapshot and no backup. Copy out what you need first.

The cloud image, the guest kernel, and firecracker are deleted with it, and the
next create downloads them again. Use --keep-downloads to keep them."""


def confirm(prompt: str, expected: str) -> None:
	answer = input(f"{prompt} ").strip()
	if answer != expected:
		raise AtlasVmError("stopped; nothing changed")


def require_root() -> None:
	if os.geteuid() != 0:
		raise AtlasVmError("run this as root")


def require_host_support() -> None:
	missing = [name for name in REQUIRED_COMMANDS if shutil.which(name) is None]
	if missing:
		raise AtlasVmError(f"missing commands: {', '.join(missing)}")
	if not Path("/dev/kvm").exists():
		raise AtlasVmError("no /dev/kvm on this host")
	if os.uname().machine != "x86_64":
		raise AtlasVmError("this CLI supports x86_64 only")


def authorize_root_key(public_key: str) -> None:
	"""Let one key log in as root. The host firewall later limits SSH to wg0 and the private network."""
	ssh_directory = Path("/root/.ssh")
	ssh_directory.mkdir(mode=0o700, exist_ok=True)
	authorized_keys = ssh_directory / "authorized_keys"
	lines = authorized_keys.read_text().splitlines() if authorized_keys.exists() else []
	if public_key not in lines:
		authorized_keys.write_text("\n".join([*lines, public_key]) + "\n")
	authorized_keys.chmod(0o600)


def install_self() -> None:
	source = Path(__file__).resolve()
	if source == INSTALLED_PATH:
		return
	shutil.copy(source, INSTALLED_PATH)
	INSTALLED_PATH.chmod(0o755)
	detail(f"installed {INSTALLED_PATH}")


def report_running_vm(machine: VirtualMachine) -> None:
	print(f"{SERVICE_NAME} already runs the VM in {machine.paths.vm_directory}.", file=sys.stderr)
	print("\nConnect to it:\n  atlas-vm ssh\n", file=sys.stderr)
	print(DESTROY_WARNING.format(directory=machine.paths.vm_directory), file=sys.stderr)
	print("\n  atlas-vm destroy", file=sys.stderr)


def command_create(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	if machine.is_running:
		report_running_vm(machine)
		raise SystemExit(1)
	require_host_support()

	paths = machine.paths
	for directory in (paths.base / "bin", paths.downloads, paths.vm_directory):
		directory.mkdir(parents=True, exist_ok=True)

	free_gib = shutil.disk_usage(paths.base).free // 1024**3
	if free_gib < machine.settings.disk_gib:
		warn(f"{paths.base} has {free_gib}G free for a {machine.settings.disk_gib}G guest disk")

	machine.install_firecracker()
	download(KERNEL_URL, paths.downloads / "vmlinuz", KERNEL_SHA256)
	extract_kernel(paths.downloads / "vmlinuz", paths.kernel_image)

	if arguments.rebuild and paths.rootfs_image.exists():
		print(DESTROY_WARNING.format(directory=paths.vm_directory), file=sys.stderr)
		confirm(f"Type the VM name ({VM_NAME}) to rebuild its disk:", VM_NAME)
		paths.rootfs_image.unlink()
	if not paths.rootfs_image.exists():
		public_key = machine.private_key.with_suffix(".pub").read_text()
		GuestImage(machine.settings, paths, public_key).build()

	if arguments.config_file.resolve() != CONFIG_FILE.resolve():
		shutil.copy(arguments.config_file, CONFIG_FILE)
		machine.settings.path = CONFIG_FILE
	CONFIG_FILE.chmod(0o600)
	machine.write_network_script()
	machine.write_configuration()
	machine.write_unit()
	install_self()

	machine.start(arguments.verbose)
	if machine.settings.developer_public_key:
		machine.setup_gateway()
	elif not arguments.skip_setup:
		machine.run_setup(arguments.script)
		machine.import_host()
	command_status(machine, arguments)


def command_start(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	if machine.is_running:
		raise AtlasVmError(f"{SERVICE_NAME} is already running")
	if not machine.is_created:
		raise AtlasVmError(f"no VM in {machine.paths.vm_directory}; run: atlas-vm create")
	machine.start(arguments.verbose)
	command_status(machine, arguments)


def command_stop(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	if not machine.is_running:
		raise AtlasVmError(f"{SERVICE_NAME} is not running")
	machine.stop()
	detail(f"the guest disk in {machine.paths.rootfs_image} is kept")


def command_restart(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	if machine.is_running:
		machine.stop()
	machine.start(arguments.verbose)
	command_status(machine, arguments)


def command_status(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	settings = machine.settings
	paths = machine.paths
	state = subprocess.run(
		["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True
	).stdout.strip()
	forwards = ", ".join(f"{host} -> {guest}" for host, guest in machine.port_forwards)
	print(f"service:  {SERVICE_NAME} ({state})")
	print(f"vm:       {VM_NAME} in {paths.vm_directory}")
	print(
		f"machine:  {settings.vcpu_count} vCPU, {settings.memory_mib} MiB memory, {settings.disk_gib} GiB disk"
	)
	print(f"address:  {VM_ADDRESS} through {HOST_ADDRESS} on {machine.tap_device}")
	print(f"ports:    {forwards}")
	if paths.rootfs_image.exists():
		# The image is sparse: the host gives it blocks as the guest writes them.
		allocated_gib = paths.rootfs_image.stat().st_blocks * 512 / 1024**3
		print(
			f"disk:     {settings.disk_gib} GiB in the guest, {allocated_gib:.1f} GiB allocated on the host"
		)
	print(f"config:   {CONFIG_FILE}")
	print(f"logs:     {paths.console_log}, {paths.setup_log}")
	print("ssh:      atlas-vm ssh")
	print(f"          ssh -i {machine.private_key} -p {settings.ssh_port} root@{HOST_ADDRESS}")


def command_ssh(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	if not machine.is_running:
		raise AtlasVmError(f"{SERVICE_NAME} is not running; start it with: atlas-vm start")
	os.execvp("ssh", machine.ssh_arguments(arguments.command))


def command_logs(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	path = machine.paths.setup_log if arguments.setup else machine.paths.console_log
	if not path.exists():
		raise AtlasVmError(f"no log at {path}")
	if arguments.follow:
		os.execvp("tail", ["tail", "-n", "50", "-F", str(path)])
	sys.stdout.write(path.read_text())


def command_setup(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	if not machine.is_running:
		raise AtlasVmError(f"{SERVICE_NAME} is not running; start it with: atlas-vm start")
	if not machine.has_current_kernel:
		raise AtlasVmError("the VM boots an older kernel; run: atlas-vm stop && atlas-vm create")
	if machine.settings.developer_public_key:
		machine.setup_gateway()
		return
	machine.run_setup(arguments.script)
	machine.import_host()


def command_reset_password(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	"""Set a new password on the Pilot admin panel or on the site Administrator."""
	if not machine.is_running:
		raise AtlasVmError(f"{SERVICE_NAME} is not running; start it with: atlas-vm start")

	settings = machine.settings
	password = generate_password()
	if arguments.target == "pilot":
		step("reset the Pilot admin password")
		command = f"pilot set-admin-password --password {shlex.quote(password)}"
	else:
		step(f"reset Administrator on {settings.site}")
		command = (
			f"pilot --bench {BENCH_NAME} --site {settings.site}"
			f" set-password Administrator {shlex.quote(password)}"
		)
	if machine.run_as_bench(command) != 0:
		raise AtlasVmError("the password did not change; the old one still works")
	print(password)


def command_destroy(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	paths = machine.paths
	if not paths.vm_directory.exists():
		raise AtlasVmError(f"no VM in {paths.vm_directory}")
	print(DESTROY_WARNING.format(directory=paths.vm_directory), file=sys.stderr)
	if not arguments.force:
		confirm(f"\nType the VM name ({VM_NAME}) to delete it:", VM_NAME)

	if machine.is_running:
		machine.stop()
	if paths.network_script.exists():
		subprocess.run([str(paths.network_script), "stop"])
	UNIT_FILE.unlink(missing_ok=True)
	run(["systemctl", "daemon-reload"])
	shutil.rmtree(paths.vm_directory)
	CONFIG_FILE.unlink(missing_ok=True)
	detail(f"deleted {paths.vm_directory}, {CONFIG_FILE}, and {UNIT_FILE}")

	if arguments.keep_downloads:
		detail(f"the cloud image, the kernel, and firecracker in {paths.base} are kept")
		return
	shutil.rmtree(paths.downloads, ignore_errors=True)
	shutil.rmtree(paths.base / "bin", ignore_errors=True)
	detail(f"deleted the cloud image, the kernel, and firecracker in {paths.base}")
	if paths.base.exists() and not any(paths.base.iterdir()):
		paths.base.rmdir()


def command_resize(machine: VirtualMachine, arguments: argparse.Namespace) -> None:
	"""Firecracker fixes the machine size at boot, so every resize reboots the VM."""
	settings = machine.settings
	if not machine.is_created:
		raise AtlasVmError(f"no VM in {machine.paths.vm_directory}; run: atlas-vm create")

	changes: dict[str, int] = {}
	if arguments.vcpu and arguments.vcpu != settings.vcpu_count:
		changes["vcpu_count"] = arguments.vcpu
	if arguments.memory and arguments.memory != settings.memory_mib:
		changes["memory_mib"] = arguments.memory
	if arguments.disk and arguments.disk != settings.disk_gib:
		if arguments.disk < settings.disk_gib:
			raise AtlasVmError(
				f"a disk can only grow; it is {settings.disk_gib}G and cannot become {arguments.disk}G"
			)
		changes["disk_gib"] = arguments.disk
	if not changes:
		raise AtlasVmError("give a new --vcpu, --memory, or --disk value that differs from the current one")

	print("This changes the VM:")
	for key, value in changes.items():
		print(f"  {key}: {getattr(settings, key)} -> {value}")
	print(
		"\nWARNING: the VM stops and every process in it is killed, including the bench,"
		"\nthe database, and any running job. Files on the guest disk are kept."
	)
	if not arguments.yes:
		confirm("\nType yes to continue:", "yes")

	if machine.is_running:
		machine.stop()

	settings.update_sizes(changes)
	if "disk_gib" in changes:
		step(f"grow the guest disk to {settings.disk_gib}G")
		run(["truncate", "-s", f"{settings.disk_gib}G", str(machine.paths.rootfs_image)])
	machine.write_configuration()

	machine.start(arguments.verbose)
	if "disk_gib" in changes:
		step("grow the guest file system")
		if machine.run_in_guest("resize2fs /dev/vda") != 0:
			raise AtlasVmError("resize2fs failed in the guest; the VM runs with the old file system size")
	command_status(machine, arguments)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="atlas-vm", description=__doc__)
	parser.add_argument(
		"-v", "--verbose", action="store_true", help="print the guest console while the VM boots"
	)
	subparsers = parser.add_subparsers(dest="command", required=True)

	create = subparsers.add_parser("create", help="build the VM, start it, and run setup.py")
	create.add_argument(
		"--config", type=Path, help="configuration file to build the VM from (default: ./atlas-vm.toml)"
	)
	create.add_argument("--skip-setup", action="store_true", help="do not run setup.py")
	create.add_argument(
		"--script", type=Path, help="run this local setup.py instead of the one in the repository"
	)
	create.add_argument("--rebuild", action="store_true", help="delete the guest disk and build it again")
	create.set_defaults(handler=command_create)

	subparsers.add_parser("start", help="start the VM").set_defaults(handler=command_start)
	subparsers.add_parser("stop", help="stop the VM and remove its host network").set_defaults(
		handler=command_stop
	)
	subparsers.add_parser("restart", help="stop and start the VM").set_defaults(handler=command_restart)
	subparsers.add_parser("status", help="report the VM, its size, and its ports").set_defaults(
		handler=command_status
	)
	ssh = subparsers.add_parser("ssh", help="open a shell or run one command in the VM")
	ssh.add_argument("command", nargs=argparse.REMAINDER, help="command to run in the VM")
	ssh.set_defaults(handler=command_ssh)
	logs = subparsers.add_parser("logs", help="read the console log or the setup log")
	logs.add_argument("--setup", action="store_true", help="read the setup log")
	logs.add_argument("--follow", "-f", action="store_true", help="follow the log")
	logs.set_defaults(handler=command_logs)
	setup = subparsers.add_parser("setup", help="run setup.py again in a running VM")
	setup.add_argument(
		"--script", type=Path, help="run this local setup.py instead of the one in the repository"
	)
	setup.set_defaults(handler=command_setup)
	reset_password = subparsers.add_parser("reset-password", help="set a new random password and print it")
	reset_password.add_argument(
		"target", choices=("pilot", "site"), help="the Pilot admin panel or the site Administrator"
	)
	reset_password.set_defaults(handler=command_reset_password)

	resize = subparsers.add_parser("resize", help="change the vCPU count, the memory, or the disk size")
	resize.add_argument("--vcpu", type=int, help="new vCPU count")
	resize.add_argument("--memory", type=int, help="new memory size in MiB")
	resize.add_argument("--disk", type=int, help="new disk size in GiB; a disk can only grow")
	resize.add_argument("--yes", action="store_true", help="do not ask for confirmation")
	resize.set_defaults(handler=command_resize)
	destroy = subparsers.add_parser("destroy", help="stop the VM and delete every file it owns")
	destroy.add_argument("--force", action="store_true", help="do not ask for confirmation")
	destroy.add_argument(
		"--keep-downloads",
		action="store_true",
		help="keep the cloud image, the kernel, and firecracker for the next create",
	)
	destroy.set_defaults(handler=command_destroy)

	return parser


def resolve_config_file(given: Path | None) -> Path:
	"""Only `create` reads the configuration file."""
	if given:
		if not given.is_file():
			raise AtlasVmError(f"no configuration file at {given}")
		return given
	candidates = []
	if os.environ.get("ATLAS_DEPLOYER_CONFIG"):
		candidates.append(Path(os.environ["ATLAS_DEPLOYER_CONFIG"]))
	candidates.append(Path.cwd() / "atlas-vm.toml")
	candidates.append(Path(__file__).resolve().parent / "atlas-vm.toml")
	candidates.append(CONFIG_FILE)
	for candidate in candidates:
		if candidate.is_file():
			return candidate
	raise AtlasVmError(
		"no configuration file; copy atlas-vm.example.toml to atlas-vm.toml and pass it with --config"
	)


def main() -> int:
	arguments = build_parser().parse_args()
	try:
		require_root()
		if arguments.command == "create":
			arguments.config_file = resolve_config_file(arguments.config)
		settings = Settings.read(arguments.config_file if arguments.command == "create" else CONFIG_FILE)
		arguments.handler(VirtualMachine(settings), arguments)
	except AtlasVmError as error:
		print(f"{paint('error:', '1;31')} {error}", file=sys.stderr)
		return 1
	except subprocess.CalledProcessError as error:
		failed = " ".join(str(part) for part in error.cmd)
		print(f"{paint('error:', '1;31')} command failed: {failed}", file=sys.stderr)
		return error.returncode
	except KeyboardInterrupt:
		return 130
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
