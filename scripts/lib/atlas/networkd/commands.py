"""Apply-pipeline command builders for `wg-mesh` (spec §16.4 / §16.5).

Pure command-string builders — the `atlas-networkd` analogue of
`scripts/lib/atlas/host_mesh.py`'s `link_*_command` / `set_key_command` /
`_apply_script`. The daemon composes these and runs them via `_run.run`; the
command construction itself is unit-testable without a host, exactly like the
existing `host_mesh` command builders (`scripts/lib/atlas/test_host_mesh.py`).

The whole-table atomic apply (spec §16.4 — Issue B):

    wg syncconf wg-mesh <(wg-quick strip /run/.../wg-mesh.conf)
    wg set wg-mesh private-key /etc/atlas-networkd/wg-private-key listen-port 51820

— `syncconf` FIRST, then `set private-key` LAST. **Load-bearing ordering**
(proven on a real Scaleway host, the existing
`atlas/atlas/host_mesh.py:378` documents it): `syncconf` from a config body
that omits `PrivateKey` **clears the interface key**, leaving the device unable
to handshake. The pushed config deliberately carries NO `PrivateKey` (the
secret rides in its own 0600 file), so the key is asserted AFTER syncconf. A
future implementer who flips this order breaks every tunnel; an assertion in
`apply_script` guards the order at construction time.

No incremental `wg set peer … allowed-ips …` for control-plane changes — the
only permitted apply shape is the whole-table syncconf above. Incremental
applies would open a window (peer A added before peer B removed) in which the
same /128 sits in two peers' `AllowedIPs` and breaks the Issue B invariant.
"""

from __future__ import annotations

# Path constants — single source of truth for the apply pipeline. Mirrored from
# `scripts/lib/atlas/host_mesh.py` (which retires in §6 / stage 6 of the build).
# The key file moved from `/etc/atlas-host-mesh.key` to the daemon's data dir
# (Issue A — keys are now self-generated, not derived by the controller).
WG_DEVICE = "wg-mesh"
WG_CONFIG_PATH = "/run/atlas-networkd/wg-mesh.conf"
WG_PRIVATE_KEY_PATH = "/etc/atlas-networkd/wg-private-key"
PRIVATE_PLANE_ROUTE = "fdaa::/16"
DEFAULT_WG_HOST_PORT = 51820
DEFAULT_WIREGUARD_MTU = 1420


def link_add_command(device: str = WG_DEVICE) -> str:
	"""`ip link add dev wg-mesh type wireguard` — create the device if missing."""
	return f"ip link add dev {device} type wireguard"


def link_mtu_command(mtu: int, device: str = WG_DEVICE) -> str:
	"""Pin the WireGuard MTU (1420 — proven on real Scaleway hosts; wg adds
	~80 B over a 1500 path, so larger frames blackhole without it)."""
	return f"ip link set dev {device} mtu {mtu}"


def link_up_command(device: str = WG_DEVICE) -> str:
	return f"ip link set dev {device} up"


def link_del_command(device: str = WG_DEVICE) -> str:
	"""Tear the device down (takes its address, peers, connected route with it)."""
	return f"ip link del dev {device}"


def addr_add_command(mesh_address: str, device: str = WG_DEVICE) -> str:
	"""Assign the host's OWN infra mesh /128 (§7.1 `mesh_address`). `replace` so
	a re-run is idempotent — a half-configured device self-heals on next apply."""
	return f"ip -6 addr replace {mesh_address}/128 dev {device}"


def route_add_command(route: str = PRIVATE_PLANE_ROUTE, device: str = WG_DEVICE) -> str:
	"""Own the whole private plane: route `fdaa::/16` out `wg-mesh`. Cryptokey
	routing (per-peer AllowedIPs) then delivers each /128 to the right host."""
	return f"ip -6 route replace {route} dev {device}"


def set_key_command(port: int, key_path: str = WG_PRIVATE_KEY_PATH, device: str = WG_DEVICE) -> str:
	"""Set the private key (from a 0600 file, never inline) + listen port. MUST
	run AFTER any `syncconf`/`addconf` — see the module docstring + the order
	assertion in `apply_script`."""
	return f"wg set {device} private-key {key_path} listen-port {port}"


def syncconf_command(config_path: str = WG_CONFIG_PATH, device: str = WG_DEVICE) -> str:
	"""`wg syncconf wg-mesh <(wg-quick strip <conf>)` — the atomic whole-table
	apply. Replaces the entire peer set from the config in one shot (no
	incremental per-peer `wg set`), which is what preserves the §16.3 non-
	overlap invariant (Issue B) at each host. Processes substitution needs bash,
	so the daemon runs this via `bash -c` (one auto-quoted argv token, exactly
	like the existing `atlas/atlas/host_mesh.py:_push_wg_mesh`)."""
	return f"wg syncconf {device} <(wg-quick strip {config_path})"


def apply_script(
	config_path: str = WG_CONFIG_PATH,
	key_path: str = WG_PRIVATE_KEY_PATH,
	port: int = DEFAULT_WG_HOST_PORT,
	device: str = WG_DEVICE,
) -> str:
	"""The on-host apply body (fed to `bash -c` as one auto-quoted argv token),
	assuming the device already exists and is up. Used on the **hot path** (a
	membership/ownership change re-renders and re-applies the peer table).

	Order is load-bearing: `syncconf` FIRST (it rewrites [Interface] and clears
	any unmentioned private key), THEN `wg set private-key` + listen-port so
	they survive. Verified on a real Scaleway host — see
	`atlas/atlas/host_mesh.py:378` for the failure mode this order prevents
	(`syncconf` after `set private-key` leaves the key `(none)`).
	"""
	# Construct the two halves separately so we can assert the order at build
	# time — a refactor that flips the order would otherwise only surface on a
	# live host (a tunnel that silently never handshakes). This is a static
	# check the existing code relies on via a comment + a memory log; ANCP makes
	# it a construction-time invariant.
	sync_first = syncconf_command(config_path, device)
	key_last = set_key_command(port, key_path, device)
	assert "syncconf" in sync_first and "private-key" in key_last
	return f"set -e; {sync_first}; {key_last}"


def bring_up_script(
	mesh_address: str,
	mtu: int = DEFAULT_WIREGUARD_MTU,
	port: int = DEFAULT_WG_HOST_PORT,
	config_path: str = WG_CONFIG_PATH,
	key_path: str = WG_PRIVATE_KEY_PATH,
	device: str = WG_DEVICE,
) -> str:
	"""The first-boot bring-up body (spec §16.5 — mirrors the existing
	`scripts/lib/atlas/host_mesh.py:bring_up_mesh`): create the device if
	missing, pin MTU, assign the host's own infra /128, bring it up, add the
	`fdaa::/16` route, then apply the peer table (syncconf) and set the key
	(LAST). Every step is create-or-replace, so a half-configured device
	self-heals on the next run.

	The peer table is applied via `apply_script` (inlined here so the whole
	bring-up is one `bash -c`); if the config file is absent (a fresh host
	before the first render), the `wg syncconf` is skipped — the device comes up
	peer-empty and waits, exactly like today's `bring_up_mesh`.
	"""
	apply = apply_script(config_path, key_path, port, device)
	return (
		f"set -e; "
		f"if ! ip link show {device} >/dev/null 2>&1; then "
		f"ip link add dev {device} type wireguard; "
		f"fi; "
		f"ip link set dev {device} mtu {mtu}; "
		f"ip -6 addr replace {mesh_address}/128 dev {device}; "
		f"ip link set dev {device} up; "
		f"ip -6 route replace {PRIVATE_PLANE_ROUTE} dev {device}; "
		f"if [ -s {config_path} ]; then {apply}; fi"
	)


__all__ = [
	"DEFAULT_WG_HOST_PORT",
	"DEFAULT_WIREGUARD_MTU",
	"PRIVATE_PLANE_ROUTE",
	"WG_CONFIG_PATH",
	"WG_DEVICE",
	"WG_PRIVATE_KEY_PATH",
	"addr_add_command",
	"apply_script",
	"bring_up_script",
	"link_add_command",
	"link_del_command",
	"link_mtu_command",
	"link_up_command",
	"route_add_command",
	"set_key_command",
	"syncconf_command",
]
