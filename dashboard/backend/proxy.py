#!/usr/bin/env python3
"""Atlas host dashboard — the dev SSH proxy.

A local stand-in for the shipped backend that lets you point the dashboard at
REAL hosts **without installing anything on them**. It serves the same
`/api/state` (and the dev `/api/state/sources` switcher) the Vite mock serves,
but each response is live: for the requested host it pushes `server.py` over
`ssh` in `--collect` mode, reads the JSON the host prints on stdout, and returns
it. Nothing lands on the host — the collector runs from a pipe (`python3 -`) and
exits.

    python3 backend/proxy.py f2-aditya-blr3 f1-aditya-blr3
    # -> http://localhost:8080 ; open the SPA, header switches between hosts

Then run the frontend against it instead of the mock:

    VITE_API_BASE=http://localhost:8080 npm run dev
    # or just open http://localhost:8080 if you've `npm run build`-ed into dist/

Host arguments are whatever your system `ssh` understands — an alias from
`~/.ssh/config`, `user@host`, or a bare hostname. The proxy shells out to `ssh`,
so it inherits your keys, config, and known_hosts; it has NO Atlas/Frappe
dependency and needs no bench.

Design mirrors the backend it fronts: stdlib only, read-only, best-effort. An
unreachable host yields a 502 with the ssh error, never a crash; a host that
answers but is missing tooling simply returns emptier sections.

Metrics: a `--collect` run is a single snapshot, so the host emits RAW counters
(`metrics.raw`) rather than rates. The proxy is the long-lived process, so it
holds a per-host history ring and derives the rate series (cpu / net / disk)
across its own polls — exactly what the in-process HTTP server does, moved to
the process that actually persists between reads.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

BIND = os.environ.get("ATLAS_PROXY_BIND", "127.0.0.1")
PORT = int(os.environ.get("ATLAS_PROXY_PORT", "8080"))
# Seconds a host's collected state is reused before re-SSHing (spec/24 § not
# realtime: 15-60s is fine). Rapid page refreshes then share one round trip.
CACHE_SECONDS = float(os.environ.get("ATLAS_PROXY_CACHE", "10"))
SSH_TIMEOUT = int(os.environ.get("ATLAS_PROXY_SSH_TIMEOUT", "25"))
# Optional explicit ssh identity + known_hosts, so the proxy can reach Atlas
# hosts as root@<ip> with the Atlas key (not just ~/.ssh/config aliases). When
# unset, ssh uses your default identity/config. `setup.sh` wires these from the
# bench so `python3 backend/proxy.py root@<ip>` authenticates the same way Atlas
# Tasks do.
SSH_IDENTITY = os.environ.get("ATLAS_PROXY_SSH_KEY", "")
SSH_KNOWN_HOSTS = os.environ.get("ATLAS_PROXY_KNOWN_HOSTS", "")

SERVER_PY = Path(__file__).with_name("server.py")

# The metric series the proxy derives, matching server.py's contract.
_METRIC_UNITS = {
	"cpu_util_pct": "%",
	"mem_used_mib": "MiB",
	"disk_io_iops": "iops",
	"net_rx_mbps": "Mb/s",
	"net_tx_mbps": "Mb/s",
	"pool_used_pct": "%",
}
_METRIC_WINDOW = 48


class Host:
	"""One target host: its ssh destination + the state it accumulates. Holds the
	previous raw counters (to diff into rates) and the per-series history ring, so
	the metric window grows across polls just like the HTTP server's in-process
	ring — the proxy is simply where that state lives for the SSH transport."""

	def __init__(self, dest: str, id: str | None = None, label: str | None = None):
		self.dest = dest
		self.id = id or _slug(dest)
		self.label = label or dest
		self._cached: dict | None = None
		self._cached_at = 0.0
		self._prev_raw: dict | None = None
		self._history: dict = {key: [] for key in _METRIC_UNITS}

	def state(self) -> dict:
		"""Return this host's state, collecting freshly over SSH if the cache is
		cold. Raises CollectError if ssh/collection fails."""
		now = time.monotonic()
		if self._cached is not None and (now - self._cached_at) < CACHE_SECONDS:
			return self._cached
		state = _collect(self.dest)
		state["metrics"] = self._fold_metrics(state.get("metrics", {}))
		self._cached = state
		self._cached_at = now
		return state

	def _fold_metrics(self, metrics: dict) -> dict:
		"""Turn the host's raw counter snapshot into the accumulating series the
		charts render. Gauges travel resolved; rate series are diffed against the
		previous poll. The first poll only seeds counters (rates need two)."""
		raw = metrics.get("raw") or {}
		sample = {
			"mem_used_mib": raw.get("mem_used_mib"),
			"pool_used_pct": raw.get("pool_used_pct"),
			"cpu_util_pct": None,
			"net_rx_mbps": None,
			"net_tx_mbps": None,
			"disk_io_iops": None,
		}
		prev = self._prev_raw
		if prev is not None:
			dt = (raw.get("mono") or 0) - (prev.get("mono") or 0)
			if dt > 0:
				cpu, pcpu = raw.get("cpu"), prev.get("cpu")
				if cpu and pcpu and (cpu[1] - pcpu[1]) > 0:
					sample["cpu_util_pct"] = _clamp(100.0 * (cpu[0] - pcpu[0]) / (cpu[1] - pcpu[1]))
				net, pnet = raw.get("net"), prev.get("net")
				if net and pnet:
					sample["net_rx_mbps"] = max(0.0, (net[0] - pnet[0]) * 8 / 1e6 / dt)
					sample["net_tx_mbps"] = max(0.0, (net[1] - pnet[1]) * 8 / 1e6 / dt)
				dsk, pdsk = raw.get("disk_ios"), prev.get("disk_ios")
				if dsk is not None and pdsk is not None:
					sample["disk_io_iops"] = max(0.0, (dsk - pdsk) / dt)
		self._prev_raw = raw

		for key, value in sample.items():
			if value is None:
				continue
			ring = self._history[key]
			ring.append(round(value, 2))
			del ring[:-_METRIC_WINDOW]

		return {
			"collected_at": metrics.get("collected_at") or _now_iso(),
			"series": {
				key: {"unit": _METRIC_UNITS[key], "points": list(points)}
				for key, points in self._history.items()
				if points
			},
		}


class CollectError(Exception):
	pass


def _collect(dest: str) -> dict:
	"""Push server.py to `dest` over ssh in --collect mode, parse its stdout JSON.

	`ssh dest python3 - --collect < server.py` runs the collector from a pipe on
	the host's own python3; nothing is written to disk there. BatchMode keeps ssh
	from blocking on a password prompt in this non-interactive path."""
	script = SERVER_PY.read_bytes()
	argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
	if SSH_IDENTITY:
		# IdentitiesOnly so ssh uses ONLY this key (not every agent identity),
		# matching how Atlas Tasks authenticate to the host as root.
		argv += ["-o", f"IdentityFile={SSH_IDENTITY}", "-o", "IdentitiesOnly=yes"]
	if SSH_KNOWN_HOSTS:
		argv += ["-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=accept-new"]
	argv += [dest, "python3 - --collect"]
	try:
		proc = subprocess.run(
			argv,
			input=script,
			capture_output=True,
			timeout=SSH_TIMEOUT,
			check=False,
		)
	except subprocess.TimeoutExpired as exc:
		raise CollectError(f"ssh to {dest} timed out after {SSH_TIMEOUT}s") from exc
	except OSError as exc:
		raise CollectError(f"could not launch ssh: {exc}") from exc

	if proc.returncode != 0:
		err = proc.stderr.decode("utf-8", "replace").strip() or f"ssh exited {proc.returncode}"
		raise CollectError(err)
	try:
		return json.loads(proc.stdout)
	except ValueError as exc:
		tail = proc.stdout.decode("utf-8", "replace")[-300:]
		raise CollectError(f"host did not return JSON: {exc}: …{tail!r}") from exc


class Handler(BaseHTTPRequestHandler):
	hosts: ClassVar[dict] = {}
	order: ClassVar[list] = []

	def do_GET(self):
		path = urlparse(self.path).path
		if path == "/api/state/sources":
			return self._json(
				200, [{"id": h.id, "label": h.label} for h in (self.hosts[i] for i in self.order)]
			)
		if path == "/api/state":
			return self._serve_state()
		return self._json(404, {"error": f"no route {path}"})

	def _serve_state(self):
		params = parse_qs(urlparse(self.path).query)
		src = (params.get("src") or [None])[0]
		host = self.hosts.get(src) if src else (self.hosts[self.order[0]] if self.order else None)
		if host is None:
			return self._json(404, {"error": f"unknown host {src!r}"})
		try:
			state = host.state()
		except CollectError as exc:
			return self._json(502, {"error": str(exc), "host": host.dest})
		self._json(200, state)

	def _json(self, code: int, body) -> None:
		payload = json.dumps(body).encode()
		self.send_response(code)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.send_header("Cache-Control", "no-store")
		# The frontend dev server is a different origin; let it read us.
		self.send_header("Access-Control-Allow-Origin", "*")
		self.end_headers()
		self.wfile.write(payload)

	def log_message(self, fmt, *args):
		sys.stderr.write("  " + (fmt % args) + "\n")


def _slug(dest: str) -> str:
	keep = [c if (c.isalnum() or c in "-_.") else "-" for c in dest]
	return "".join(keep).strip("-") or "host"


def _clamp(value: float) -> float:
	return max(0.0, min(100.0, value))


def _now_iso() -> str:
	return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hosts_from_args(argv: list) -> list:
	"""Build the host list from the CLI. Two forms, combinable:

	  --hosts-json <file>   a manifest of {id,label,dest} (setup.sh writes it from
	                        the bench, so labels carry the Server title + status)
	  <ssh-host> ...        bare ssh aliases / user@host, labelled by the dest

	Order is manifest-first, then bare args, de-duplicated by id."""
	hosts, seen = [], set()

	def add(host):
		if host.id in seen:
			return
		seen.add(host.id)
		hosts.append(host)

	i = 0
	while i < len(argv):
		arg = argv[i]
		if arg == "--hosts-json":
			i += 1
			manifest = json.loads(Path(argv[i]).read_text())
			for h in manifest.get("hosts", []):
				add(Host(h["dest"], id=h.get("id"), label=h.get("label")))
		elif not arg.startswith("-"):
			add(Host(arg))
		i += 1
	return hosts


def main():
	if not SERVER_PY.is_file():
		sys.stderr.write(f"error: {SERVER_PY} not found (proxy pushes it over ssh)\n")
		raise SystemExit(2)
	try:
		hosts = _hosts_from_args(sys.argv[1:])
	except (OSError, ValueError, KeyError) as exc:
		sys.stderr.write(f"error reading hosts: {exc}\n")
		raise SystemExit(2)
	if not hosts:
		sys.stderr.write(
			"usage: python3 backend/proxy.py <ssh-host> [<ssh-host> ...]\n"
			"   or: python3 backend/proxy.py --hosts-json hosts.json\n"
			"  each host is an ssh alias / user@host the local `ssh` can reach.\n"
		)
		raise SystemExit(2)

	Handler.hosts = {h.id: h for h in hosts}
	Handler.order = [h.id for h in hosts]

	server = ThreadingHTTPServer((BIND, PORT), Handler)
	print(f"atlas dashboard proxy on http://{BIND}:{PORT}")
	if SSH_IDENTITY:
		print(f"  ssh key: {SSH_IDENTITY}")
	for h in hosts:
		print(f"  · {h.id:24} → ssh {h.dest}")
	print(f"  point the app at it:  VITE_API_BASE=http://{BIND}:{PORT} npm run dev")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		server.shutdown()


if __name__ == "__main__":
	main()
