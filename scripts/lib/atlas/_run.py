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
import re
import shlex
import subprocess
import sys
import tempfile
import time

# The literal two-char placeholder `{}` — and ONLY that. We deliberately do NOT use
# str.format(): this codebase is brace-heavy (nft chain clauses `{ type filter … }`
# appear verbatim in command strings, many already inside f-strings), and format()
# would force every one of those braces to be doubled. This token matches `{}` and
# nothing else, so every other brace is left byte-for-byte untouched.
_HOLE = re.compile(r"\{\}")


def _substitute(template: str, params: tuple) -> str:
	"""Replace each literal `{}` in `template` with `shlex.quote(str(param))`, in
	order, leaving every other character (notably nft's `{ … }` clauses) untouched.

	Quoting makes each param exactly ONE shell token: a value with an internal space,
	a `;`, a `|`, a `$(…)`, a quote — none can break out of its slot. This is the
	parameterized-SQL trust model (`execute("… WHERE id = ?", id)`): literal template,
	quoted holes, and forgetting to quote is not expressible.

	Raises TypeError when the number of `{}` placeholders doesn't match the number of
	params (a programming bug — caught loud, not silently mis-rendered). The arity is
	checked by counting up front rather than by exhausting an iterator: on CPython
	3.14 a StopIteration raised inside an re.sub replacement propagates raw (it is NOT
	wrapped in RuntimeError), so a count check is the version-independent contract."""
	holes = _HOLE.findall(template)
	if len(holes) != len(params):
		raise TypeError(f"{template!r}: {len(holes)} {{}} placeholder(s) but {len(params)} param(s)")
	it = iter(params)
	return _HOLE.sub(lambda _m: shlex.quote(str(next(it))), template)


def _render(template: str, params: tuple) -> list[str]:
	"""Quoted-substitute, then `shlex.split` into a real argv for `shell=False`. The
	quoting in `_substitute` guarantees each param survives the split as exactly one
	argv token; literals in the template split on whitespace as written. There is no
	shell anywhere in this path."""
	return shlex.split(_substitute(template, params))


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


def run(command: str, *params: object, check: bool = True, quiet: bool = False, trace: bool = True) -> str:
	"""Run one command, echo it (the `set -x` trace), return its stdout.

	- `command` reads like a shell line; interpolated values go through `{}`
	  placeholders that are auto-quoted (`_render`), so a value with an internal
	  space — or a `;`, `|`, `$(…)`, quote — stays exactly one argv token and cannot
	  break out (the bug that forced the `mapfile` dance for cpu.max disappears, and
	  there is no injection surface to forget about). There is **no shell**: the
	  rendered argv is run with `shell=False`. For a genuine pipeline use `shell()`.

	      run("sudo systemctl stop nginx")            # literals
	      run("sudo ip link set {} up", tap_device)   # one var, auto-quoted

	- On non-zero exit raises CommandError unless `check=False` (the Python form
	  of a guarded `|| true`), in which case the exit code is discarded and
	  stdout returned.
	- The `+ <command>` line goes to stderr so it interleaves with `bash -x`
	  tracing already in the Task log and never pollutes stdout that a caller
	  parses (e.g. blockdev --getsize64).
	- `trace=False` suppresses that `+ <command>` line — for an always-on daemon
	  that polls the same read-only command every second (atlas-wake-trap.py), where
	  a per-second trace would flood the journal for no diagnostic value.
	"""
	argv = _render(command, params)
	start = _trace(argv) if trace else 0.0
	result = subprocess.run(argv, capture_output=True, text=True, check=False)
	if trace:
		_traced(argv, start)
	if result.stderr and not quiet:
		sys.stderr.write(result.stderr)
		sys.stderr.flush()
	if check and result.returncode != 0:
		raise CommandError(argv, result.returncode, result.stdout + result.stderr)
	return result.stdout


def run_ok(command: str, *params: object) -> bool:
	"""Run a command purely as a boolean gate — the Python form of
	`cmd >/dev/null 2>&1` used in an `if`. Never raises, never prints output;
	True iff exit 0. Same string + `{}` auto-quoting front-end as `run()`. This is
	how atlas_lv_exists's `>/dev/null 2>&1` gate ports."""
	result = subprocess.run(_render(command, params), capture_output=True, text=True, check=False)
	return result.returncode == 0


def run_input(command: str, *params: object, stdin: str) -> str:
	"""Run a command feeding `stdin` to its standard input — the Python form of
	`printf ... | sudo cmd`. Same string + `{}` auto-quoting front-end as `run()`.
	Echoes the command (the set -x trace), raises CommandError on non-zero, returns
	stdout."""
	argv = _render(command, params)
	start = _trace(argv)
	result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
	_traced(argv, start)
	if result.stderr:
		sys.stderr.write(result.stderr)
		sys.stderr.flush()
	if result.returncode != 0:
		raise CommandError(argv, result.returncode, result.stdout + result.stderr)
	return result.stdout


def shell(
	command: str, *params: object, check: bool = True, quiet: bool = False, stdin: str | None = None
) -> str:
	"""Run a command **through `sh -c`** so shell metacharacters in the *template*
	(`|`, `>`, `*`, `&&`) are honored — the one thing `run()` deliberately won't do.

	Params still go through the SAME `{}` auto-quoting, so an interpolated value can
	never inject into the pipeline; only the literal template you write is shell.
	Use sparingly — `run()` is the default and never silently invokes a shell.

	    shell("tail -c +{} {} | zstd -dc -f > {}", n, packed_path, kernel_part)

	`stdin`, when given, is piped to the shell's stdin (the heredoc/`tee -a` form)."""
	rendered = _substitute(command, params)
	if stdin is not None:
		return run_input("sh -c {}", rendered, stdin=stdin)
	return run("sh -c {}", rendered, check=check, quiet=quiet)


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
		prefix = "sudo " if sudo else ""
		run(prefix + "install -m {} {} {}", mode, src, dest)
	finally:
		os.unlink(src)


def install_directory(dest: str, *, mode: str = "0700", sudo: bool = True) -> None:
	"""`install -d -m <mode> <dest>` — create a directory with an explicit mode."""
	prefix = "sudo " if sudo else ""
	run(prefix + "install -d -m {} {}", mode, dest)


def firecracker_api(socket_directory: str, socket_name: str, method: str, api_path: str, body: str) -> None:
	"""Call the Firecracker API over its jailed unix socket.

	The absolute socket path exceeds AF_UNIX's 108-byte sun_path limit, so we
	`cd` into the socket directory (as root via `sudo sh -c` — the dir is
	0700-owned by the per-VM uid) and address the socket by its short relative
	name. --fail makes a 4xx/5xx exit non-zero so a refused state change surfaces
	as a failed Task, not a silent success.

	`method`/`api_path` are caller-fixed literals (PATCH/PUT + `/vm` etc.), so they
	stay inline in the template; `socket_directory`, `socket_name`, and `body` are
	the data and go through `{}` auto-quoting. `sudo sh -c {…}` renders to one argv
	(`['sudo','sh','-c', <the whole quoted shell line>]`) — the shell line itself is
	a single quoted token, so the cd/&&/curl pipeline runs under that one `sh`."""
	command = (
		"cd {} && "
		"curl --fail --silent --show-error "
		"--unix-socket {} "
		f"-X {method} 'http://localhost{api_path}' "
		"-H 'Content-Type: application/json' "
		"-d {}"
	)
	rendered = _substitute(command, (socket_directory, socket_name, body))
	run("sudo sh -c {}", rendered)


def firecracker_api_patch(socket_directory: str, socket_name: str, body: str) -> None:
	"""PATCH the Firecracker /vm state (Paused/Resumed) — see firecracker_api."""
	firecracker_api(socket_directory, socket_name, "PATCH", "/vm", body)
