"""WireGuard spoke plumbing for an Atlas host — the host-side of the Central tunnel.

The spoke mirror of Central's `lib/central/wireguard.py` (spec/21-tunnel.md): one
`wg0` with exactly one peer — the Central hub. The Atlas host keeps a stable public
IP and its `wg` listens on a public UDP port (the one thing its public firewall lets
in, see `firewall.py`); the hub dials it, and the spoke is configured with the hub as
a peer so either side can (re)handshake.

Same split as `reserved_ip_nat.py`: `spoke_conf` is pure string construction
(unit-testable with bare `python3 -m unittest`); only `ensure_keypair`,
`ensure_interface`, `down` touch the host.
"""

from __future__ import annotations

from atlas._run import install_file, run, run_input, run_ok

WG_DIR = "/etc/wireguard"


def conf_path(interface: str) -> str:
	return f"{WG_DIR}/{interface}.conf"


# --- pure builder (unit-testable, no host) ----------------------------------


def spoke_conf(
	private_key: str,
	address: str,
	listen_port: int,
	hub_public_key: str,
	hub_endpoint: str,
	allowed_ips: str,
	keepalive: int = 25,
) -> str:
	"""A complete spoke `wg0.conf`: the `[Interface]` plus the single `[Peer]` (the
	hub). Unlike the hub's conf (dynamic peers, persisted with `wg-quick save`), the
	spoke's is fully determined by the provision payload, so it is regenerated whole
	on every apply. `address` is the assigned `/32` (e.g. `10.88.0.2/32`); the hub
	peer's `AllowedIPs` is the tunnel CIDR so all tunnel traffic routes to the hub;
	`PersistentKeepalive` holds the session open through this host's own firewall."""
	return (
		"[Interface]\n"
		f"PrivateKey = {private_key}\n"
		f"Address = {address}\n"
		f"ListenPort = {listen_port}\n"
		"\n"
		"[Peer]\n"
		f"PublicKey = {hub_public_key}\n"
		f"Endpoint = {hub_endpoint}\n"
		f"AllowedIPs = {allowed_ips}\n"
		f"PersistentKeepalive = {keepalive}\n"
	)


# --- host functions (idempotent) --------------------------------------------


def _exists_as_root(path: str) -> bool:
	return run_ok("sudo test -f {}", path)


def interface_is_up(interface: str) -> bool:
	return run_ok("sudo wg show {}", interface)


def ensure_keypair(private_key_path: str) -> str:
	"""Ensure a `0600` WireGuard private key exists at `private_key_path` (generate it
	with `wg genkey` if absent — the private key never leaves the host) and return the
	public key. Idempotent: an existing key is read, not regenerated, so this Atlas's
	public key is stable across re-provision."""
	if _exists_as_root(private_key_path):
		private_key = run("sudo cat {}", private_key_path).strip()
	else:
		private_key = run("wg genkey").strip()
		install_file(private_key + "\n", private_key_path, mode="0600", sudo=True)
	return run_input("wg pubkey", stdin=private_key).strip()


def ensure_interface(
	interface: str,
	private_key_path: str,
	address: str,
	listen_port: int,
	hub_public_key: str,
	hub_endpoint: str,
	allowed_ips: str,
	keepalive: int = 25,
) -> None:
	"""Write the spoke `wg0.conf` from the stored key + the provision payload and
	bring the interface up (down-then-up if already up, so a re-provision with changed
	peer/address takes effect — a brief drop is fine: tunnel-up runs during
	provisioning, before the tunnel is load-bearing). Enable `wg-quick@<interface>`
	for reboot persistence."""
	private_key = run("sudo cat {}", private_key_path).strip()
	conf = spoke_conf(private_key, address, listen_port, hub_public_key, hub_endpoint, allowed_ips, keepalive)
	install_file(conf, conf_path(interface), mode="0600", sudo=True)
	if interface_is_up(interface):
		run("sudo wg-quick down {}", interface)
	run("sudo wg-quick up {}", interface)
	run("sudo systemctl enable {}", f"wg-quick@{interface}")


def down(interface: str) -> None:
	"""Tear the interface down and disable its unit (the rollback path). Best-effort —
	a missing interface/unit is not an error."""
	run("sudo wg-quick down {}", interface, check=False)
	run("sudo systemctl disable {}", f"wg-quick@{interface}", check=False)
