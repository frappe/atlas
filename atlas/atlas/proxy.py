"""Proxy control plane — Atlas reconciles each proxy guest's live map (and pushes
the wildcard cert) over SSH-to-the-guest.

Atlas is the source of truth; each proxy VM's `lua_shared_dict` is a cache
(spec principle #2). This module is the controller side of the design's §7:
build the desired regional map from the `Subdomain` rows, serialize it the SAME
canonical way the guest's persist.lua does (so "in sync?" is a byte compare),
SSH into each proxy guest, read its live `/map` off the unix-socket admin API,
and bulk-`/sync` the full map on drift. Cert push uses the same guest-SSH path.

This is NOT a host Task (which stages a script onto a Server and runs it there):
it runs on the controller and SSHes *into the guest* (the second SSH target,
`connection_for_guest`). Each guest operation is still recorded as a Task row for
the operator's audit trail, with a synthetic script name (`proxy-sync` /
`proxy-push-cert`) and the proxy VM in `virtual_machine`.
"""

import json
import shlex

import frappe

from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.doctype.subdomain.subdomain import subdomain_map
from atlas.atlas.placement import atlas_region
from atlas.atlas.ssh import connection_for_guest

# Paths mirror the stock Ubuntu `nginx` package (config /etc/nginx, state
# /var/lib/nginx, socket /run/nginx, binary /usr/sbin/nginx) so the guest looks
# like a default nginx box to anyone debugging it.
ADMIN_SOCKET = "/run/nginx/admin.sock"
CERT_DIRECTORY = "/var/lib/nginx/certs"
# The guest file build.sh leaves empty and the proxy recipe's finalize step writes
# the real region into (image_recipes._finalize_proxy); init_by_lua reads it.
REGION_FILE = "/var/lib/nginx/region"
# The guest admin API answers HTTP over the unix socket; the host part is ignored
# but curl needs one, so use a fixed placeholder.
ADMIN_BASE = "http://localhost"


def canonical_json(site_map: dict[str, str]) -> str:
	"""The one canonical serialization of a subdomain→address map, byte-identical
	to the guest's persist.lua output: sorted keys, 2-space indent, one key per
	line, trailing newline. Because both sides emit the same bytes, the reconcile
	"in sync?" check is a plain string compare — no semantic diff (design §4.3,
	§7.2)."""
	return json.dumps(site_map, sort_keys=True, indent=2) + "\n"


def reconcile_proxies() -> list[str]:
	"""Reconcile every proxy VM to the desired map. Returns the names of the proxy
	VMs that were synced (drifted). Each proxy holds the WHOLE map (design §1
	non-goals), so they all get the same body.

	A proxy that can't be reached is recorded as a failed Task and skipped — the
	other proxies still serve, so one wedged guest never wedges the loop (§7.3)."""
	desired_json = canonical_json(subdomain_map())
	synced = []
	for vm_name in _proxy_vms():
		try:
			if _reconcile_proxy(vm_name, desired_json):
				synced.append(vm_name)
		except Exception as exception:
			# Record the failure on the Task row (done inside _reconcile_proxy's
			# guest-task wrapper) and move to the next proxy. Don't abort the loop.
			frappe.log_error(f"Proxy reconcile failed for {vm_name}: {exception}", "Proxy reconcile")
	return synced


def reconcile_proxy(virtual_machine: str) -> bool:
	"""Reconcile a single proxy VM to the desired map. Returns True iff a sync was
	needed (the live map had drifted)."""
	desired_json = canonical_json(subdomain_map())
	return _reconcile_proxy(virtual_machine, desired_json)


def _reconcile_proxy(virtual_machine: str, desired_json: str) -> bool:
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		# 1. Read the live map. The admin API serves the SAME canonical bytes, so
		#    the compare below is exact.
		live_json, _stderr, _code = run_ssh(
			connection, key_path, _curl_command("GET", "/map"), timeout_seconds=60
		)
		if live_json == desired_json:
			return False
		# 2. Drift: bulk declarative /sync the full desired map (idempotent,
		#    self-healing, rebuild-safe — design §7.2). Stream the body to the
		#    guest curl's stdin (--data-binary @-), no file staged on the guest.
		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			_curl_command("POST", "/sync", data_stdin=True),
			timeout_seconds=120,
			stdin=desired_json,
		)
	_record_guest_task(virtual_machine, "proxy-sync", {"region": atlas_region()}, stdout, stderr, code)
	if code != 0:
		frappe.throw(f"Proxy sync to {virtual_machine} failed (exit {code}): {stderr[-500:]}")
	return True


def push_cert(virtual_machine: str, fullchain: str, privkey: str) -> None:
	"""Push the regional wildcard cert into a proxy guest and reload nginx.

	Drops fullchain.pem/privkey.pem into the guest's per-region cert dir over the
	same guest-SSH path as the map sync, then reloads (a reload is fine here —
	cert changes are rare, unlike map changes; design §7.3). The cert is pushed,
	never baked into the image, so one proxy image serves any region and a renewed
	cert is a re-push, not a rebuild (§5.3).

	The cert dir is still scoped under this instance's `Atlas Settings.region`, so a
	later customer-domain feature can serve more than one cert per proxy without an
	image roll — and the on-guest layout is byte-identical to what the proxies were
	baked/pushed with, so removing the per-VM region field needs no cert move."""
	region = atlas_region()
	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	connection = connection_for_guest(vm)
	cert_dir = f"{CERT_DIRECTORY}/{region}"
	with ssh_key_file(connection.ssh_private_key) as key_path:
		# Write both PEMs and reload in one round trip. The key is 0600; the dir is
		# created first. `tee` writes from stdin so the private key never lands in
		# a process argv (which `ps` could read). Two tees → two stdin streams, so
		# do them as separate commands but in one SSH session via `&&`.
		_write_guest_file(
			connection, key_path, f"{cert_dir}/fullchain.pem", fullchain, mode="0644", make_dir=cert_dir
		)
		_write_guest_file(connection, key_path, f"{cert_dir}/privkey.pem", privkey, mode="0600")
		# Point the flat cert symlink nginx reads at this region's dir (idempotent;
		# self-sufficient so a cert push takes effect even on a guest whose symlink
		# still aims at the build-time _placeholder), then reload.
		stdout, stderr, code = run_ssh(
			connection,
			key_path,
			f"{_point_cert_symlink_command(region)} && /usr/sbin/nginx -s reload",
			timeout_seconds=60,
		)
	_record_guest_task(virtual_machine, "proxy-push-cert", {"region": region}, stdout, stderr, code)
	if code != 0:
		frappe.throw(f"Cert push/reload to {virtual_machine} failed (exit {code}): {stderr[-500:]}")


def build_proxy(virtual_machine: str) -> None:
	"""Turn a freshly-provisioned Ubuntu guest into a proxy: upload the committed
	`proxy/` tree and run build.sh inside the guest, then write the region and
	start the unit.

	This is the controller side of the design's §3.1 ("compile nginx+Lua inside
	the guest"): the same SSH-to-the-guest path the map sync uses, pointed at a
	bare VM. The upload+build+finalize+audit is the shared `image_builder.run_build`
	seam handed the `proxy` recipe (its finalize, `image_recipes._finalize_proxy`,
	writes the region + restarts the unit); this wrapper keeps the proxy-only
	guards. build.sh is the AUTHORITATIVE build the compose release gate also
	exercises (proxy/test/Dockerfile runs the same script), so a built guest runs
	the byte-identical stack. Idempotent, so this doubles as the "re-bake" verb.

	Recorded as a `proxy-build` Task row for the audit trail, like every guest op.
	"""
	from atlas.atlas.image_builder import run_build
	from atlas.atlas.image_recipes import get_recipe

	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	if not vm.is_proxy:
		frappe.throw(f"Virtual Machine {virtual_machine} is not a proxy (is_proxy unset)")
	# stream=True (spec/22 sample): surface the proxy-build Task as Running and tail
	# its in-guest nginx+luajit compile live, instead of writing the row only on
	# completion. The 10-20 min build is exactly the case the streamed view is for.
	run_build(virtual_machine, get_recipe("proxy"), stream=True)


def _remote_parent(remote_path: str) -> str:
	parent = remote_path.rsplit("/", 1)[0]
	return parent or "/"


def _write_guest_file(
	connection, key_path, path: str, content: str, mode: str, make_dir: str | None = None
) -> None:
	"""Write `content` to `path` in the guest via `tee` (content arrives on stdin,
	never in argv), then chmod. Optionally mkdir -p the parent first."""
	quoted = shlex.quote(path)
	command = ""
	if make_dir:
		command += f"mkdir -p {shlex.quote(make_dir)} && "
	command += f"tee {quoted} >/dev/null && chmod {mode} {quoted}"
	_stdout, stderr, code = run_ssh(connection, key_path, command, timeout_seconds=60, stdin=content)
	if code != 0:
		frappe.throw(f"Writing {path} to guest failed (exit {code}): {stderr[-300:]}")


def _point_cert_symlink_command(region: str) -> str:
	"""Shell to repoint the flat cert path nginx reads (CERT_DIRECTORY/{fullchain,
	privkey}.pem) at this region's cert dir. nginx's static ssl_certificate can't
	interpolate the region, so it reads a flat symlink; build.sh aims it at the
	`_placeholder` region, and this moves it to certs/<region>/ once the real cert
	is in place. Relative targets (so the link stays valid regardless of where
	certs/ is mounted) and `-n` so we replace the link, not follow it on a re-run.
	Idempotent."""
	return (
		f"ln -sfn {shlex.quote(f'{region}/fullchain.pem')} {shlex.quote(f'{CERT_DIRECTORY}/fullchain.pem')} && "
		f"ln -sfn {shlex.quote(f'{region}/privkey.pem')} {shlex.quote(f'{CERT_DIRECTORY}/privkey.pem')}"
	)


def _curl_command(method: str, path: str, data_stdin: bool = False) -> str:
	"""Build the guest-side `curl --unix-socket` invocation. With data_stdin the
	body is read from the SSH stdin stream (--data-binary @-)."""
	parts = [
		"curl",
		"-s",
		"--fail-with-body",
		"--unix-socket",
		ADMIN_SOCKET,
		"-X",
		method,
	]
	if data_stdin:
		parts += ["--data-binary", "@-"]
	parts.append(f"{ADMIN_BASE}{path}")
	return " ".join(shlex.quote(p) for p in parts)


def _proxy_vms() -> list[str]:
	"""Every VM marked is_proxy. These are the reconcile targets; each gets the full
	map."""
	return frappe.get_all(
		"Virtual Machine",
		filters={"is_proxy": 1},
		pluck="name",
	)


def wildcard_targets() -> tuple[list[str], list[str]]:
	"""The proxy fleet's public addresses the regional wildcard should resolve to:
	(ipv4, ipv6). AAAA = each proxy VM's `/128`; A = the Reserved IP attached to
	each proxy (a proxy without an attached reserved IP contributes no v4). Both are
	round-robin sets (spec/12-proxy.md: "DNS round-robin over their v4 + v6")."""
	ipv4: list[str] = []
	ipv6: list[str] = []
	for vm_name in _proxy_vms():
		vm_ipv6 = frappe.db.get_value("Virtual Machine", vm_name, "ipv6_address")
		if vm_ipv6:
			ipv6.append(vm_ipv6)
		reserved_ipv4 = frappe.db.get_value("Reserved IP", {"virtual_machine": vm_name}, "ip_address")
		if reserved_ipv4:
			ipv4.append(reserved_ipv4)
	return ipv4, ipv6


def _record_guest_task(
	virtual_machine: str, script: str, variables: dict, stdout: str, stderr: str, exit_code: int
) -> str:
	"""Record one guest-SSH operation as a Task row for the operator's audit
	trail. Unlike host Tasks this isn't a staged script — the `script` is a
	synthetic name and there are no uploads — but the row shape (status, output,
	exit code) is identical, so the operator sees proxy reconciles in the same
	Task list as every other action. Returns the Task's name so a caller (the
	Image Build controller) can link it for the audit trail."""
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"server": frappe.db.get_value("Virtual Machine", virtual_machine, "server"),
			"virtual_machine": virtual_machine,
			"script": script,
			"status": "Success" if exit_code == 0 else "Failure",
			"triggered_by": frappe.session.user if frappe.session else "Administrator",
			"stdout": stdout,
			"stderr": stderr,
			"exit_code": exit_code,
			"ended": frappe.utils.now_datetime(),
		}
	)
	task.variables_dict = variables
	task.insert(ignore_permissions=True)
	return task.name
