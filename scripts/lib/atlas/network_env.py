"""Typed reader for the per-VM network.env sidecar.

provision writes /var/lib/atlas/virtual-machines/<uuid>/network.env (a shell
KEY=value file) carrying the tap, addresses, netns, veth names, and per-VM uid.
The systemd hooks (vm-disk-up, vm-network-up/down) read it back instead of
consulting the Frappe DB — the host state is reconstructible from disk after a
reboot. The shell sourced it with `.` and guarded each var with `${VAR:?...}`.

Here that parse-and-guard is one typed object: read_network_env() parses the
file into a NetworkEnv, and .require()/.require_int() reproduce the shell's
fail-loud-on-missing semantics, naming the variable. Pure except for the file
read, so the parsing is unit-testable from a string with no host.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from atlas._run import run


@dataclass(frozen=True)
class NetworkEnv:
	"""The KEY=value pairs from a network.env, with fail-loud typed accessors."""

	values: dict[str, str]

	@classmethod
	def parse(cls, text: str) -> "NetworkEnv":
		"""Parse shell KEY=value lines. Blank lines and comments are skipped;
		surrounding quotes on a value are stripped (provision writes bare values,
		but be liberal). Mirrors what `.` sourcing would expose as variables."""
		values: dict[str, str] = {}
		for line in text.splitlines():
			line = line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, _, value = line.partition("=")
			value = value.strip()
			if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
				value = value[1:-1]
			values[key.strip()] = value
		return cls(values)

	def require(self, name: str) -> str:
		"""Return the value, or exit non-zero naming the missing var — the form
		of the shell's `: "${VAR:?missing in network.env}"`."""
		value = self.values.get(name)
		if not value:
			raise SystemExit(f"{name}: missing in network.env")
		return value

	def require_int(self, name: str) -> int:
		raw = self.require(name)
		try:
			return int(raw)
		except ValueError:
			raise SystemExit(f"{name}: expected an integer in network.env, got {raw!r}")

	def get(self, name: str, default: str = "") -> str:
		"""Optional read — the form of `${VAR:-}` (used by vm-network-down, which
		tolerates a partially-written or absent env)."""
		return self.values.get(name) or default


def read_network_env(path: str) -> NetworkEnv:
	"""Read and parse a network.env file. Raises if the file is unreadable —
	a missing env at disk-up/network-up time is a real failure (the VM was never
	provisioned), so unlike the down path we do not tolerate absence here."""
	# nosemgrep: frappe-security-file-traversal -- host/guest script; reads a per-VM network.env path derived from the VM name, not untrusted web input
	with open(path) as handle:
		return NetworkEnv.parse(handle.read())


def upsert_network_env(text: str, key: str, value: str) -> str:
	"""Return `text` with `key=value` set — replacing an existing line in place
	(preserving order) or appending it. Used to add `RESERVED_IPV4` to a running
	VM's network.env at attach time, so a later reboot re-creates the 1:1-NAT from
	disk exactly as provision would. Pure string transform: the caller does the
	atomic write (install_file). Trailing-newline preserving."""
	lines = text.splitlines()
	rendered = f"{key}={value}"
	for index, line in enumerate(lines):
		stripped = line.strip()
		if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
			lines[index] = rendered
			break
	else:
		lines.append(rendered)
	return "\n".join(lines) + "\n"


def remove_network_env(text: str, key: str) -> str:
	"""Return `text` with any `key=…` line removed. The detach twin of
	`upsert_network_env`: a detached VM's env no longer carries `RESERVED_IPV4`,
	so a reboot brings it up with no inbound NAT. Pure string transform."""
	kept = [
		line
		for line in text.splitlines()
		if not (
			line.strip() and not line.strip().startswith("#") and line.strip().split("=", 1)[0].strip() == key
		)
	]
	return "\n".join(kept) + "\n" if kept else ""


def read_network_env_optional(path: str) -> NetworkEnv:
	"""The down-path twin of read_network_env: return an empty NetworkEnv when
	the file is absent (terminate-vm may have removed it before the unit's
	ExecStopPost runs). Each value is then read with .get() and guarded by
	`if value:` — the shell's `[ -n "${VAR:-}" ]` tolerance."""
	if not os.path.isfile(path):
		return NetworkEnv({})
	return read_network_env(path)


def default_route_device(family: str = "", *, tolerate_missing: bool = False) -> str:
	"""The interface carrying the default route — the host uplink. Ports the
	shell's `ip -j [-6] route show default | jq -r '.[0].dev'` (no sudo: a
	read-only query). `family` is "-6" for the IPv6 uplink, "" for IPv4 (which
	may differ on a multi-homed host). Used by bootstrap (masquerade rule) and
	the network hooks (proxy-NDP, NAT).

	`tolerate_missing=True` is the down-path form (the shell's trailing
	`2>/dev/null || true`): on any failure or no default route, return "" instead
	of raising, so teardown proceeds even when the route is already gone."""
	argv = ["ip", "-j"]
	if family:
		argv.append(family)
	argv += ["route", "show", "default"]
	output = run(*argv, check=not tolerate_missing, quiet=tolerate_missing)
	if not output.strip():
		return ""
	routes = json.loads(output)
	return routes[0]["dev"] if routes else ""
