"""Read + atomically update the local-ownership cache (spec §11.3).

`atlas-networkd` never polls Frappe and never listens for VM lifecycle events.
Instead the VM-lifecycle scripts (`vm-network-up.py` / `vm-network-down.py`)
atomically update `/etc/atlas-networkd/local-ownership.json` — the same hook
point where they install the per-VM veth nft rules — and the daemon reads it
during its periodic scan (the loop calls `read_local_ownership` every
`ownership_scan_interval`). This is the seam that keeps the §4 decoupling
("networking should not understand virtualization") intact: the daemon reads
an address list, nothing more.

File shape (atomic tempfile + os.replace from the writers):

    { "owned": ["fdaa:1a2b:3c4d:0:9f3e:1100:abcd:42", ...] }

The reader is pure; the writers (`add_local_owned` / `remove_local_owned`) do a
read-modify-write under a POSIX `flock` exclusive lock (`/etc/atlas-networkd/
local-ownership.lock`). The lock is needed because two concurrent
`vm-network-up.py` processes (triggered by overlapping VM boots) would both
read the same baseline set, then each overwrite with only its own addition —
the second `os.replace` silently clobbers the first VM's /128. The daemon
scan never writes (only reads), so the lock serializes only the VM-lifecycle
writers; the daemon reads the final file lock-free after the writer releases.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

_DEFAULT_CACHE_PATH = "/etc/atlas-networkd/local-ownership.json"
_LOCK_PATH = "/etc/atlas-networkd/local-ownership.lock"


def read_local_ownership(path: str = _DEFAULT_CACHE_PATH) -> frozenset[str]:
	"""Return the set of /128s the host locally owns, per the cache at `path`.

	A missing file → empty set (the daemon is fresh, no VM yet). A present but
	malformed file raises — fail loud at the boundary (Taste.md); a silent
	empty-set on a corrupt cache would re-advertise "I own nothing" at gen
	`persisted+1` and could withdraw routes the host should still be carrying.
	An empty `{}` or `{"owned": []}` is legitimate (the host owns nothing this
	scan) and returns an empty set.
	"""
	p = Path(path)
	if not p.exists():
		return frozenset()
	with p.open("r", encoding="utf-8") as fh:
		data = json.load(fh)
	if not isinstance(data, dict) or "owned" not in data:
		raise ValueError(f"local-ownership cache at {path} is not a dict with 'owned'")
	owned = data["owned"]
	if not isinstance(owned, list):
		raise ValueError(f"local-ownership cache at {path} 'owned' is not a list")
	return frozenset(str(ip) for ip in owned)


def add_local_owned(ip: str, path: str = _DEFAULT_CACHE_PATH) -> None:
	"""Atomically add `ip` to the local-ownership cache (spec §11.3). Used by
	`vm-network-up.py` once the per-VM veth + routes are installed: the address
	now belongs to this host and the daemon's scan will pick it up on the next
	tick. Read-modify-write under a POSIX ``flock`` (exclusive) so concurrent
	vm-network-up.py invocations don't clobber each other. No-op if `ip` is
	already in the cache (idempotent)."""
	fd = _lock()
	try:
		current = read_local_ownership(path)
		if ip in current:
			return
		_atomic_write(path, {**_read_dict(path), "owned": sorted(current | {ip})})
	finally:
		_unlock(fd)


def remove_local_owned(ip: str, path: str = _DEFAULT_CACHE_PATH) -> None:
	"""Atomically remove `ip` from the cache. Used by `vm-network-down.py` once
	the per-VM teardown completes: the /128 no longer belongs to this host. No-op
	if the cache is missing or `ip` isn't in it. The daemon's next scan will see
	the smaller set and advertise the withdrawal at a fresh Generation (§12.1)."""
	fd = _lock()
	try:
		current = read_local_ownership(path)
		if ip not in current:
			return
		_atomic_write(path, {**_read_dict(path), "owned": sorted(current - {ip})})
	finally:
		_unlock(fd)


def same_set(a: frozenset[str], b: frozenset[str]) -> bool:
	"""Order-insensitive equality of two /128 sets — the §11.2 gate that decides
	whether a fresh scan warrants a new advertisement (Generation bump) or not.
	Exposed for the daemon loop; pure so the trigger is unit-testable."""
	return a == b


# --- writers (flock-serialized) --------------------------------------------

# Lock file shared across all VM-lifecycle scripts on the host. Writers acquire
# an exclusive flock before the read-modify-write so two concurrent
# vm-network-up.py invocations don't clobber each other's additions.


def _lock() -> int:
	"""Acquire an exclusive flock on ``_LOCK_PATH``. Blocks until held.
	Returns the fd so the caller can ``_unlock(fd)``."""
	p = Path(_LOCK_PATH)
	p.parent.mkdir(parents=True, exist_ok=True)
	fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
	fcntl.flock(fd, fcntl.LOCK_EX)
	return fd


def _unlock(fd: int) -> None:
	"""Release the exclusive lock and close the fd."""
	fcntl.flock(fd, fcntl.LOCK_UN)
	os.close(fd)


# --- helpers (atomic write + defensive read) -------------------------------


def _read_dict(path: str) -> dict:
	"""Return the cache's dict (top-level shape) so writers can preserve any
	extra fields the daemon may later add (e.g. a schema-version stamp). Falls
	back to `{}` on a missing file so the first add creates the cache."""
	p = Path(path)
	if not p.exists():
		return {}
	try:
		with p.open("r", encoding="utf-8") as fh:
			data = json.load(fh)
		return data if isinstance(data, dict) else {}
	except (json.JSONDecodeError, ValueError):
		# A corrupt cache: drop it (the writers replace it with a clean one).
		# The reader raises loudly on a malformed file; the writers recover by
		# writing the new state from scratch — semantics: "the cache IS what
		# the current scan computed", not "preserve the corrupted tail".
		return {}


def _fsync_dir(path: Path) -> None:
	"""fsync the directory `path` so a preceding `os.replace` into it is durable
	across a crash/power-cut. Without this the rename can be lost even though
	`os.replace` returned — a just-added VM's /128 could vanish after a power
	cut. Linux is the target (`O_DIRECTORY`); degrade to a no-op elsewhere."""
	dir_flag = getattr(os, "O_DIRECTORY", 0)
	if not dir_flag:
		return
	fd = os.open(str(path), dir_flag)
	try:
		os.fsync(fd)
	finally:
		os.close(fd)


def _atomic_write(path: str, body: dict) -> None:
	"""Write `body` to `path` atomically: tempfile + os.replace, creating the
	parent dir (0755) on the way. Atomic at the rename level so a daemon mid-
	scan sees either the OLD or the NEW file, never an empty/truncated one.
	A missing parent dir is a fresh install; we create it once. The parent dir
	is fsync'd after the rename so the replace survives a power-cut."""
	p = Path(path)
	p.parent.mkdir(parents=True, exist_ok=True)
	# write_text + flush + os.replace — inside `try` to clean up on failure.
	tmp_name: str | None = None
	try:
		fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
		with os.fdopen(fd, "w", encoding="utf-8") as fh:
			json.dump(body, fh, sort_keys=True)
			fh.write("\n")
			fh.flush()
			os.fsync(fh.fileno())
		os.chmod(tmp_name, 0o644)
		os.replace(tmp_name, str(p))
		_fsync_dir(p.parent)
	except Exception:
		if tmp_name is not None:
			try:
				os.unlink(tmp_name)
			except FileNotFoundError:
				pass
		raise


__all__ = [
	"_DEFAULT_CACHE_PATH",
	"add_local_owned",
	"read_local_ownership",
	"remove_local_owned",
	"same_set",
]
