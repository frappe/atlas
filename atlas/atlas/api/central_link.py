"""Atlas inbound provisioning API — the surface Central drives during the reversed
registration handshake (spec/21-tunnel.md § "Atlas inbound API").

Central authenticates every call with THIS Atlas's admin token (System Manager).
These three are the only new inbound methods:

- `provision_tunnel` (over the public base_url): bring up the spoke `wg0`
  (tunnel-up.py), lock the public interface with the auto-revert ARMED
  (mgmt-firewall-apply.py), then store the pushed Central service-user creds + tunnel
  parameters in Central Settings. Returns this host's wg public key + listen port so
  the hub can add the peer. Idempotent.
- `confirm_tunnel` (over the tunnel): persist the lockdown + cancel the auto-revert
  (mgmt-firewall-confirm.py) and flip `tunnel_status` Active. Arriving over `wg0` is itself
  the proof of end-to-end reachability that makes the lockdown safe to keep.
- `tunnel_status`: read-back for diagnostics.

Privileged host work runs through `run_local_task` (audited Task rows), exactly like
issue-cert.py; the wg/nft/systemd commands are sudoers-pinned (scripts/sudoers.d/
atlas-tunnel). All three guard on System Manager — the Atlas admin identity Central
holds.
"""

from __future__ import annotations

import frappe

from atlas.atlas.local_task import run_local_task
from atlas.atlas.task_results import parse_result

# The spoke private key lives 0600 at this path; tunnel-up.py generates it if absent
# (sudoers pins cat/install on /etc/wireguard/*) and reuses it on re-provision, so this
# Atlas's public key is stable. `wg0` is the spoke interface throughout (spec/21-tunnel.md).
SPOKE_PRIVATE_KEY_PATH = "/etc/wireguard/wg0.key"

_REQUIRED = (
	"atlas_id",
	"hub_public_key",
	"hub_endpoint",
	"tunnel_ip",
	"tunnel_cidr",
	"central_url",
	"service_api_key",
	"service_api_secret",
)


@frappe.whitelist()
def provision_tunnel(**payload) -> dict:
	"""Bring up this Atlas's end of the tunnel and lock its public face down.

	Central calls this over the public base_url with the Atlas admin token, pushing
	the hub's identity + this Atlas's allocated tunnel address + the per-Atlas Central
	service-user creds. We (1) run tunnel-up.py to bring up `wg0` (generating our
	keypair if absent), (2) mgmt-firewall-apply.py to default-deny the public interface with
	the auto-revert ARMED — until confirm_tunnel arrives the host re-opens itself, so a
	failed handoff can never lock Central out — then (3) store the creds + tunnel params
	in Central Settings. Returns `{wg_public_key, listen_port, tunnel_ip}` so the hub can
	peer. Idempotent: re-running re-asserts `wg0` + the firewall with the same keypair.
	"""
	frappe.only_for("System Manager")
	missing = [key for key in _REQUIRED if not payload.get(key)]
	if missing:
		frappe.throw(f"provision_tunnel missing required fields: {', '.join(missing)}")

	tunnel_task = run_local_task(
		script="tunnel-up.py",
		variables={
			"PRIVATE_KEY_PATH": SPOKE_PRIVATE_KEY_PATH,
			"TUNNEL_IP": payload["tunnel_ip"],
			"TUNNEL_CIDR": payload["tunnel_cidr"],
			"HUB_PUBLIC_KEY": payload["hub_public_key"],
			"HUB_ENDPOINT": payload["hub_endpoint"],
		},
	)
	result = parse_result(tunnel_task.stdout)

	# Lock the public interface with the auto-revert ARMED. No flags: mgmt-firewall-apply.py
	# discovers the public interface from the default route and uses the default wg port
	# (51820), revert window, and empty public_allow_ports — confirm_tunnel re-discovers
	# the same way, so the persisted ruleset will match this live one.
	run_local_task(script="mgmt-firewall-apply.py", variables={})

	_store_provisioning(payload, result)

	return {
		"wg_public_key": result["wg_public_key"],
		"listen_port": result["listen_port"],
		"tunnel_ip": result["tunnel_ip"],
	}


def _store_provisioning(payload: dict, result: dict) -> None:
	"""Write the pushed Central service-user creds + tunnel parameters into the Central
	Settings single. url/api_key/api_secret now hold the per-Atlas Central service-user
	creds (no longer hand-entered) — Central rotates them by re-provisioning, so we
	overwrite them every time. The Password (api_secret) is encrypted on save."""
	settings = frappe.get_single("Central Settings")
	settings.url = payload["central_url"]
	settings.api_key = payload["service_api_key"]
	settings.api_secret = payload["service_api_secret"]
	settings.atlas_id = payload["atlas_id"]
	settings.tunnel_ip = payload["tunnel_ip"]
	settings.tunnel_cidr = payload["tunnel_cidr"]
	settings.hub_public_key = payload["hub_public_key"]
	settings.hub_endpoint = payload["hub_endpoint"]
	settings.wg_public_key = result["wg_public_key"]
	settings.wg_listen_port = result["listen_port"]
	settings.tunnel_status = "Provisioning"
	settings.save(ignore_permissions=True)


@frappe.whitelist()
def confirm_tunnel() -> dict:
	"""Persist the lockdown and cancel the auto-revert — called by Central OVER the
	tunnel. Reaching this method over `wg0` is itself the proof that Central can already
	talk to Atlas privately, so it is safe to make the public side permanently dark.
	Runs mgmt-firewall-confirm.py (cancel the timer + write the fail-closed boot ruleset) and
	flips `tunnel_status` Active.
	"""
	frappe.only_for("System Manager")
	run_local_task(script="mgmt-firewall-confirm.py", variables={})
	frappe.db.set_single_value("Central Settings", "tunnel_status", "Active")
	return {"tunnel_status": "Active"}


@frappe.whitelist()
def deprovision_tunnel() -> dict:
	"""Tear down this Atlas's tunnel + management firewall — the inverse of
	provision_tunnel. Reverts the firewall first (mgmt-firewall-revert.py: restore public
	access, drop the persisted ruleset, disable the boot unit), then tears wg0
	(tunnel-down.py), then clears the tunnel fields in Central Settings. Both scripts
	are idempotent (best-effort, `check=False`), so this is safe to re-run.

	Central calls this over the tunnel while Active: firewall-revert reopens the public
	interface and tunnel-down then drops wg0, so the HTTP response races the teardown.
	The work has already committed host-side regardless; Central tolerates the dropped
	connection and re-verifies over the now-public base_url (see central remove_tunnel).
	"""
	frappe.only_for("System Manager")
	run_local_task(script="mgmt-firewall-revert.py", variables={})
	run_local_task(script="tunnel-down.py", variables={})

	settings = frappe.get_single("Central Settings")
	settings.tunnel_status = "Inactive"
	for field in (
		"tunnel_ip",
		"tunnel_cidr",
		"hub_public_key",
		"hub_endpoint",
		"wg_public_key",
		"wg_listen_port",
	):
		settings.set(field, None)
	settings.save(ignore_permissions=True)
	return {"tunnel_status": "Inactive"}


@frappe.whitelist()
def tunnel_status() -> dict:
	"""Read-back of the tunnel fields for diagnostics."""
	frappe.only_for("System Manager")
	settings = frappe.get_single("Central Settings")
	return {
		"tunnel_status": settings.tunnel_status or "Inactive",
		"tunnel_ip": settings.tunnel_ip,
		"wg_public_key": settings.wg_public_key,
		"wg_listen_port": settings.wg_listen_port,
	}
