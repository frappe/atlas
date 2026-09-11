#!/usr/bin/env python3
"""Manage the Atlas VM on one bare metal host. One VM per host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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
# The Ubuntu kernel does not boot on the Firecracker device model. Use the CI kernel.
KERNEL_URL = "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/vmlinux-5.10.223"
BOOT_ARGUMENTS = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw"
VM_NAME = "atlas"
BENCH_NAME = "atlas"
FIRECRACKER_VERSION = "v1.16.1"
HOST_ADDRESS = "172.16.100.1"
VM_ADDRESS = "172.16.100.2"
FORWARD_PORTS = (80, 443)
REQUIRED_COMMANDS = (
	"curl", "ip", "iptables", "unsquashfs", "mkfs.ext4", "truncate", "ssh", "scp", "ssh-keygen",
)
SSH_OPTIONS = (
	"-o", "StrictHostKeyChecking=no",
	"-o", "UserKnownHostsFile=/dev/null",
	"-o", "LogLevel=ERROR",
	"-o", "ConnectTimeout=5",
)


class AtlasVmError(Exception):
	"""An error the operator must act on."""


USE_COLOR = sys.stdout.isatty()


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
	vcpu_count: int = 4
	memory_mib: int = 8192
	disk_gib: int = 24
	ssh_port: int = 2222
	setup_script_url: str = ""

	@classmethod
	def read(cls, path: Path) -> Settings:
		if not path.is_file():
			raise AtlasVmError(f"no configuration at {path}; run: atlas-vm create --config FILE")
		document = tomllib.loads(path.read_text())
		vm = document.get("vm", {})
		pilot = document.get("pilot", {})
		atlas = document.get("atlas", {})
		if not pilot.get("site") or not pilot.get("password"):
			raise AtlasVmError(f"{path} needs pilot.site and pilot.password")
		validate_images(document.get("image", []), path)

		repository = atlas.get("repository", "https://github.com/frappe/atlas").removesuffix(".git")
		raw = repository.replace("github.com", "raw.githubusercontent.com")
		branch = atlas.get("branch", "develop")
		return cls(
			path=path,
			vcpu_count=int(vm.get("vcpu_count", 4)),
			memory_mib=int(vm.get("memory_mib", 8192)),
			disk_gib=int(vm.get("disk_gib", 24)),
			ssh_port=int(vm.get("ssh_port", 2222)),
			setup_script_url=vm.get("setup_script_url", f"{raw}/{branch}/scripts/atlas-vm/setup.py"),
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


def validate_images(images: list[dict], path: Path) -> None:
	"""Refuse an image the builder cannot make, before the VM boots."""
	for image in images:
		version = str(image.get("version", "24.04"))
		architecture = image.get("architecture", "amd64")
		if version not in ("22.04", "24.04"):
			raise AtlasVmError(f"{path} asks for Ubuntu {version}; only 22.04 and 24.04 are supported")
		if architecture != "amd64":
			raise AtlasVmError(f"{path} asks for {architecture}; only amd64 is supported")
		if image.get("minimal") and version != "24.04":
			raise AtlasVmError(f"{path} asks for a minimal Ubuntu {version}; only 24.04 has one")


def find_host_key() -> Path:
	"""The VM trusts the key this host already uses for Secure Shell."""
	homes = [Path("/root")]
	if os.environ.get("SUDO_USER"):
		homes.append(Path("/home") / os.environ["SUDO_USER"])
	for home in homes:
		for name in ("id_ed25519", "id_rsa"):
			candidate = home / ".ssh" / name
			if candidate.is_file() and candidate.with_suffix(".pub").is_file():
				return candidate
	raise AtlasVmError("no Secure Shell key pair for this host; create one with: ssh-keygen -t ed25519")


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
		return self.downloads / "vmlinux"

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
	run(["curl", "-fL", "--progress-bar", "-o", str(staged), url])
	if checksum:
		digest = hashlib.sha256(staged.read_bytes()).hexdigest()
		if digest != checksum:
			staged.unlink()
			raise AtlasVmError(f"{path.name} does not match its checksum")
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


NETWORK_SCRIPT_BODY = r'''
# The tap device is excluded from PREROUTING, so guest traffic to a forwarded
# port leaves the host instead of returning to the VM.
rules() {
	local uplink pair host_port guest_port
	uplink=$(ip -4 route show default | awk 'NR == 1 { print $5 }')
	[[ -n $uplink ]] || { echo "this host has no IPv4 default route" >&2; exit 1; }

	echo "-t nat -A POSTROUTING -s $vm_address/32 -o $uplink -j MASQUERADE"
	echo "-t nat -A POSTROUTING -s 127.0.0.0/8 -d $vm_address -j SNAT --to-source $host_address"
	echo "-I FORWARD -i $tap_device -j ACCEPT"
	echo "-I FORWARD -o $tap_device -j ACCEPT"
	for pair in $forwards; do
		host_port=${pair%%:*}
		guest_port=${pair##*:}
		echo "-t nat -A PREROUTING ! -i $tap_device -p tcp --dport $host_port -j DNAT --to-destination $vm_address:$guest_port"
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
'''


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
	def port_forwards(self) -> list[tuple[int, int]]:
		return [(self.settings.ssh_port, 22)] + [(port, port) for port in FORWARD_PORTS]

	def ssh_arguments(self, command: list[str] | None = None) -> list[str]:
		arguments = [
			"ssh",
			"-i", str(self.private_key),
			"-p", str(self.settings.ssh_port),
			*SSH_OPTIONS,
			f"root@{HOST_ADDRESS}",
		]
		return arguments + (command or [])

	def copy_to_guest(self, source: Path, target: str) -> None:
		"""scp spells the port -P, so it cannot borrow the ssh arguments."""
		run(
			[
				"scp",
				"-i", str(self.private_key),
				"-P", str(self.settings.ssh_port),
				*SSH_OPTIONS,
				str(source),
				f"root@{HOST_ADDRESS}:{target}",
			],
			stdout=subprocess.DEVNULL,
		)

	def write_in_guest(self, target: str, content: str) -> None:
		run(self.ssh_arguments([f"install -m 600 /dev/stdin {target}"]), input=content, text=True)

	def run_in_guest(self, command: str) -> int:
		return subprocess.run(self.ssh_arguments([command])).returncode

	def write_network_script(self) -> None:
		forwards = " ".join(f"{host}:{guest}" for host, guest in self.port_forwards)
		header = f"""#!/usr/bin/env bash
# Host network for the {VM_NAME} VM. Generated by atlas-vm.
set -euo pipefail

tap_device={self.tap_device}
host_address={HOST_ADDRESS}
vm_address={VM_ADDRESS}
forwards="{forwards}"
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
ExecStartPre={self.paths.network_script} start
ExecStart={self.paths.firecracker_binary} --api-sock {self.paths.api_socket} --config-file {self.paths.configuration}
ExecStopPost={self.paths.network_script} stop
TimeoutStopSec=30
StandardOutput=append:{self.paths.console_log}
StandardError=append:{self.paths.console_log}
Restart=no

[Install]
WantedBy=multi-user.target
""",
		)
		run(["systemctl", "daemon-reload"])

	def install_firecracker(self) -> None:
		if self.paths.firecracker_binary.exists():
			return
		version = FIRECRACKER_VERSION
		archive = self.paths.downloads / "firecracker.tgz"
		step(f"install firecracker {version}")
		run([
			"curl", "-fL", "--progress-bar", "-o", str(archive),
			f"https://github.com/firecracker-microvm/firecracker/releases/download/{version}/firecracker-{version}-x86_64.tgz",
		])
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
			self.run_in_guest(f"curl -fsS --show-error -L -o /root/setup.py '{url}'")
		command = "python3 /root/setup.py"
		detail(f"log: {self.paths.setup_log}")

		# The file holds the site password, so the guest keeps it for this run only.
		self.write_in_guest("/root/atlas-vm.toml", self.settings.path.read_text())
		try:
			with subprocess.Popen(
				self.ssh_arguments([command]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
			) as process, self.paths.setup_log.open("w") as log:
				for line in process.stdout:
					print(line, end="", flush=True)
					log.write(line)
		finally:
			self.run_in_guest("rm -f /root/atlas-vm.toml /root/setup.py")
		if process.returncode != 0:
			raise AtlasVmError(f"setup.py failed; read {self.paths.setup_log}")


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
	download(KERNEL_URL, paths.kernel_image)

	if arguments.rebuild and paths.rootfs_image.exists():
		print(DESTROY_WARNING.format(directory=paths.vm_directory), file=sys.stderr)
		confirm(f"Type the VM name ({VM_NAME}) to rebuild its disk:", VM_NAME)
		paths.rootfs_image.unlink()
	if not paths.rootfs_image.exists():
		public_key = machine.private_key.with_suffix(".pub").read_text()
		GuestImage(machine.settings, paths, public_key).build()

	if arguments.config_file.resolve() != CONFIG_FILE.resolve():
		shutil.copy(arguments.config_file, CONFIG_FILE)
		CONFIG_FILE.chmod(0o600)
		machine.settings.path = CONFIG_FILE
	machine.write_network_script()
	machine.write_configuration()
	machine.write_unit()
	install_self()

	machine.start(arguments.verbose)
	if not arguments.skip_setup:
		machine.run_setup(arguments.script)
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
	print(f"machine:  {settings.vcpu_count} vCPU, {settings.memory_mib} MiB memory, {settings.disk_gib} GiB disk")
	print(f"address:  {VM_ADDRESS} through {HOST_ADDRESS} on {machine.tap_device}")
	print(f"ports:    {forwards}")
	if paths.rootfs_image.exists():
		# The image is sparse: the host gives it blocks as the guest writes them.
		allocated_gib = paths.rootfs_image.stat().st_blocks * 512 / 1024**3
		print(f"disk:     {settings.disk_gib} GiB in the guest, {allocated_gib:.1f} GiB allocated on the host")
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
	machine.run_setup(arguments.script)


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
	create.add_argument("--script", type=Path, help="run this local setup.py instead of the one in the repository")
	create.add_argument("--rebuild", action="store_true", help="delete the guest disk and build it again")
	create.set_defaults(handler=command_create)

	subparsers.add_parser("start", help="start the VM").set_defaults(handler=command_start)
	subparsers.add_parser("stop", help="stop the VM and remove its host network").set_defaults(handler=command_stop)
	subparsers.add_parser("restart", help="stop and start the VM").set_defaults(handler=command_restart)
	subparsers.add_parser("status", help="report the VM, its size, and its ports").set_defaults(handler=command_status)
	ssh = subparsers.add_parser("ssh", help="open a shell or run one command in the VM")
	ssh.add_argument("command", nargs=argparse.REMAINDER, help="command to run in the VM")
	ssh.set_defaults(handler=command_ssh)
	logs = subparsers.add_parser("logs", help="read the console log or the setup log")
	logs.add_argument("--setup", action="store_true", help="read the setup log")
	logs.add_argument("--follow", "-f", action="store_true", help="follow the log")
	logs.set_defaults(handler=command_logs)
	setup = subparsers.add_parser("setup", help="run setup.py again in a running VM")
	setup.add_argument("--script", type=Path, help="run this local setup.py instead of the one in the repository")
	setup.set_defaults(handler=command_setup)
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
		"no configuration file; copy atlas-vm.example.toml to atlas-vm.toml"
		" and pass it with --config"
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
