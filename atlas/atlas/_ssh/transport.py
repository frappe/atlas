"""SSH/SCP subprocess plumbing.

This module hides all the system-`ssh`/`scp` invocations behind small helpers.
Higher layers (runner.py) compose these to drive Task lifecycles without
knowing anything about ssh option strings or tempfile lifetimes for keys.
"""

import dataclasses
import io
import os
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import frappe

from atlas.atlas._ssh._quote import substitute

KNOWN_HOSTS_PATH = Path("~/.atlas/known_hosts").expanduser()
REMOTE_STAGING_DIRECTORY = "/tmp/atlas"

# Connection-multiplexing control sockets. A Task opens 2+ ssh/scp connections to
# the same host back-to-back (stage the script, then run it); without sharing,
# each pays a full TCP+SSH handshake (the dominant cost of a provision over a
# remote droplet — observed ~1.5s+ per handshake, several per Task). With a
# master, the first connection does the handshake and every later ssh/scp to the
# same (user, host, port) rides the existing socket. `%C` is ssh's hash of those
# three, so concurrent Tasks to *different* servers get distinct sockets and
# never collide; concurrent Tasks to the *same* server safely share one master
# (ssh multiplexes channels). ControlPersist keeps the master alive briefly after
# the last channel closes so the very next Task reuses it too, then it self-reaps.
CONTROL_PATH_DIRECTORY = Path("~/.atlas/cm").expanduser()

SSH_OPTIONS = [
	"-o",
	"StrictHostKeyChecking=accept-new",
	"-o",
	f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
	"-o",
	"BatchMode=yes",
	"-o",
	"ConnectTimeout=30",
	# Connection sharing — see CONTROL_PATH_DIRECTORY above. ControlMaster=auto:
	# reuse a master if one is live, else become it. The control dir is created by
	# _ensure_known_hosts_directory() (which every ssh/scp already calls) before
	# any connection, so ssh can always bind the master socket.
	"-o",
	"ControlMaster=auto",
	"-o",
	f"ControlPath={CONTROL_PATH_DIRECTORY}/%C",
	"-o",
	"ControlPersist=60s",
	# Keepalive so a long-running command (the golden bake's `bench init` +
	# `new-site`, minutes of apt/clone/node) survives a brief network blip yet a
	# genuinely half-open connection DIES instead of hanging to the Task timeout.
	# ConnectTimeout only bounds the initial handshake, not a stalled session;
	# without these a wedged mid-command SSH blocks for the full `timeout_seconds`
	# (observed: a 1800s bake hang on a dead-but-not-closed connection). 15s x 4
	# missed probes ≈ 60s to give up — fast enough to fail loud, slack enough not
	# to false-trip on a slow remote step.
	"-o",
	"ServerAliveInterval=15",
	"-o",
	"ServerAliveCountMax=4",
]


# Per-probe ConnectTimeout for wait_for_ssh's readiness poll. The shared
# SSH_OPTIONS default (30s) is right for established hosts, but a guest still
# booting off the golden snapshot can take longer to start answering on :22; a
# longer single connect attempt avoids declaring it not-ready prematurely. Scoped
# to this step only via run_ssh(extra_options=...), so other SSH calls keep 30s.
PROBE_CONNECT_TIMEOUT_SECONDS = 90


@dataclasses.dataclass(frozen=True)
class Connection:
	host: str
	ssh_private_key: str
	user: str = "root"


def _bracket_host(host: str) -> str:
	"""Wrap an IPv6 literal in brackets for scp's `host:path` syntax; leave IPv4
	and hostnames untouched. A v6 literal is detected by the presence of a colon
	(hostnames and v4 never contain one); already-bracketed hosts pass through."""
	if ":" in host and not host.startswith("["):
		return f"[{host}]"
	return host


def wait_for_ssh(connection: Connection, timeout_seconds: int = 300, poll_seconds: int = 5) -> None:
	"""Poll the host until SSH accepts a `true` command, or raise.

	A freshly-booted guest (a clone off the golden snapshot) often isn't serving
	sshd yet when the first probe fires. That shows up two ways, BOTH of which mean
	"not ready, keep polling" — not "fail the whole provision":
	  - sshd is up but rejecting (or the host key dance fails) -> non-zero exit code;
	  - sshd isn't listening yet, so the TCP connect hangs until the per-probe ssh
	    timeout fires -> subprocess.TimeoutExpired.
	The second case is the subtle one: the connect hangs for the whole ConnectTimeout
	before raising. We override ConnectTimeout to 90s for THIS probe (the shared
	default is 30s) so a slow-booting guest gets a longer single attempt rather than
	being declared not-ready prematurely; the per-probe subprocess timeout is raised
	to match so it doesn't kill ssh before its own connect attempt finishes. Either
	signal is retried until the real `deadline`, then raised."""
	_ensure_known_hosts_directory()
	forget_host(connection.host)
	deadline = time.monotonic() + timeout_seconds
	with ssh_key_file(connection.ssh_private_key) as key_path:
		while True:
			try:
				_, _, exit_code = run_ssh(
					connection,
					key_path,
					"true",
					timeout_seconds=PROBE_CONNECT_TIMEOUT_SECONDS,
					extra_options=[f"ConnectTimeout={PROBE_CONNECT_TIMEOUT_SECONDS}"],
				)
				ready = exit_code == 0
			except subprocess.TimeoutExpired:
				ready = False
			if ready:
				return
			if time.monotonic() >= deadline:
				raise frappe.ValidationError(f"SSH to {connection.host} not ready after {timeout_seconds}s")
			time.sleep(poll_seconds)


def upload_files(connection: Connection, files: list[tuple[str, str]]) -> None:
	"""Upload files to the server. `files` is (local_path, remote_path) pairs.

	Not recorded as a Task. All files ship in ONE tar-over-ssh stream instead of
	one scp per file: a tar archive is built in-process (each member named by its
	remote path) and piped to a single remote `tar -x` that unpacks it into place.
	One SSH connection, one round trip — a 54-file sync that was ~70s of per-file
	scp handshakes lands in a couple of seconds. Remote parent directories are
	created by tar as it extracts, so no separate mkdir pass.

	Paths are made archive-relative (leading `/` stripped) and extracted under `/`
	via `tar -C /`, so each `remote_path` lands exactly where it was mapped
	regardless of the local path it came from.
	"""
	if not files:
		return

	_ensure_known_hosts_directory()
	print(
		str(frappe.utils.nowtime()),
		f"Uploading {len(files)} files to {connection.host} in one tar stream",
	)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		_upload_via_tar(connection, key_path, files)


def _build_tar_archive(files: list[tuple[str, str]]) -> bytes:
	"""Pack `files` into a GZIPPED tar archive in memory, each member stored under
	its remote path made relative (leading `/` stripped) so a remote `tar -xz -C /`
	drops it at the absolute destination. Built with the stdlib `tarfile` — not the
	local `tar` binary — so the client side needs no GNU-only features (macOS ships
	BSD tar, which lacks `--transform`); the archive layout is identical regardless
	of the operator's OS.

	Compression is the point, not just tidiness: over a high-latency link the raw
	stdin stream is what dominates — a 490KB uncompressed tar of the durable scripts
	took ~4-7s to push (even piping it to `cat >/dev/null` on the host took ~4s),
	while gzip shrinks it ~3.7x (490KB -> ~135KB, Python source compresses hugely)
	and the same push lands in ~1.5s. The bottleneck was bytes-on-the-wire, so fewer
	bytes is the fix.

	Ownership is normalized to root:root: the connection runs as root, and tar
	extracting as root restores the archived uid/gid — so without this the files
	would land owned by the operator's LOCAL uid (e.g. 501:staff on macOS) rather
	than root, unlike the scp path this replaces."""

	def _root_owned(info: tarfile.TarInfo) -> tarfile.TarInfo:
		info.uid = info.gid = 0
		info.uname = info.gname = "root"
		return info

	buffer = io.BytesIO()
	with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
		for local, remote in files:
			archive.add(local, arcname=remote.lstrip("/"), recursive=False, filter=_root_owned)
	return buffer.getvalue()


def _upload_via_tar(connection: Connection, key_path: str, files: list[tuple[str, str]]) -> None:
	"""Stream `files` to the host as a single gzipped tar archive over one SSH
	connection.

	The archive is assembled + compressed locally with stdlib `tarfile` and piped to
	one remote `tar -xz -C /`, which decompresses and unpacks each member to its
	absolute destination, creating parent directories as it goes. One connection,
	one round trip, and the compressed payload keeps the transfer (the real cost
	over a remote link) small. `-m` skips restoring mtimes (irrelevant for code) to
	avoid clock-skew warnings."""
	archive = _build_tar_archive(files)
	ssh_args = [
		"ssh",
		"-i",
		key_path,
		*SSH_OPTIONS,
		f"{connection.user}@{connection.host}",
		"tar -xz -m -f - -C /",
	]
	result = subprocess.run(
		ssh_args,
		input=archive,
		capture_output=True,
		timeout=600,
		check=False,
	)
	if result.returncode != 0:
		stderr = result.stderr.decode(errors="replace")
		stdout = result.stdout.decode(errors="replace")
		raise frappe.ValidationError(
			f"tar upload to {connection.host} failed: {stderr[-500:] or stdout[-500:]}"
		)


def run_ssh(
	connection: Connection,
	key_path: str,
	remote_command: str,
	*params: object,
	timeout_seconds: int,
	stdin: str | None = None,
	extra_options: list[str] | None = None,
) -> tuple[str, str, int]:
	"""Run one remote command over SSH. `remote_command` reads like a shell line; any
	interpolated values go through `{}` placeholders that are auto-quoted (`substitute`)
	so each becomes exactly one token to the REMOTE shell — the same author syntax as the
	host `run()`. Unlike `run()`, the result is NOT shlex.split: `remote_command` is the
	command line the remote sshd hands to the remote shell, so it must stay a STRING with
	the quoted holes intact (Trap 3).

	    run_ssh(conn, key, "test -f {}", path, timeout_seconds=60)

	`timeout_seconds` is keyword-only (it follows `*params`). `stdin`, if given, is piped
	to the remote command's stdin — the path the proxy control plane uses to stream a map
	body to a guest's `curl --unix-socket … --data-binary @-` (design §7.3), without first
	staging a file on the guest.

	`extra_options` are appended after SSH_OPTIONS as additional `-o key=value`
	flags. ssh honours the LAST occurrence of a repeated option, so a caller can
	override a default (e.g. ConnectTimeout) for one step without mutating the
	shared SSH_OPTIONS."""
	if params:
		remote_command = substitute(remote_command, params)
	# StrictHostKeyChecking=accept-new must WRITE the new host key into
	# ~/.atlas/known_hosts, so the parent dir has to exist — ensure it here so no
	# caller can forget (cheap + idempotent; the guest control plane in proxy.py
	# SSHes without going through the runner that used to do this).
	_ensure_known_hosts_directory()
	override_options: list[str] = []
	for option in extra_options or []:
		override_options += ["-o", option]
	args = [
		"ssh",
		"-i",
		key_path,
		*SSH_OPTIONS,
		*override_options,
		f"{connection.user}@{connection.host}",
		remote_command,
	]
	result = subprocess.run(
		args,
		input=stdin,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
	)
	return result.stdout, result.stderr, result.returncode


def run_detached(
	connection: Connection,
	key_path: str,
	remote_command: str,
	*,
	log_path: str,
	done_path: str,
	overall_timeout_seconds: int = 1800,
	poll_seconds: int = 10,
	on_log: Callable[[str], None] | None = None,
) -> tuple[str, str, int]:
	"""Run a LONG remote command detached from the SSH session, then poll for it.

	Returns (stdout, stderr, exit_code) like run_ssh — stdout is the captured log,
	exit_code the command's real exit status. Raises on the overall timeout (the
	command genuinely overran), which is distinct from a dropped poll (retried).

	Why detach: a multi-minute guest build (a golden bench bake, an nginx+luajit
	compile — 10-20 min) run as a foreground child of one SSH session ties its life
	to that connection, so a single "Connection reset by peer" mid-build SIGHUPs it
	and kills the build (observed at ~162s). `setsid nohup` frees it from the
	session, tees output to `log_path`, and stamps the exit code into `done_path` on
	completion; we then poll for that marker over SHORT, independently-retried SSH
	calls. A network blip now fails one poll (retried), never the build itself.
	Callers pass distinct log/done paths so concurrent builds don't collide.

	`on_log`, if given, turns the marker poll into a LIVE TAIL: each poll reads the
	bytes the build appended to `log_path` since the previous read (tracked by a
	byte offset, fetched with `tail -c +<offset>`) and hands them to the callback —
	the seam the proxy/bench bake uses to stream output onto the Task row while it
	runs, instead of only reading the whole log once on completion (spec/22). It is
	one extra short SSH call per poll and is wholly opt-in: with `on_log=None` this
	function makes the exact same calls it always did, so every other caller is
	untouched. The callback is best-effort — it must not raise; a slow or failing
	sink should never kill the build it is only observing."""
	# Fresh markers, then launch under setsid+nohup. `sh -c` so the redirect + the
	# exit-code stamp run in the detached shell; the trailing write captures the
	# command's own exit status ($?). The detached shell body is a single {} param so
	# it reaches the outer remote shell as ONE token; log/done paths inside it are
	# quoted via substitute too, so a path with a space survives the inner shell.
	# `remote_command` is concatenated (NOT templated) so a literal `{}` in it is never
	# mistaken for a placeholder.
	inner = remote_command + " > " + substitute("{} 2>&1; echo $? > {}", (log_path, done_path))
	launch = substitute(
		"rm -f {} {}; setsid nohup sh -c {} >/dev/null 2>&1 < /dev/null &",
		(log_path, done_path, inner),
	)
	run_ssh(connection, key_path, launch, timeout_seconds=60)

	# Byte offset of the next unread log byte. `tail -c +N` is 1-indexed (`+1` is
	# the whole file), so we start at 1 and advance by the length of each chunk we
	# read. Tracking an offset (not re-reading the whole log) keeps each poll cheap
	# and the appends non-overlapping, even on a multi-MB bake log.
	offset = 1
	deadline = time.monotonic() + overall_timeout_seconds
	while time.monotonic() < deadline:
		time.sleep(poll_seconds)
		# Short poll: has the marker appeared? A dropped poll just retries next loop.
		try:
			done, _stderr, _code = run_ssh(
				connection, key_path, "cat {} 2>/dev/null || true", done_path, timeout_seconds=30
			)
		except Exception:
			continue  # transient SSH failure — keep polling, the build runs on
		if on_log is not None:
			offset = _stream_log_tail(connection, key_path, log_path, offset, on_log)
		if done.strip():
			exit_code = int(done.strip())
			log, _e, _c = run_ssh(
				connection, key_path, "cat {} 2>/dev/null || true", log_path, timeout_seconds=120
			)
			if on_log is not None:
				# Final drain: emit anything written between the last tail and the
				# done-marker so the streamed view ends byte-for-byte equal to `log`.
				_stream_log_tail(connection, key_path, log_path, offset, on_log)
			return log, "", exit_code
	raise frappe.ValidationError(
		f"Detached command on {connection.host} did not finish within {overall_timeout_seconds}s (still running)"
	)


def _stream_log_tail(
	connection: Connection,
	key_path: str,
	log_path: str,
	offset: int,
	on_log: Callable[[str], None],
) -> int:
	"""Read the bytes appended to `log_path` since `offset` and hand them to
	`on_log`; return the new offset. A transient SSH failure or a failing sink
	leaves the offset unchanged so the next poll re-reads the same window — the
	build is never disturbed by its own observer."""
	try:
		tail, _stderr, code = run_ssh(
			connection,
			key_path,
			"tail -c +{} {} 2>/dev/null || true",
			offset,
			log_path,
			timeout_seconds=30,
		)
	except Exception:
		return offset  # transient SSH failure — retry the same window next poll
	if code != 0 or not tail:
		return offset
	try:
		on_log(tail)
	except Exception:
		# Best-effort: a sink that throws must not advance the offset (so nothing is
		# silently dropped) and must not propagate (so it can't kill the build).
		frappe.logger("atlas").warning(f"task log sink raised on {connection.host}; retrying chunk next poll")
		return offset
	return offset + len(tail.encode("utf-8", "surrogatepass"))


def run_scp(
	connection: Connection,
	key_path: str,
	local_path: str,
	remote_path: str,
	timeout_seconds: int,
) -> None:
	_ensure_known_hosts_directory()
	# scp's `host:path` form splits on the first colon, so an IPv6 literal host
	# (e.g. a guest's /128) must be bracketed — `user@[2400:...]:/path` — or scp
	# mangles the address. ssh doesn't need this (it takes a bare v6 as the host),
	# so the bracketing lives only here. _bracket_host is a no-op for v4/hostnames.
	args = [
		"scp",
		"-i",
		key_path,
		*SSH_OPTIONS,
		local_path,
		f"{connection.user}@{_bracket_host(connection.host)}:{remote_path}",
	]
	result = subprocess.run(
		args,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
	)
	if result.returncode != 0:
		raise frappe.ValidationError(f"scp {local_path} -> {remote_path} failed: {result.stderr}")


@contextmanager
def ssh_key_file(private_key: str):
	"""Write the SSH private key to a 0600 tempfile; delete it on exit."""
	handle = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="atlas-ssh-", suffix=".key")
	try:
		os.chmod(handle.name, 0o600)
		key = private_key if private_key.endswith("\n") else private_key + "\n"
		handle.write(key)
		handle.flush()
		handle.close()
		yield handle.name
	finally:
		try:
			os.unlink(handle.name)
		except OSError:
			pass


def _ensure_known_hosts_directory() -> None:
	parent = KNOWN_HOSTS_PATH.parent
	if not parent.exists():
		parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	# The ControlPath dir must exist before ssh can bind a master socket there;
	# fold it into the one ensure-helper every ssh/scp already calls so no call
	# site can forget it. Same 0700 as known_hosts — these sockets gate host access.
	if not CONTROL_PATH_DIRECTORY.exists():
		CONTROL_PATH_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)


def forget_host(host: str) -> None:
	"""Drop any cached host key for `host` from `~/.atlas/known_hosts`.

	The provider recycles public IPs: a new VM can land on an address a terminated
	VM held, whose host key we already pinned. `StrictHostKeyChecking=accept-new`
	does NOT cover that case — it accepts an *unknown* host, but a *changed* key for
	a known host is a hard MITM failure ("REMOTE HOST IDENTIFICATION HAS CHANGED"),
	which wedges every SSH to the recycled IP until someone runs `ssh-keygen -R` by
	hand (memory: real-provision-traps #1). So `wait_for_ssh` — the first SSH any
	freshly-(re)created VM gets — forgets the address first; the next successful
	poll re-pins the new key via `accept-new`. Best-effort: no entry to remove (the
	common case) exits 0; a missing known_hosts file or absent `ssh-keygen` is
	swallowed, since this is a convenience de-pin, not a security boundary."""
	if not KNOWN_HOSTS_PATH.exists():
		return
	# ssh-keygen stores v6 literals bracketed and non-22 ports as [host]:port; for
	# the default-port case the bare literal is the key. Strip our own brackets so
	# the form matches what accept-new wrote.
	target = host[1:-1] if host.startswith("[") and host.endswith("]") else host
	try:
		subprocess.run(
			["ssh-keygen", "-R", target, "-f", str(KNOWN_HOSTS_PATH)],
			capture_output=True,
			timeout=15,
			check=False,
		)
	except (FileNotFoundError, subprocess.SubprocessError):
		pass
