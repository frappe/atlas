"""The one place that touches the host — the zx slice the spec promised.

`bash -x; set -euo pipefail` gave us, for free: echo-every-command tracing into
the Task log, and abort-on-first-failure. Python gives us neither by default, so
we reimplement that small slice here (spec principle 6: don't import — copy).
This is ~one screen of code and it is the *only* module in the package that runs
a subprocess; everything else is pure functions over strings, so everything else
is unit-testable without a host.

Patterned on references/agent/agent/base.py::execute — a subprocess wrapper that
streams output and raises on non-zero — reduced to the slice Atlas needs.
"""

import os
import shlex
import subprocess
import sys
import tempfile
import time


def _trace(argv: list[str]) -> float:
	"""Echo the `set -x` trace line for `argv` to stderr and return a monotonic
	start time. Pair with `_traced` to print the command's wall-clock duration on
	the same trace stream, so the Task log shows which commands are slow."""
	print("+ " + shlex.join(argv), file=sys.stderr, flush=True)
	return time.monotonic()


def _traced(argv: list[str], start: float) -> None:
	"""Close the trace opened by `_trace`: print `+ (<elapsed>) <command>` so each
	command's duration sits next to its invocation in the Task log (stderr)."""
	elapsed = time.monotonic() - start
	print(f"+ ({elapsed:.3f}s) {shlex.join(argv)}", file=sys.stderr, flush=True)


class CommandError(RuntimeError):
	"""A command exited non-zero. Carries the argv, code, and captured output so
	the Task log (stderr) shows exactly what failed — the Python equivalent of
	`bash -x` stopping at the failing line."""

	def __init__(self, argv: list[str], returncode: int, output: str):
		self.argv = argv
		self.returncode = returncode
		self.output = output
		super().__init__(f"command failed (exit {returncode}): {shlex.join(argv)}\n{output}")


def run(*argv: str, check: bool = True, quiet: bool = False) -> str:
	"""Run one command, echo it (the `set -x` trace), return its stdout.

	- `argv` is a real argument vector — no shell, so no quoting hazards, no
	  word-splitting of values with internal spaces (the bug that forced the
	  `mapfile` dance for cpu.max in provision-vm.sh disappears entirely).
	- On non-zero exit raises CommandError unless `check=False` (the Python form
	  of a guarded `|| true`), in which case the exit code is discarded and
	  stdout returned.
	- The `+ <command>` line goes to stderr so it interleaves with `bash -x`
	  tracing already in the Task log and never pollutes stdout that a caller
	  parses (e.g. blockdev --getsize64).
	"""
	start = _trace(list(argv))
	result = subprocess.run(argv, capture_output=True, text=True, check=False)
	_traced(list(argv), start)
	if result.stderr and not quiet:
		sys.stderr.write(result.stderr)
		sys.stderr.flush()
	if check and result.returncode != 0:
		raise CommandError(list(argv), result.returncode, result.stdout + result.stderr)
	return result.stdout


def run_ok(*argv: str) -> bool:
	"""Run a command purely as a boolean gate — the Python form of
	`cmd >/dev/null 2>&1` used in an `if`. Never raises, never prints output;
	True iff exit 0. This is how atlas_lv_exists's `>/dev/null 2>&1` gate ports."""
	result = subprocess.run(argv, capture_output=True, text=True, check=False)
	return result.returncode == 0


def run_input(*argv: str, stdin: str) -> str:
	"""Run a command feeding `stdin` to its standard input — the Python form of
	`printf ... | sudo cmd` or a heredoc piped into `install /dev/stdin`. Echoes
	the command (the set -x trace), raises CommandError on non-zero, returns
	stdout."""
	start = _trace(list(argv))
	result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
	_traced(list(argv), start)
	if result.stderr:
		sys.stderr.write(result.stderr)
		sys.stderr.flush()
	if result.returncode != 0:
		raise CommandError(list(argv), result.returncode, result.stdout + result.stderr)
	return result.stdout


def install_file(content: str, dest: str, *, mode: str = "0644", sudo: bool = True) -> None:
	"""Write `content` to `dest` with `mode`, atomically, via `install -m <mode>
	<src> <dest>` — preserves the install(1) semantics the heredocs relied on
	(create-or-replace with the mode set in one shot).

	`src` is a real temp file, NOT `/dev/stdin`. uutils (rust-coreutils) `install`
	— the default on Ubuntu 26.04 — cannot reliably copy from a non-seekable pipe
	source: feeding content as the child's stdin and passing `/dev/stdin` fails
	~90% of the time (proven 29/30 on a Self-Managed host) with `install: No such
	file or directory`, while the SAME pipe reads fine via cat. GNU install tolerates
	it; uutils does not. The flakiness silently broke bootstrap and sync-image. A
	spooled regular file is seekable, so install copies it 30/30."""
	with tempfile.NamedTemporaryFile("w", prefix="atlas-install-", delete=False) as spool:
		spool.write(content)
		# nosemgrep: tempfile-without-flush -- false positive: the file is closed (and flushed) by the with-block exit before install reads src below
		src = spool.name
	try:
		argv = (["sudo"] if sudo else []) + ["install", "-m", mode, src, dest]
		run(*argv)
	finally:
		os.unlink(src)


def install_directory(dest: str, *, mode: str = "0700", sudo: bool = True) -> None:
	"""`install -d -m <mode> <dest>` — create a directory with an explicit mode."""
	argv = (["sudo"] if sudo else []) + ["install", "-d", "-m", mode, dest]
	run(*argv)


def firecracker_api(socket_directory: str, socket_name: str, method: str, api_path: str, body: str) -> None:
	"""Call the Firecracker API over its jailed unix socket.

	The absolute socket path exceeds AF_UNIX's 108-byte sun_path limit, so we
	`cd` into the socket directory (as root via `sudo sh -c` — the dir is
	0700-owned by the per-VM uid) and address the socket by its short relative
	name. --fail makes a 4xx/5xx exit non-zero so a refused state change surfaces
	as a failed Task, not a silent success."""
	command = (
		f"cd {shlex.quote(socket_directory)} && "
		f"curl --fail --silent --show-error "
		f"--unix-socket {shlex.quote(socket_name)} "
		f"-X {method} 'http://localhost{api_path}' "
		f"-H 'Content-Type: application/json' "
		f"-d {shlex.quote(body)}"
	)
	run("sudo", "sh", "-c", command)


def firecracker_api_patch(socket_directory: str, socket_name: str, body: str) -> None:
	"""PATCH the Firecracker /vm state (Paused/Resumed) — see firecracker_api."""
	firecracker_api(socket_directory, socket_name, "PATCH", "/vm", body)
