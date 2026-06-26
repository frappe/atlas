"""TCP proxy control plane — Atlas reconciles each proxy guest's live TCP port map
over SSH-to-the-guest (spec/17-tcp-proxy.md).

The exact mirror of `atlas/atlas/proxy.py`, for the L4 forwarder instead of the
HTTP reverse proxy. Atlas is the source of truth; each proxy VM's stream{}
`lua_shared_dict ports` is a cache (spec principle #2). This module builds the
desired regional port map from the `Port Mapping` rows, serializes it the SAME
canonical way the guest's stream_persist.lua does (so "in sync?" is a byte
compare — `canonical_json` is reused verbatim from `proxy.py`), SSHes into each
proxy guest, reads its live map off the stream-admin socket, and bulk-`SYNC`s the
full map on drift.

The differences from `proxy.py` are exactly two, both forced by the http/stream
lua_shared_dict isolation (a dict in http{} is invisible to stream{} Lua and vice
versa, so the TCP map needs its own admin surface inside stream{}):

1. The admin surface is a SECOND unix socket (`stream-admin.sock`) speaking a
   minimal LINE PROTOCOL (GET / SYNC / DUMP), not HTTP — so the guest transport is
   the stdlib `stream-admin` client build.sh installs, not `curl --unix-socket`.
2. There is no build verb (the TCP stream{} config + Lua are part of the SAME
   proxy/ tree and build.sh — a proxy is HTTP+TCP from one `build_proxy`) and no
   cert push (TCP forwarding is L4 passthrough; the proxy never terminates TLS).

Each guest operation is recorded as a Task row (`script` = `tcp-proxy-sync`, with
the proxy VM) for the operator's audit trail — the same row shape as `proxy-sync`.
"""

import shlex

import frappe

from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.doctype.port_mapping.port_mapping import port_map
from atlas.atlas.placement import atlas_region
from atlas.atlas.proxy import _proxy_vms, _record_guest_task, canonical_json
from atlas.atlas.ssh import connection_for_guest

# The stream{}-side admin client build.sh installs on PATH (spec/17-tcp-proxy.md);
# the L4 analogue of `curl --unix-socket` for the http admin. It speaks the line
# protocol over /run/nginx/stream-admin.sock.
STREAM_ADMIN = "stream-admin"


def reconcile_proxies() -> list[str]:
	"""Reconcile every proxy VM to the desired port map. Returns the names of the
	proxy VMs that were synced (drifted). Each proxy holds the WHOLE map, so they
	all get the same body.

	A proxy that can't be reached is recorded as a failed Task and skipped — the
	other proxies still serve, so one wedged guest never wedges the loop. Identical
	guarantees and shape as proxy.reconcile_proxies."""
	desired_json = canonical_json(port_map())
	synced = []
	for vm_name in _proxy_vms():
		try:
			if _reconcile_proxy(vm_name, desired_json):
				synced.append(vm_name)
		except Exception as exception:
			frappe.log_error(f"TCP proxy reconcile failed for {vm_name}: {exception}", "TCP proxy reconcile")
	return synced


def reconcile_proxy(virtual_machine: str) -> bool:
	"""Reconcile a single proxy VM to the desired port map. Returns True iff a sync
	was needed (the live map had drifted)."""
	desired_json = canonical_json(port_map())
	return _reconcile_proxy(virtual_machine, desired_json)


def _reconcile_proxy(virtual_machine: str, desired_json: str) -> bool:
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		# 1. Read the live map. The stream admin serves the SAME canonical bytes
		#    stream_persist.serialize emits (byte-for-byte twin of persist.lua), so
		#    the compare below is exact — the same string-compare proxy.py does.
		live_json, _stderr, _code = run_ssh(
			connection, key_path, _stream_admin_command("GET"), timeout_seconds=60
		)
		if live_json == desired_json:
			return False
		# 2. Drift: bulk declarative SYNC the full desired map (idempotent,
		#    self-healing, rebuild-safe). Stream the canonical body to the guest
		#    client's stdin, no file staged on the guest.
		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			_stream_admin_command("SYNC"),
			timeout_seconds=120,
			stdin=desired_json,
		)
	_record_guest_task(virtual_machine, "tcp-proxy-sync", {"region": atlas_region()}, stdout, stderr, code)
	if code != 0:
		frappe.throw(f"TCP proxy sync to {virtual_machine} failed (exit {code}): {stderr[-500:]}")
	# The client prints "ok\n" on a successful SYNC and "error...\n" otherwise; a
	# clean exit with an error reply is still a failure to record loudly.
	if not stdout.startswith("ok"):
		frappe.throw(f"TCP proxy sync to {virtual_machine} rejected: {stdout.strip()!r}")
	return True


def _stream_admin_command(verb: str) -> str:
	"""The guest-side `stream-admin <verb>` invocation. For SYNC the body is read
	from the SSH stdin stream by the client (it reads stdin for SYNC), so the
	command itself carries no body — exactly as the http side streams the body to
	`curl --data-binary @-` over stdin."""
	return f"{shlex.quote(STREAM_ADMIN)} {shlex.quote(verb)}"
