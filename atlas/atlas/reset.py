"""Reset a bootstrapped Server back to its just-bootstrapped state — for a host
whose on-disk state has drifted away from the Frappe DB.

Run:

    bench --site <site> execute atlas.atlas.reset.census --kwargs '{"server": "<name-or-title>"}'
    bench --site <site> execute atlas.atlas.reset.reset  --kwargs '{"server": "<name-or-title>", "confirm": "<title>"}'

`census(server)` is READ-ONLY: it SSHes the host and prints exactly what a reset
would destroy (VM dirs, units, LVs, netns, links, nbd, migration tunnels). Run it
first — it changes nothing.

`reset(server, confirm)` is DESTRUCTIVE and irreversible. It refuses to run unless
`confirm` exactly matches the server's title, so a reset is always deliberate and
one host at a time — there is no fleet-wide sweep. It then:

  1. runs the `reset-server` Task on the host (scripts/reset-server.py), wiping
     every VM/image/snapshot/tunnel/networking artifact off it while KEEPING the
     bootstrap floor (atlas VG + empty pool0, the venv, host hardening) so the
     host stays provision-ready without a re-bootstrap; and
  2. hard-deletes the DB rows that only had meaning while the host held that
     state — its Virtual Machines (site VMs AND the region's proxy VM), Snapshots,
     Image Builds, Reserved IPs, Tasks, plus the proxy-dependent rows that are
     orphaned once the proxy VM is gone: the Subdomains those VMs served and the
     Sites those subdomains fronted. The Server row itself is kept and its status
     reset to Active.

`developer_mode`-gated: this is an operator recovery tool for a dev/staging
controller, never a routine production action. It throws on a non-developer_mode
site.
"""

from __future__ import annotations

import frappe
from frappe import _

# The host-side wipe verb (scripts/reset-server.py). run_task stages the atlas
# package + runs `atlas reset-server` over SSH, recording an auditable Task row.
RESET_VERB = "reset-server"


def require_developer_mode() -> None:
	"""Throw unless the site is in developer_mode. This is a recovery tool for a
	drifted host, gated off production exactly like the Fake provider."""
	if not frappe.conf.developer_mode:
		frappe.throw(_("atlas.atlas.reset is only available when developer_mode is enabled"))


def census(server: str) -> dict:
	"""READ-ONLY. SSH the host and report what a reset would destroy. Prints a
	human summary and returns the raw counts. Changes nothing on host or in DB."""
	require_developer_mode()
	doc = _resolve_server(server)
	host = _census_host(doc)
	rows = _db_footprint(doc.name)

	print(f"\n=== {doc.title} ({doc.name}) — {doc.ipv4_address} ===")
	print("ON HOST (would be wiped; bootstrap floor kept):")
	for label, value in host.items():
		print(f"  {label:28} {value}")
	print("IN DB (would be hard-deleted; Server row kept):")
	for label, value in rows.items():
		print(f"  {label:28} {value}")
	print("\nRun reset(server=..., confirm=<title>) to wipe. This is DESTRUCTIVE and cannot be undone.\n")
	return {"host": host, "db": rows}


def reset(server: str, confirm: str, delete_server: bool = False, skip_host: bool = False) -> None:
	"""DESTRUCTIVE. Wipe the host to its just-bootstrapped state, then delete the
	DB rows that had meaning only while it held that state. Refuses unless
	`confirm` == the server's title (one host at a time, always deliberate).

	`delete_server=True` also deletes the Server row itself (for a bogus
	test-fixture host with no real machine behind it). Default keeps the row and
	just resets its status — the real host is still bootstrapped, only emptied.

	`skip_host=True` skips the on-host wipe and cleans the DB only — for a Server
	whose host is unreachable (a fixture, or an already-gone machine). census()
	reports reachability; a truly unreachable host is skipped automatically with a
	warning, so this flag is only needed to force a DB-only clean of a REACHABLE
	host."""
	require_developer_mode()
	doc = _resolve_server(server)

	if confirm != doc.title:
		frappe.throw(
			_(
				'Refusing to reset {0}: pass confirm="{1}" (the server\'s exact title) '
				"to proceed. Reset is destructive and irreversible."
			).format(doc.name, doc.title)
		)

	# Show the operator what is about to be destroyed, from a fresh read. The
	# census doubles as the reachability probe: if it can't reach the host, we do
	# not attempt (and cannot) a host wipe — we clean the DB only.
	report = census(doc.name)
	reachable = report["host"].get("reachable") == "yes"
	print(f">>> Resetting {doc.title} — wiping host + DB rows...\n")

	# 1. Wipe the host, unless it's unreachable or the caller opted out. run_task
	#    raises on any SSH/exit failure, with the Task row recorded either way — so
	#    a host wipe that half-fails is auditable and the DB rows are NOT deleted
	#    (we never reach step 2).
	if skip_host or not reachable:
		reason = (
			"skip_host requested" if skip_host else f"host unreachable ({report['host'].get('reachable')})"
		)
		print(f"!! Skipping on-host wipe — {reason}. Cleaning DB only.")
	else:
		from atlas.atlas.ssh import run_task

		# reset-server is a python VERB: run_task runs the pip-installed
		# `atlas reset-server` on PATH rather than scp-ing the file per Task. A host
		# whose durable /var/lib/atlas/bin predates this script won't have the verb,
		# so refresh the durable package first (idempotent scp sweep, no bootstrap,
		# no daemon-reload). The atlas CLI derives its subcommands from the on-disk
		# entry scripts, so the verb is dispatchable the moment the file lands.
		count = doc.sync_scripts()
		print(f"Synced {count} durable script(s) to the host.")

		task = run_task(script=RESET_VERB, variables={}, server=doc.name, timeout_seconds=1800)
		print(f"Host wipe Task {task.name}: {task.status}")

	# 2. Delete the now-meaningless DB rows, dependents before parents.
	deleted = _delete_db_rows(doc.name)
	for doctype, count in deleted.items():
		if count:
			print(f"Deleted {count} {doctype} row(s)")

	# 3. Either delete the Server row (fixture) or reset it to a clean Active state
	#    (real host: row kept, still bootstrapped, just empty). set_value bypasses
	#    the immutable-field validate (status is not immutable) and touches nothing
	#    else.
	if delete_server:
		frappe.delete_doc("Server", doc.name, force=True, ignore_permissions=True, delete_permanently=True)
		print(f"Deleted Server row {doc.title} ({doc.name})")
	else:
		frappe.db.set_value("Server", doc.name, "status", "Active", update_modified=False)

	# nosemgrep: frappe-manual-commit -- recovery tool: persist the host wipe's DB cleanup as one durable unit
	frappe.db.commit()
	outcome = "row deleted" if delete_server else "reset to its just-bootstrapped state"
	print(f"\n{doc.title} {outcome}.\n")


# --- server resolution --------------------------------------------------------


def _resolve_server(server: str):
	"""Accept a Server name (UUID) or its title; return the doc. Throws if the
	title is ambiguous or nothing matches — a reset must target one exact host."""
	if frappe.db.exists("Server", server):
		return frappe.get_doc("Server", server)
	matches = frappe.get_all("Server", filters={"title": server}, pluck="name")
	if not matches:
		frappe.throw(_("No Server matches {0} (by name or title)").format(server))
	if len(matches) > 1:
		frappe.throw(_("{0} is ambiguous — {1} servers share that title").format(server, len(matches)))
	return frappe.get_doc("Server", matches[0])


def verify_dispatch(server: str) -> None:
	"""Sync the durable scripts to the host and prove `atlas reset-server` is
	dispatchable there — running only its `--help` (which wipes nothing). A safe
	end-to-end check of the SSH + CLI wiring before committing to a real wipe."""
	require_developer_mode()
	import atlas
	from atlas.atlas.ssh import connection_for_server, run_ssh

	doc = _resolve_server(server)
	count = doc.sync_scripts()
	print(f"Synced {count} durable script(s) to {doc.title}.")

	connection = connection_for_server(doc)
	key = atlas.get_ssh_private_key_path()
	out, err, rc = run_ssh(connection, key, "atlas reset-server --help", timeout_seconds=60)
	print(f"`atlas reset-server --help` rc={rc}")
	print(out or err)
	if rc != 0:
		frappe.throw(_("reset-server verb is NOT dispatchable on {0}").format(doc.title))
	print(f"reset-server is dispatchable on {doc.title}.")


# --- read-only host census ----------------------------------------------------


def _census_host(doc) -> dict:
	"""SSH the host once and count the artifacts a reset would remove. Read-only.
	A host that is unreachable (already gone, or a Fake/private-IP fixture) yields
	an 'unreachable' marker rather than raising — the DB-only cleanup still runs."""
	import atlas
	from atlas.atlas.ssh import connection_for_server, run_ssh

	try:
		connection = connection_for_server(doc)
		key = atlas.get_ssh_private_key_path()
	except Exception as exc:  # no ipv4, no key, etc. — a fixture row, not a host.
		return {"reachable": f"no (setup: {exc})"}

	probe = (
		"echo VM_DIRS=$(ls -1 /var/lib/atlas/virtual-machines/ 2>/dev/null | wc -l); "
		"echo FC_UNITS=$(systemctl list-units 'firecracker-vm@*' --all --no-legend --plain "
		"2>/dev/null | awk '{print $1}' | grep -v atlas-pool | grep -c .); "
		"echo MIG_UNITS=$(systemctl list-units 'atlas-mig6-*' --all --no-legend --plain "
		"2>/dev/null | grep -c .); "
		"echo VM_LVS=$(sudo lvs --noheadings -o lv_name atlas 2>/dev/null | "
		"grep -Ec 'atlas-(vm|data|snap|datasnap|clonemeta)-'); "
		"echo IMAGE_LVS=$(sudo lvs --noheadings -o lv_name atlas 2>/dev/null | grep -c 'atlas-image-'); "
		"echo IMAGE_DIRS=$(ls -1 /var/lib/atlas/images/ 2>/dev/null | wc -l); "
		"echo SNAP_DIRS=$(ls -1 /var/lib/atlas/snapshots/ 2>/dev/null | wc -l); "
		"echo NETNS=$(ip netns list 2>/dev/null | grep -c atlas-); "
		"echo LINKS=$(ip -o link show 2>/dev/null | awk -F': ' '$2 ~ /^(veth-|tap|mig6-)/' | grep -c .); "
		"echo NBD=$(for d in /sys/block/nbd*; do s=$(cat $d/size 2>/dev/null); "
		'[ "$s" != 0 ] && [ -n "$s" ] && echo x; done | grep -c .); '
		"echo NDP=$(ip -6 neigh show proxy 2>/dev/null | grep -c .)"
	)
	out, err, rc = run_ssh(connection, key, probe, timeout_seconds=90)
	if rc != 0:
		return {"reachable": f"no (ssh rc={rc}: {err.strip()[:200]})"}

	counts: dict = {"reachable": "yes"}
	for line in out.splitlines():
		if "=" in line:
			label, _, value = line.strip().partition("=")
			counts[label.lower()] = value
	return counts


# --- DB footprint + deletion --------------------------------------------------

# Every doctype whose rows are tied to a Server (directly via `.server`, or via a
# Virtual Machine on that server). Order matters for deletion: dependents first.
# Site -> Subdomain -> Virtual Machine is the proxy dependency chain the user
# called out: once a Server (hence its proxy VM) is wiped, its Subdomains and
# Sites are meaningless, so they go too.


def _vm_names(server: str) -> list[str]:
	return frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name")


def _db_footprint(server: str) -> dict:
	"""Count the rows reset() would delete. Read-only."""
	vms = _vm_names(server)
	return {
		"Virtual Machine": len(vms),
		"Virtual Machine Snapshot": frappe.db.count("Virtual Machine Snapshot", {"server": server}),
		"Image Build": frappe.db.count("Image Build", {"server": server}),
		"Reserved IP": frappe.db.count("Reserved IP", {"server": server}),
		"Subdomain": _count_linked("Subdomain", "virtual_machine", vms),
		"Site": _count_linked("Site", "virtual_machine", vms),
		"Task": frappe.db.count("Task", {"server": server}),
	}


def _count_linked(doctype: str, field: str, vms: list[str]) -> int:
	if not vms:
		return 0
	return frappe.db.count(doctype, {field: ["in", vms]})


def _delete_db_rows(server: str) -> dict:
	"""Hard-delete every row tied to this server, dependents before parents.
	force + ignore_permissions so on_trash guards (e.g. an attached Reserved IP,
	a non-terminal VM status) can't block a recovery wipe."""
	vms = _vm_names(server)
	deleted: dict = {}

	# Detach any attached Reserved IP first: its on_trash refuses deletion while
	# attached. The host-side NAT is already gone (the wipe tore it down), so this
	# is a DB-only detach.
	for name in frappe.get_all("Reserved IP", filters={"server": server}, pluck="name"):
		ip = frappe.get_doc("Reserved IP", name)
		if ip.virtual_machine:
			try:
				ip.db_set("virtual_machine", None, update_modified=False)
			except Exception:
				pass

	# Sites and Subdomains first (they point AT the VMs), then the server-linked
	# rows, then the VMs themselves last.
	deleted["Site"] = _delete_where("Site", {"virtual_machine": ["in", vms]}, vms)
	deleted["Subdomain"] = _delete_where("Subdomain", {"virtual_machine": ["in", vms]}, vms)
	for doctype in ("Virtual Machine Snapshot", "Image Build", "Reserved IP", "Task"):
		deleted[doctype] = _delete_where(doctype, {"server": server}, [server])
	deleted["Virtual Machine"] = _delete_names("Virtual Machine", vms)
	return deleted


def _delete_where(doctype: str, filters: dict, guard: list) -> int:
	"""Delete every row of `doctype` matching `filters`; no-op if the guard list
	(the VM/server names the filter keys off) is empty, so an `in []` filter never
	matches the whole table."""
	if not guard:
		return 0
	names = frappe.get_all(doctype, filters=filters, pluck="name")
	return _delete_names(doctype, names)


def _delete_names(doctype: str, names: list[str]) -> int:
	count = 0
	for name in names:
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)
		count += 1
	return count
