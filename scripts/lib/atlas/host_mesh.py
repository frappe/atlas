"""Host-side WireGuard mesh bring-up (the private-plane fabric, design §3).

This is the HOST analogue of `atlas.lvm.ThinPool.ensure()` — a durable, idempotent
routine that brings the host's `wg-mesh` device up in the root netns so a rebooted
host rejoins the fabric BEFORE the controller's next reconcile, rather than being a
black hole until the backstop sweep runs. It is invoked by `host-mesh.service` at
boot (the reboot self-sufficiency path) and is safe to re-run.

The controller (`atlas/atlas/host_mesh.py`) owns the PEER set: it pushes
`/etc/wireguard/wg-mesh.conf` and `wg syncconf`s the peers on drift. This host-side
routine owns the DEVICE: create it, pin MTU, assign the host's own infra mesh
address (§2.4), set the derived private key + listen port, add the `fdaa::/16`
route, and load whatever peer config the controller last pushed. On a fresh host
the config file may be absent (not yet reconciled) — the device still comes up
peer-empty and waits, and the first controller reconcile fills in the peers.

**The key-vs-syncconf order is load-bearing** (verified on a real host): `wg
syncconf` / `wg setconf` from a config that omits `PrivateKey` CLEARS the interface
key. So the private key is set from its own 0600 file (`/etc/atlas-host-mesh.key`)
AFTER any config load, never before. `wg addconf` (used here for the boot load)
MERGES rather than rewrites, so it does not clear the key — but we still set the key
last to be robust against a future switch to setconf/syncconf.

Everything here is pure string/argv construction except `bring_up_mesh`, which
touches the host. The command generation is unit-testable with bare `python3 -m
unittest` (no host), like `reserved_ip_nat` / `wireguard`.
"""

from __future__ import annotations

import os

from atlas._run import run, run_ok
from atlas.network_env import read_network_env_optional

MESH_DEVICE = "wg-mesh"
MESH_CONFIG_PATH = "/etc/wireguard/wg-mesh.conf"
MESH_KEY_PATH = "/etc/atlas-host-mesh.key"
MESH_ENV_PATH = "/etc/atlas-host-mesh.env"

# The whole private plane routes out wg-mesh; cryptokey routing (per-peer
# AllowedIPs) delivers each /128 to the right host (design §2.3).
PRIVATE_PLANE_ROUTE = "fdaa::/16"
DEFAULT_WG_HOST_PORT = 51820
DEFAULT_WIREGUARD_MTU = 1420


def link_add_command(device: str = MESH_DEVICE) -> str:
	return f"ip link add dev {device} type wireguard"


def link_mtu_command(mtu: int, device: str = MESH_DEVICE) -> str:
	return f"ip link set dev {device} mtu {mtu}"


def link_up_command(device: str = MESH_DEVICE) -> str:
	return f"ip link set dev {device} up"


def link_del_command(device: str = MESH_DEVICE) -> str:
	return f"ip link del dev {device}"


def set_key_command(port: int, key_path: str = MESH_KEY_PATH, device: str = MESH_DEVICE) -> str:
	"""Set the derived private key (from a 0600 file, never inline) + listen port.
	MUST run AFTER any config load — see the module docstring."""
	return f"wg set {device} private-key {key_path} listen-port {port}"


def addr_add_command(mesh_address: str, device: str = MESH_DEVICE) -> str:
	"""Assign the host's OWN infra mesh /128 (§2.4) to the device. It lives on
	wg-mesh (root netns), so it is reachable only from another host across the
	tunnel (§4c), never from a guest veth."""
	return f"ip -6 addr replace {mesh_address}/128 dev {device}"


def route_add_command(device: str = MESH_DEVICE) -> str:
	"""Own the whole private plane: route fdaa::/16 out wg-mesh."""
	return f"ip -6 route replace {PRIVATE_PLANE_ROUTE} dev {device}"


def addconf_command(config_path: str = MESH_CONFIG_PATH, device: str = MESH_DEVICE) -> str:
	"""Load the controller-pushed peer config. `addconf` MERGES (does not clear the
	key); the `-` prefix on the ExecStart tolerates its absence on a fresh host."""
	return f"wg addconf {device} {config_path}"


def bring_up_mesh() -> None:
	"""Idempotently bring `wg-mesh` up in the host root netns (design §3). Reads
	`/etc/atlas-host-mesh.env` for the host's own mesh address, port, and MTU; the
	private key from its 0600 file; and the last-pushed peer config if present.

	Order is load-bearing:
	  1. create the device (if missing) + pin MTU;
	  2. assign the host's own infra mesh /128;
	  3. load the pushed peer config (addconf — merges, tolerates absence);
	  4. set the derived private key + listen port (LAST, so a config load can never
	     clear it);
	  5. bring the link up + add the fdaa::/16 route.

	Re-running (a `systemctl restart` after a fresh config push, a second boot) is a
	no-op — every step is create-or-replace, mirroring vm-network-up.py / ThinPool."""
	env = read_network_env_optional(MESH_ENV_PATH)
	mesh_address = env.get("MESH_ADDRESS")
	port = int(env.get("WG_HOST_PORT") or DEFAULT_WG_HOST_PORT)
	mtu = int(env.get("WIREGUARD_MTU") or DEFAULT_WIREGUARD_MTU)

	if not run_ok("sudo ip link show {}", MESH_DEVICE):
		run("sudo " + link_add_command())
	run("sudo " + link_mtu_command(mtu))

	if mesh_address:
		run("sudo " + addr_add_command(mesh_address))

	# Load the controller-pushed peers if the file exists (a fresh host may not have
	# been reconciled yet — come up peer-empty and wait). addconf merges, so it never
	# clears the key we set next; check=False tolerates a malformed/partial file
	# rather than failing the whole boot unit.
	if os.path.exists(MESH_CONFIG_PATH):
		run("sudo " + addconf_command(), check=False)

	# Set the derived key LAST (a config load above must not clobber it).
	if os.path.exists(MESH_KEY_PATH):
		run("sudo " + set_key_command(port))

	run("sudo " + link_up_command())
	run("sudo " + route_add_command())


def tear_down_mesh() -> None:
	"""Delete the device (takes its address + peers + connected route with it). Best-
	effort so a `systemctl stop`/`restart` starts clean, symmetric with the tunnel /
	VM teardown paths."""
	run("sudo " + link_del_command(), check=False)
