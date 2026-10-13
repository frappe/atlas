#!/usr/bin/python3

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, NotRequired, TextIO, TypedDict
from urllib.request import Request, urlopen

DEVICE = "/dev/vdb"
ROOT = Path("/mnt/original")
STATE = Path("/var/lib/atlas-rescue")
UNIT = "atlas-rescue-operation"


class SiteInventory(TypedDict):
	"""Paths found and missing for one installed site."""

	path: str
	present: list[str]
	missing: list[str]


class RecoveryInventory(TypedDict):
	"""Recovery sources and optional export results."""

	sites: list[SiteInventory]
	paths: list[str]
	missing: list[str]
	database_paths: list[str]
	limits: list[str]
	complete: NotRequired[bool]
	files: NotRequired[list[dict[str, str | int]]]
	symbolic_links: NotRequired[list[dict[str, str]]]
	special_files: NotRequired[list[dict[str, str]]]


def run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
	return subprocess.run(args, check=True, text=True, **kwargs)


def capture(*args: str) -> str:
	return run(*args, capture_output=True).stdout.strip()


def metadata(path: str) -> str:
	base = "http://169.254.169.254/latest/"
	token = (
		urlopen(
			Request(base + "api/token", method="PUT", headers={"X-metadata-token-ttl-seconds": "60"}),
			timeout=3,
		)
		.read()
		.decode()
	)
	return (
		urlopen(Request(base + "meta-data/" + path, headers={"X-metadata-token": token}), timeout=3)
		.read()
		.decode()
		.strip()
	)


def access_mode() -> str:
	try:
		mode = metadata("rescue/access")
		return mode if mode in {"protected", "read_only", "unprotected"} else "unknown"
	except OSError:
		return "unknown"


def require_writable() -> None:
	if access_mode() not in {"protected", "unprotected"} or capture("blockdev", "--getro", DEVICE) != "0":
		raise RuntimeError(
			"Writes are disabled. Select protected rescue or explicitly approve unprotected writes in Atlas."
		)


def require_supported_filesystem() -> None:
	probe = subprocess.run(
		["blkid", "-p", "-s", "TYPE", "-o", "value", DEVICE], capture_output=True, text=True
	)
	kind = probe.stdout.strip()
	if probe.returncode not in (0, 2):
		raise RuntimeError("Cannot inspect the original disk. Check device access before repair.")
	if kind != "ext4":
		raise RuntimeError(
			f"Detected {kind or 'unknown layout'}. Guided repair supports whole-disk ext4 only. Use the expert shell for other layouts."
		)


def original_disk_mounts() -> list[str]:
	result = subprocess.run(["findmnt", "-rn", "-S", DEVICE, "-o", "TARGET"], capture_output=True, text=True)
	if result.returncode not in (0, 1) or result.stderr.strip():
		raise RuntimeError("Cannot inspect original-disk mounts. Refusing to continue.")
	return result.stdout.strip().splitlines()


def mount_original(read_only: bool = False) -> None:
	require_supported_filesystem()
	if not read_only:
		require_writable()
	mounts = original_disk_mounts()
	if mounts:
		if mounts != [str(ROOT)]:
			raise RuntimeError("Original disk is mounted elsewhere; unmount it before using guided tools.")
		options = capture("findmnt", "-rn", "-M", str(ROOT), "-o", "OPTIONS").split(",")
		if read_only != ("ro" in options):
			raise RuntimeError(
				"Original disk is mounted with a different access mode. Unmount it before changing modes."
			)
		return
	ROOT.mkdir(parents=True, exist_ok=True)
	if ROOT.is_symlink() or os.path.ismount(ROOT):
		raise RuntimeError("The recovery mount path is occupied.")
	# Do not propagate this mount into service namespaces, where a copy can
	# survive unmount and keep ext4 busy during an offline check or resize.
	run("mount", "--make-rprivate", "/")
	run("mount", "-o", "ro,noload" if read_only else "rw", DEVICE, str(ROOT))


def confirm(message: str) -> None:
	if input(message + " Type YES to continue: ").strip() != "YES":
		raise RuntimeError("Cancelled; no change requested.")


def require_unmounted_original_disk() -> None:
	if original_disk_mounts():
		raise RuntimeError("Unmount the original disk before filesystem checks or growth.")


def operation_running() -> bool:
	return subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0


def start_operation(command: str, arguments: list[str]) -> None:
	if operation_running():
		raise RuntimeError("A managed operation is already running. Use atlas-rescue status.")
	subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True)
	(STATE / "result.json").unlink(missing_ok=True)
	(STATE / "operation.log").write_text("")
	run(
		"systemd-run",
		"--unit=" + UNIT,
		"--collect",
		"--property=Type=exec",
		"--property=StandardOutput=append:" + str(STATE / "operation.log"),
		"--property=StandardError=append:" + str(STATE / "operation.log"),
		"/usr/local/sbin/atlas-rescue",
		"--worker",
		command,
		*arguments,
	)
	print(
		"Started. This operation survives disconnects. Use atlas-rescue status to read progress and its result."
	)


def original_path(relative: str) -> Path:
	path = ROOT / relative
	if not path.resolve().is_relative_to(ROOT.resolve()):
		raise RuntimeError("Installed path escapes the original filesystem: " + relative)
	return path


def account(user: str) -> list[str]:
	if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*\$?", user):
		raise RuntimeError("Invalid local user name.")
	for line in original_path("etc/passwd").read_text().splitlines():
		fields = line.split(":")
		if len(fields) == 7 and fields[0] == user:
			return fields
	raise RuntimeError("That local Linux account does not exist on the original disk.")


def reset_password(user: str, unlock: bool) -> None:
	mount_original()
	fields = account(user)
	if fields[6].endswith(("nologin", "/false")):
		raise RuntimeError(
			"This account has a non-login shell. Password reset will not enable login; choose another account or inspect it in chroot."
		)
	shadow = next(
		(
			line.split(":")[1]
			for line in original_path("etc/shadow").read_text().splitlines()
			if line.split(":")[0] == user
		),
		None,
	)
	if shadow is None:
		raise RuntimeError("Local shadow entry is missing.")
	if shadow.startswith(("!", "*")) and not unlock:
		raise RuntimeError("This account is locked. Retry with --unlock only if you intend to unlock it.")
	print(
		f"Target: {DEVICE} at {ROOT}; installed Linux user: {user}. SSH authentication policy will not change."
	)
	confirm("Reset this account password" + (" and unlock the account?" if unlock else "?"))
	password = _read_password()
	run("chpasswd", "--root", str(ROOT), input=f"{user}:{password}\n", capture_output=True)
	print("Password reset completed. This does not reset a Frappe account password.")


def _read_password() -> str:
	first = getpass.getpass("New password: ")
	second = getpass.getpass("Repeat password: ")
	if not first or first != second or any(char in first for char in "\n\r\x00"):
		raise RuntimeError("Passwords do not match or are invalid.")
	return first


def add_keys(user: str) -> None:
	mount_original()
	fields = account(user)
	home = ROOT / fields[5].lstrip("/")
	if not home.resolve().is_relative_to(ROOT.resolve()):
		raise RuntimeError("Account home escapes the original filesystem.")
	keys = capture("/usr/local/lib/atlas/authorized-keys-command").splitlines()
	if not keys:
		raise RuntimeError("No VM keys were returned by metadata; no key files changed.")
	confirm(f"Add the VM's {len(keys)} SSH keys to installed user {user}?")
	_install_authorized_keys(home, keys, int(fields[2]), int(fields[3]))
	print("SSH keys added; existing keys retained.")


def _install_authorized_keys(home: Path, keys: list[str], user_id: int, group_id: int) -> None:
	directory = home / ".ssh"
	if directory.is_symlink() or (directory / "authorized_keys").is_symlink():
		raise RuntimeError("SSH key paths must not be symbolic links.")
	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	path = directory / "authorized_keys"
	existing = path.read_text().splitlines() if path.exists() else []
	with path.open("a") as stream:
		for key in keys:
			if key not in existing:
				stream.write("\n" + key + "\n")
	os.chmod(directory, 0o700)
	os.chmod(path, 0o600)
	for item in (directory, path):
		os.chown(item, user_id, group_id)


def discover_sites(root: Path) -> tuple[list[SiteInventory], set[Path]]:
	"""Find sites without running installed tenant code."""
	sites: list[SiteInventory] = []
	benches = set()
	for base, directories, files in os.walk(root, followlinks=False):
		directories[:] = _discovery_directories(Path(base), directories)
		if "site_config.json" not in files:
			continue
		site = Path(base)
		if site.parent.name != "sites" or site.name == ".migrate":
			continue
		benches.add(site.parent.parent)
		sites.append(_site_inventory(root, site))
	return sites, benches


def _discovery_directories(base: Path, directories: list[str]) -> list[str]:
	return [
		name
		for name in directories
		if name not in {"proc", "sys", "dev", ".git", "node_modules", "env", ".venv"}
		and not (base / name).is_symlink()
	]


def _site_inventory(root: Path, site: Path) -> SiteInventory:
	components = ["site_config.json", "public/files", "private/files", "private/backups", ".migrate"]
	return {
		"path": str(site.relative_to(root)),
		"present": [name for name in components if (site / name).exists()],
		"missing": [name for name in components if not (site / name).exists()],
	}


def _bench_recovery_paths(root: Path, bench: Path) -> tuple[list[str], list[str]]:
	paths = []
	missing = []
	for name in ("bench.toml", "apps", "sites/apps.txt", "sites/common_site_config.json", "migrations"):
		path = bench / name
		if path.exists():
			paths.append(str(path.relative_to(root)))
		else:
			missing.append(str(path.relative_to(root)))
	common = bench.parent / "common_config.toml"
	if common.exists():
		paths.append(str(common.relative_to(root)))
	return paths, missing


def discover_recovery_files(root: Path) -> RecoveryInventory:
	"""Return paths and completeness evidence without configuration secrets."""
	sites, benches = discover_sites(root)
	paths = {entry["path"] for entry in sites}
	missing = []
	for bench in benches:
		bench_paths, bench_missing = _bench_recovery_paths(root, bench)
		paths.update(bench_paths)
		missing.extend(bench_missing)
	for name in ("etc/os-release", "usr/lib/os-release", "var/lib/dpkg/status", "etc/fstab"):
		if (root / name).exists():
			paths.add(name)
	databases = [
		name
		for name in ("var/lib/mysql", "var/lib/postgresql", "etc/mysql", "etc/postgresql")
		if (root / name).exists()
	]
	paths.update(databases)
	return {
		"sites": sites,
		"paths": sorted(paths),
		"missing": sorted(missing),
		"database_paths": databases,
		"limits": [
			"External databases, volumes and nonstandard data paths are not included automatically.",
			"Raw database files are preservation copies, not validated portable backups.",
			"Symbolic links are preserved; their targets need separate verification and may require separate recovery.",
			"Missing backups or migration artifacts may be normal. Check the report before relying on this export.",
		],
	}


def export_recovery_files(destination: str | Path) -> None:
	mount_original(read_only=True)
	destination = Path(destination).resolve()
	if destination.is_relative_to(ROOT.resolve()) or (
		destination.exists() and destination.stat().st_dev == ROOT.stat().st_dev
	):
		raise RuntimeError("Select a destination outside the original disk.")
	if destination.exists() and any(destination.iterdir()):
		raise RuntimeError("Export destination must be empty.")
	destination.mkdir(parents=True, exist_ok=True, mode=0o700)
	os.chmod(destination, 0o700)
	report = discover_recovery_files(ROOT)
	report.update(complete=False, files=[], symbolic_links=[], special_files=[])
	manifest = destination / "manifest.json"
	manifest.write_text(json.dumps(report, indent=2))
	for relative in report["paths"]:
		# Preserve the final symlink, but never traverse an installed parent link
		# into the rescue environment while reading the source.
		original_path(str(Path(relative).parent))
		target = destination / "data" / relative
		target.parent.mkdir(parents=True, exist_ok=True)
		source = ROOT / relative
		run("rsync", "-aH", "--numeric-ids", "--", str(source), str(target.parent) + "/")
	record_exported_files(destination, report)
	report["complete"] = True
	manifest.write_text(json.dumps(report, indent=2))
	print(
		"Export complete. Configs contain secrets; keep the destination private. Check manifest.json for missing components."
	)


def record_exported_files(destination: Path, report: RecoveryInventory) -> None:
	for base, directories, files in os.walk(destination / "data", followlinks=False):
		for name in directories + files:
			path = Path(base) / name
			if path.is_symlink():
				report["symbolic_links"].append(
					{"path": str(path.relative_to(destination)), "target": os.readlink(path)}
				)
		for name in files:
			path = Path(base) / name
			if path.is_symlink():
				continue
			mode = path.stat().st_mode
			if not stat.S_ISREG(mode):
				report["special_files"].append(
					{"path": str(path.relative_to(destination)), "mode": stat.filemode(mode)}
				)
				continue
			with path.open("rb") as stream:
				digest = hashlib.file_digest(stream, "sha256")
			report["files"].append(
				{
					"path": str(path.relative_to(destination)),
					"bytes": path.stat().st_size,
					"sha256": digest.hexdigest(),
				}
			)


def chroot() -> None:
	mount_original()
	confirm("Open a modifying shell in the installed system?")
	binds = []
	try:
		for name in ("dev", "proc", "sys", "run"):
			target = original_path(name)
			target.mkdir(exist_ok=True)
			run("mount", "--rbind", "/" + name, str(target))
			binds.append(target)
			run("mount", "--make-rslave", str(target))
		subprocess.run(["chroot", str(ROOT), "/bin/bash"])
	finally:
		failed = []
		for target in reversed(binds):
			try:
				run("umount", "-R", str(target))
			except (OSError, subprocess.CalledProcessError):
				failed.append(str(target))
		if failed:
			raise RuntimeError("Could not unmount chroot filesystems: " + ", ".join(failed))


def show_status() -> None:
	print("Original disk access:", access_mode())
	print("Mounts:", ", ".join(original_disk_mounts()) or "unmounted")
	print("Managed operation:", "running" if operation_running() else "not running")
	for name in ("operation.log", "result.json"):
		path = STATE / name
		if path.exists():
			print(path.read_text()[-16000:])


def check_or_resize_filesystem(args: argparse.Namespace) -> None:
	command = args.command
	require_supported_filesystem()
	require_unmounted_original_disk()
	if command == "resize" or args.repair:
		require_writable()
	if not args.worker:
		if command == "resize" or args.repair:
			confirm("Modify the original filesystem? Copy important data out first.")
		start_operation(command, ["--repair"] if args.repair else [])
	else:
		_run_filesystem_repair(command, args.repair)


def _run_filesystem_repair(command: str, repair: bool) -> None:
	if command == "fsck":
		result = subprocess.run(["e2fsck", "-f", "-p" if repair else "-n", DEVICE])
		if result.returncode not in (0, 1, 2):
			raise RuntimeError(
				f"Filesystem check returned {result.returncode}; inspect the log before further repair."
			)
	else:
		run("resize2fs", DEVICE)


def show_installed_logs() -> None:
	mount_original(read_only=True)
	journal = original_path("var/log/journal")
	if journal.is_dir():
		run("journalctl", "--directory=" + str(journal), "--no-pager", "-n", "200")
	else:
		print("No persistent journal found. Recent installed-system text logs:")
		for name in ("syslog", "auth.log", "kern.log", "cloud-init.log", "cloud-init-output.log"):
			path = original_path("var/log/" + name)
			if path.is_file():
				print("\n--- " + name + " ---")
				run("tail", "-n", "80", "--", str(path))


def show_disk_usage() -> None:
	mount_original(read_only=True)
	run("df", "-h", str(ROOT))
	run("df", "-i", str(ROOT))
	run("du", "-x", "-h", "--max-depth=1", str(ROOT))


def copy_recovery_files(args: argparse.Namespace) -> None:
	mount_original(read_only=True)
	if args.destination:
		if args.worker:
			export_recovery_files(args.destination)
		else:
			confirm(
				"Copy configs, attachments, app code, backups and local database files to the selected destination? It must be off the original disk and will contain secrets."
			)
			start_operation("copy-out", ["--destination", args.destination])
	else:
		print(json.dumps(discover_recovery_files(ROOT), indent=2))
		print(
			"Use copy-out --destination PATH on a separate mounted disk or remote filesystem. Or pull files with scp/rsync from your workstation. Files on the rescue disk are deleted on exit."
		)


def open_expert_shell() -> None:
	print(
		"Expert shell. Managed-operation locks do not protect arbitrary commands. Original-disk access:",
		access_mode(),
	)
	subprocess.run(["/bin/bash"])


def exit_rescue(rollback: bool) -> None:
	if operation_running():
		raise RuntimeError("A managed operation is running. Wait for its result before exiting.")
	if rollback:
		print(
			"Use Atlas > Rescue Mode > Roll back. This also restores writes made during normal boot; no keep-exit was requested."
		)
	else:
		confirm("Keep original-disk changes and discard the rescue disk? Copy rescue-disk files out first.")
		if original_disk_mounts():
			run("umount", "-R", str(ROOT))
		run("systemctl", "reboot")


def execute(args: argparse.Namespace) -> None:
	actions = {
		"status": show_status,
		"mount": lambda: mount_original(args.read_only or access_mode() not in {"protected", "unprotected"}),
		"umount": lambda: run("umount", str(ROOT)),
		"fsck": lambda: check_or_resize_filesystem(args),
		"resize": lambda: check_or_resize_filesystem(args),
		"passwd": lambda: reset_password(args.user, args.unlock),
		"add-keys": lambda: add_keys(args.user),
		"chroot": chroot,
		"logs": show_installed_logs,
		"usage": show_disk_usage,
		"copy-out": lambda: copy_recovery_files(args),
		"shell": open_expert_shell,
		"exit": lambda: exit_rescue(args.rollback),
	}
	actions[args.command]()


def _parse_arguments() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Offline repair tools for the original Atlas disk.")
	parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
	parser.add_argument(
		"command",
		choices=[
			"status",
			"mount",
			"umount",
			"fsck",
			"resize",
			"passwd",
			"add-keys",
			"chroot",
			"logs",
			"usage",
			"copy-out",
			"shell",
			"exit",
		],
	)
	parser.add_argument("user", nargs="?")
	parser.add_argument("--read-only", action="store_true")
	parser.add_argument("--repair", action="store_true")
	parser.add_argument("--unlock", action="store_true")
	parser.add_argument("--rollback", action="store_true")
	parser.add_argument("--destination")
	args = parser.parse_args()
	if os.geteuid() != 0:
		parser.error("Run with sudo or as root in rescue.")
	if args.command in {"passwd", "add-keys"} and not args.user:
		parser.error("Select an installed Linux user explicitly.")
	return args


def _execute_locked(args: argparse.Namespace, lock: TextIO) -> None:
	if args.command in {"status", "shell"}:
		execute(args)
		return
	try:
		fcntl.flock(lock, fcntl.LOCK_EX if args.worker else fcntl.LOCK_EX | fcntl.LOCK_NB)
	except BlockingIOError:
		raise RuntimeError("Another managed action is running. Use atlas-rescue status.")
	execute(args)
	if args.worker:
		(STATE / "result.json").write_text(json.dumps({"command": args.command, "success": True}))


def main() -> None:
	args = _parse_arguments()
	STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
	with (STATE / "operation.lock").open("w") as lock:
		try:
			_execute_locked(args, lock)
		except (OSError, RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt, EOFError) as error:
			# Subprocess errors must not disclose password input.
			message = (
				str(error)
				if not isinstance(error, subprocess.CalledProcessError)
				else f"Command failed with exit status {error.returncode}."
			)
			print(message or "Cancelled", file=sys.stderr)
			if args.worker:
				(STATE / "result.json").write_text(
					json.dumps({"command": args.command, "success": False, "error": message})
				)
			sys.exit(1)


if __name__ == "__main__":
	main()
