import frappe


def execute():
	"""Bring every Active host to the boat generation this release ships.

	Atlas routes ten host verbs and every VM lifecycle verb at `boat`, so a host
	running an older daemon runs new Atlas against an old boat — one verb at a time,
	mid-flow. Until now a new boat binary, a new sudoers allow-list line or an
	edited unit could only reach a host through a full re-`bootstrap`: `sync_scripts`
	ships neither the binary nor the units by design. So a fleet already bootstrapped
	on an older boat had no path to the current generation short of re-bootstrapping
	each host by hand.

	This patch closes that on upgrade. It enqueues one `Server.upgrade_boat` per
	Active host — non-blocking, because a migrate must never hang on N privileged
	SSH installs, and one unreachable host must not stall the rest — which re-ships
	the binary, the allow-list and the units, reloads systemd, restarts the daemon
	onto the new inode, and reinstalls the durable scripts. Idempotent: a host
	already current is re-shipped identical bytes and left as it was.

	Runs once (Frappe records executed patches), not on every migrate. A no-op on a
	site with no Active hosts (a fresh or dev site); Fake hosts skip their host steps
	inside `upgrade_boat`."""
	from atlas.atlas.doctype.server.server import upgrade_all_hosts_to_current_boat

	queued = upgrade_all_hosts_to_current_boat(enqueue=True)
	if queued:
		frappe.logger("atlas").info(
			f"upgrade_hosts_to_current_boat: queued {len(queued)} host(s): {sorted(queued)}"
		)
