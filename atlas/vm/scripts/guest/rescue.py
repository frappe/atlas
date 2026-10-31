#!/usr/bin/python3

import argparse
import fcntl
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO
from urllib.request import Request, urlopen

DEVICE = "/dev/vdb"
ROOT = Path("/mnt/original")
STATE = Path("/var/lib/atlas-rescue")
UNIT = "atlas-rescue-operation"
assume_yes = False


def run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
	return subprocess.run(args, check=True, text=True, **kwargs)


def capture(*args: str) -> str:
	return run(*args, capture_output=True).stdout.strip()


def read_vm_keys() -> list[str]:
	"""Read every VM key, and fail when one is missing so that a key file never gets a partial list."""
	base = "http://169.254.169.254/latest/"
	token_request = Request(base + "api/token", method="PUT", headers={"X-metadata-token-ttl-seconds": "60"})
	token = urlopen(token_request, timeout=3).read().decode()

	def read(path: str) -> str:
		request = Request(base + "meta-data/" + path, headers={"X-metadata-token": token})
		return urlopen(request, timeout=3).read().decode().strip()

	entries = read("public-keys/").splitlines()
	return [read(f"public-keys/{entry.partition('=')[0]}/openssh-key") for entry in entries if entry]


def require_writable() -> None:
	if capture("blockdev", "--getro", DEVICE) != "0":
		raise RuntimeError("The VM disk is read-only.")


def require_supported_filesystem() -> None:
	probe = subprocess.run(
		["blkid", "-p", "-s", "TYPE", "-o", "value", DEVICE], capture_output=True, text=True
	)
	kind = probe.stdout.strip()
	if probe.returncode not in (0, 2):
		raise RuntimeError("Cannot inspect the VM disk. Check device access before repair.")
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
			raise RuntimeError("VM disk is mounted elsewhere; unmount it before using guided tools.")
		options = capture("findmnt", "-rn", "-M", str(ROOT), "-o", "OPTIONS").split(",")
		if not read_only and "ro" in options:
			raise RuntimeError("VM disk is mounted read-only. Unmount it before writing to it.")
		return
	ROOT.mkdir(parents=True, exist_ok=True)
	if ROOT.is_symlink() or os.path.ismount(ROOT):
		raise RuntimeError("The recovery mount path is occupied.")
	# Do not propagate this mount into service namespaces, where a copy can
	# survive unmount and keep ext4 busy during an offline check or resize.
	run("mount", "--make-rprivate", "/")
	run("mount", "-o", "ro,noload" if read_only else "rw", DEVICE, str(ROOT))


def confirm(message: str) -> None:
	if assume_yes:
		return
	if input(message + " Type YES to continue: ").strip() != "YES":
		raise RuntimeError("Cancelled; no change requested.")


def require_unmounted_original_disk() -> None:
	if original_disk_mounts():
		raise RuntimeError("Unmount the VM disk before filesystem checks or growth.")
	release_other_namespaces()


def other_namespace_mounts() -> dict[int, list[str]]:
	"""Return the VM disk mount points that other mount namespaces still hold, by one process of each."""
	own_namespace = os.readlink("/proc/self/ns/mnt")
	seen = {own_namespace}
	holders: dict[int, list[str]] = {}
	for entry in os.listdir("/proc"):
		if not entry.isdigit():
			continue
		try:
			namespace = os.readlink(f"/proc/{entry}/ns/mnt")
			if namespace in seen:
				continue
			seen.add(namespace)
			lines = Path(f"/proc/{entry}/mountinfo").read_text().splitlines()
		except OSError:
			continue
		targets = [line.split()[4] for line in lines if f" {DEVICE} " in line]
		if targets:
			holders[int(entry)] = targets
	return holders


def release_other_namespaces() -> None:
	"""A service that started during a mount keeps its own copy, and the filesystem stays live until it goes."""
	for process_id, targets in other_namespace_mounts().items():
		for target in reversed(targets):
			subprocess.run(["nsenter", "-t", str(process_id), "-m", "umount", target], capture_output=True)
	if other_namespace_mounts():
		raise RuntimeError("A background service still uses the VM disk. Restart the VM, then try again.")


def unmount_original() -> None:
	run("umount", str(ROOT))
	release_other_namespaces()


def is_operation_running() -> bool:
	return subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0


def start_operation(command: str, arguments: list[str]) -> None:
	if is_operation_running():
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
		capture_output=True,
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
	for line in original_path("etc/passwd").read_text(errors="replace").splitlines():
		fields = line.split(":")
		if len(fields) == 7 and fields[0] == user and fields[2].isdigit() and fields[3].isdigit():
			return fields
	raise RuntimeError("That local Linux account does not exist on the VM disk.")


def list_users() -> None:
	"""Print each installed login account and whether it is locked."""
	mount_original(read_only=True)
	locked = set()
	for line in original_path("etc/shadow").read_text(errors="replace").splitlines():
		fields = line.split(":")
		if len(fields) > 1 and fields[1].startswith(("!", "*")):
			locked.add(fields[0])
	for line in original_path("etc/passwd").read_text(errors="replace").splitlines():
		fields = line.split(":")
		if (
			len(fields) == 7
			and fields[2].isdigit()
			and fields[3].isdigit()
			and (fields[2] == "0" or int(fields[2]) >= 1000)
			and not fields[6].endswith(("nologin", "/false", "/sync"))
		):
			print(fields[0], "locked" if fields[0] in locked else "active", sep="\t")


def follow_operation() -> None:
	"""Print the log of the managed operation until it ends, then report its result."""
	log = STATE / "operation.log"
	position = 0
	while True:
		running = is_operation_running()
		if log.exists():
			with log.open() as stream:
				stream.seek(position)
				print(stream.read(), end="", flush=True)
				position = stream.tell()
		if not running:
			break
		time.sleep(1)
	path = STATE / "result.json"
	result = json.loads(path.read_text()) if path.exists() else {"success": False}
	if not result["success"]:
		raise RuntimeError("This did not finish successfully. See the output above.")
	print("Finished successfully.")


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
			for line in original_path("etc/shadow").read_text(errors="replace").splitlines()
			if line.startswith(user + ":")
		),
		None,
	)
	if shadow is None:
		raise RuntimeError("Local shadow entry is missing.")
	if shadow.startswith(("!", "*")) and not unlock:
		raise RuntimeError("This account is locked. Retry with --unlock only if you intend to unlock it.")
	print(f"Set a new password for {user}. SSH settings do not change.")
	confirm("Reset this account password" + (" and unlock the account?" if unlock else "?"))
	password = _read_password()
	run("chpasswd", "--root", str(ROOT), input=f"{user}:{password}\n", capture_output=True)
	print("Password reset completed.")


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
	keys = read_vm_keys()
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


def chroot() -> None:
	mount_original()
	confirm("Open a modifying shell in the installed system?")
	names = ("dev", "proc", "sys", "run")
	for name in names:
		original_path(name).mkdir(exist_ok=True)
	# A private namespace removes these mounts when the session ends, even after a disconnect.
	script = 'for name in "$@"; do mount --rbind "/$name" "$root/$name" || exit 1; done; exec chroot "$root" /bin/bash'
	subprocess.run(
		[
			"unshare",
			"--mount",
			"--propagation",
			"private",
			"env",
			f"root={ROOT}",
			"sh",
			"-c",
			script,
			"sh",
			*names,
		]
	)


def show_status() -> None:
	print("VM disk read-only:", capture("blockdev", "--getro", DEVICE))
	print("Mounts:", ", ".join(original_disk_mounts()) or "unmounted")
	print("Managed operation:", "running" if is_operation_running() else "not running")
	for name in ("operation.log", "result.json"):
		path = STATE / name
		if path.exists():
			print(path.read_text()[-16000:])


def check_or_resize_filesystem(command: str, *, repair: bool, worker: bool) -> None:
	require_supported_filesystem()
	require_unmounted_original_disk()
	if command == "resize" or repair:
		require_writable()
	if not worker:
		if command == "resize" or repair:
			confirm("Modify the original filesystem? Copy important data out first.")
		start_operation(command, ["--repair"] if repair else [])
	else:
		_run_filesystem_repair(command, repair)


def _run_filesystem_repair(command: str, repair: bool) -> None:
	# -p stops at damage that it cannot fix safely, so a repair accepts every fix.
	mode = "-y" if repair else "-p" if command == "resize" else "-n"
	# resize2fs refuses a filesystem that was mounted after its last forced check.
	result = subprocess.run(["e2fsck", "-f", mode, DEVICE])
	if result.returncode not in (0, 1, 2):
		raise RuntimeError(
			f"Filesystem check returned {result.returncode}; inspect the log before further repair."
		)
	if command == "resize":
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


def show_boot_errors() -> None:
	mount_original(read_only=True)
	journal = original_path("var/log/journal")
	if not journal.is_dir():
		print("The installed system has no persistent journal. Use Read logs for its text logs.")
		return
	errors = capture("journalctl", "--directory=" + str(journal), "-b", "-0", "-p", "err", "--no-pager", "-q")
	print(errors or "The last boot of the installed system logged no errors.")


def edit_fstab() -> None:
	mount_original()
	fstab = original_path("etc/fstab")
	# Keep the first backup, so that a second edit cannot replace the working file.
	backup = fstab.with_name("fstab.atlas-rescue-backup")
	if fstab.exists() and not backup.exists():
		shutil.copy2(fstab, backup)
	if not fstab.exists():
		print("This system has no /etc/fstab. Saving the file creates it.")
	subprocess.run([shutil.which("nano") or "vi", str(fstab)])
	if not fstab.exists():
		return
	if backup.exists():
		print("The file before your first edit is /etc/fstab.atlas-rescue-backup.")
	# Run inside the installed system, so that its own paths are the ones checked.
	result = subprocess.run(["chroot", str(ROOT), "findmnt", "--verify"], capture_output=True, text=True)
	errors = [line.strip() for line in result.stdout.splitlines() if "[E]" in line]
	if result.stderr.strip():
		errors.append(result.stderr.strip())
	if errors:
		print("Check these before you restart:\n" + "\n".join(errors))
	elif result.returncode > 1:
		print(f"Could not check /etc/fstab (exit status {result.returncode}). Check it before you restart.")
	else:
		print("No mistakes found in /etc/fstab.")


def disable_firewall() -> None:
	mount_original()
	confirm("Turn off the firewall of the installed system at its next boot?")
	changed = []
	ufw = original_path("etc/ufw/ufw.conf")
	if ufw.is_file() and re.search(r"^ENABLED=yes", ufw.read_text(), re.MULTILINE):
		ufw.write_text(re.sub(r"^ENABLED=yes", "ENABLED=no", ufw.read_text(), flags=re.MULTILINE))
		changed.append("ufw")
	for service in ("firewalld", "nftables", "iptables", "netfilter-persistent"):
		state = subprocess.run(
			["systemctl", "--root", str(ROOT), "is-enabled", service], capture_output=True, text=True
		)
		if state.stdout.strip() == "enabled":
			run("systemctl", "--root", str(ROOT), "disable", service, capture_output=True)
			changed.append(service)
	print("Turned off: " + ", ".join(changed) if changed else "No enabled firewall was found.")
	print("The Atlas firewall of this VM does not change.")


def show_disk_usage() -> None:
	mount_original(read_only=True)
	run("df", "-h", str(ROOT))
	run("df", "-i", str(ROOT))
	run("du", "-x", "-h", "--max-depth=1", str(ROOT))


def open_expert_shell() -> None:
	print("Expert shell. Commands can modify the VM disk. Take a snapshot before repair.")
	subprocess.run(["/bin/bash"])


def execute(
	command: str,
	*,
	read_only: bool = False,
	repair: bool = False,
	worker: bool = False,
	user: str | None = None,
	unlock: bool = False,
) -> None:
	actions = {
		"status": show_status,
		"users": list_users,
		"wait": follow_operation,
		"mount": lambda: mount_original(read_only),
		"umount": unmount_original,
		"fsck": lambda: check_or_resize_filesystem(command, repair=repair, worker=worker),
		"resize": lambda: check_or_resize_filesystem(command, repair=repair, worker=worker),
		"passwd": lambda: reset_password(user, unlock),
		"add-keys": lambda: add_keys(user),
		"chroot": chroot,
		"logs": show_installed_logs,
		"boot-errors": show_boot_errors,
		"edit-fstab": edit_fstab,
		"disable-firewall": disable_firewall,
		"usage": show_disk_usage,
		"shell": open_expert_shell,
	}
	actions[command]()


def _parse_arguments() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Offline repair tools for the original Atlas disk.")
	parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
	parser.add_argument(
		"command",
		choices=[
			"status",
			"users",
			"wait",
			"mount",
			"umount",
			"fsck",
			"resize",
			"passwd",
			"add-keys",
			"chroot",
			"logs",
			"boot-errors",
			"edit-fstab",
			"disable-firewall",
			"usage",
			"shell",
		],
	)
	parser.add_argument("user", nargs="?")
	parser.add_argument("--read-only", action="store_true")
	parser.add_argument("--repair", action="store_true")
	parser.add_argument("--unlock", action="store_true")
	parser.add_argument("--yes", action="store_true", help="Skip the confirmation question.")
	args = parser.parse_args()
	if os.geteuid() != 0:
		parser.error("Run with sudo or as root in rescue.")
	if args.command in {"passwd", "add-keys"} and not args.user:
		parser.error("Select an installed Linux user explicitly.")
	return args


def _execute_locked(args: argparse.Namespace, lock: TextIO) -> None:
	if args.command in {"status", "shell", "wait"}:
		execute(**vars(args))
		return
	try:
		fcntl.flock(lock, fcntl.LOCK_EX if args.worker else fcntl.LOCK_EX | fcntl.LOCK_NB)
	except BlockingIOError:
		raise RuntimeError("Another managed action is running. Use atlas-rescue status.")
	execute(**vars(args))
	if args.worker:
		(STATE / "result.json").write_text(json.dumps({"command": args.command, "success": True}))


def main() -> None:
	global assume_yes
	args = _parse_arguments()
	assume_yes = args.yes
	del args.yes
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
