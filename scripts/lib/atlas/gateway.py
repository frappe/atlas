"""Customer gateway bring-up — the gateway VM's single wg0 + the static same_48 guard.

Runs INSIDE the gateway guest (spec/25 Phase 5, spec/26, reference §9). The customer
gateway is an operator-owned infra guest, the proxy VM's sibling: it terminates every
region customer as a `[Peer]` on ONE `wg0`, pins each source with cryptokey routing,
and confines each destination to the source's own /48 with a static eBPF guard.

This module owns the DEVICE + the guard; the controller (`atlas/atlas/customer_gateway.py`)
owns the PEER set (it `wg syncconf`s peers over guest-SSH). Bring-up is idempotent and
boot-safe (a `gateway.service` oneshot re-runs it), the vm-network-up / host-mesh pattern:

  1. create wg0 (if missing) + pin MTU 1420;
  2. mint the gateway's OWN wg0 keypair ONCE (0600, kept on the gateway) + listen on 51820;
  3. bring the link up;
  4. attach the STATIC same_48 eBPF guard on wg0 tc ingress (one program, ALL customers,
     never touched per customer) — the destination-confinement half of isolation;
  5. add the one nft `iifname wg0 drop` input rule (host-local protection — the tc guard
     sees forwarded packets; host-local delivery to sshd/Frappe on `::` needs this).

NONE of 4-5 change per customer — enrolling a customer is JUST a wg peer (the controller's
`wg syncconf`), no eBPF change, no new nft rule. The compiled `vpc_guard.bpf.o` is staged
next to this script (built at bake time; clang/libbpf are BUILD-time only — the runtime
gateway needs neither, only the .o).

Everything here is pure string/argv construction except `bring_up_gateway`, which touches
the host — so the command generation is unit-testable with bare `python3 -m unittest` (no
host), like host_mesh / private_network.
"""

from __future__ import annotations

import os

from atlas._run import run, run_ok
from atlas.network_env import read_network_env_optional

GATEWAY_DEVICE = "wg0"
GATEWAY_KEY_PATH = "/etc/wireguard/wg0.key"
GATEWAY_CONFIG_PATH = "/etc/wireguard/wg0.conf"
GATEWAY_ENV_PATH = "/etc/atlas-gateway.env"

# The compiled eBPF object, staged alongside this lib on the gateway (built at bake time).
# Resolved relative to this file so it works wherever the atlas package is placed.
BPF_OBJECT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vpc_guard.bpf.o")

DEFAULT_WG_GATEWAY_PORT = 51820
DEFAULT_WIREGUARD_MTU = 1420


def link_add_command(device: str = GATEWAY_DEVICE) -> str:
	return f"ip link add dev {device} type wireguard"


def link_mtu_command(mtu: int, device: str = GATEWAY_DEVICE) -> str:
	return f"ip link set dev {device} mtu {mtu}"


def link_up_command(device: str = GATEWAY_DEVICE) -> str:
	return f"ip link set dev {device} up"


def link_del_command(device: str = GATEWAY_DEVICE) -> str:
	return f"ip link del dev {device}"


def genkey_command(key_path: str = GATEWAY_KEY_PATH) -> str:
	"""Mint the gateway's OWN wg0 private key into a 0600 file, ONCE. Idempotent via the
	`test -f` guard in the caller — a re-run reuses the existing key so the customers'
	configs (which carry the derived public key) never go stale."""
	return f"umask 077 && wg genkey > {key_path}"


def set_key_command(port: int, key_path: str = GATEWAY_KEY_PATH, device: str = GATEWAY_DEVICE) -> str:
	"""Set the minted private key (from the 0600 file, never inline) + listen port."""
	return f"wg set {device} private-key {key_path} listen-port {port}"


def clsact_command(device: str = GATEWAY_DEVICE) -> str:
	"""Add the clsact qdisc wg0 needs before a tc filter can attach. Idempotent via the
	caller's guard (`tc qdisc show` grep)."""
	return f"tc qdisc add dev {device} clsact"


def attach_guard_command(obj_path: str = BPF_OBJECT_PATH, device: str = GATEWAY_DEVICE) -> str:
	"""Attach the static same_48 eBPF guard on wg0 tc INGRESS (post-decrypt). `da` =
	direct-action; `sec tc` names the load section (gotcha #2). wg0 is L3 so the program
	reads the IPv6 header directly (gotcha #1, baked into the .c)."""
	return f"tc filter add dev {device} ingress bpf da obj {obj_path} sec tc"


# The gateway guest's OWN nft table for the host-local input drop. Distinct from the
# HOST's `inet atlas` table (which lives on the server, not in this guest) — inside the
# gateway guest we own a self-contained `inet gateway` table with a single input chain.
GUEST_TABLE = "inet gateway"


def create_table_commands() -> list[str]:
	"""Create the gateway guest's own `inet gateway` table + input chain (policy accept,
	so ordinary guest ingress — sshd on the public path, the wg0 outer UDP listener on the
	uplink — is untouched; only the wg0-decrypted-into-host-local drop is added). Split
	from the drop so the caller can guard table creation idempotently."""
	return [
		f"nft add table {GUEST_TABLE}",
		f"nft add chain {GUEST_TABLE} input {{ type filter hook input priority 0 ; policy accept ; }}",
	]


def input_drop_command(device: str = GATEWAY_DEVICE) -> str:
	"""The one nft rule: drop any customer→gateway-host-local traffic (sshd/Frappe bind
	`::`). The tc ingress guard governs FORWARDED packets; host-local delivery to a service
	on `::` needs this separate input-hook drop (reference §6.2). Added once at bring-up,
	never per customer. Nothing legitimately terminates on the gateway over wg0 (a customer
	reaches VMs, not the gateway itself), so a blanket wg0 input drop is safe."""
	return f'nft add rule {GUEST_TABLE} input iifname "{device}" drop'


def route_private_plane_command() -> str:
	"""Route the whole private plane at the host: `fdaa::/16 via fe80::1 dev eth0`. The
	gateway decrypts a customer packet on wg0 and forwards it out eth0 to its host, whose
	wg-mesh cryptokey-routes each fdaa:: /128 to the right VM host — exactly the route a
	tenant guest's atlas-network.service installs. `replace` so a re-run is idempotent."""
	return "ip -6 route replace fdaa::/16 via fe80::1 dev eth0"


def public_key_command(key_path: str = GATEWAY_KEY_PATH) -> str:
	"""Print the gateway's wg0 PUBLIC key (the one key shared by every peer). The
	controller reads this over guest-SSH and denorms it onto the peer rows."""
	return f"wg pubkey < {key_path}"


def bring_up_gateway() -> None:
	"""Idempotently bring the gateway's wg0 up + attach the static guard (reference §9).
	Reads /etc/atlas-gateway.env for the port + MTU. Re-running (a `systemctl restart`, a
	second boot) is a no-op — every step is create-or-replace / guarded, mirroring
	host_mesh.bring_up_mesh / vm-network-up.py.

	The customer PEERS are NOT set here — the controller `wg syncconf`s them over guest-SSH.
	This brings up the interface + the never-per-customer guard so the gateway is ready to
	accept the controller's first peer push."""
	env = read_network_env_optional(GATEWAY_ENV_PATH)
	port = int(env.get("WG_GATEWAY_PORT") or DEFAULT_WG_GATEWAY_PORT)
	mtu = int(env.get("WIREGUARD_MTU") or DEFAULT_WIREGUARD_MTU)

	# 0. The WireGuard kernel module — the gateway is a GUEST, so (unlike a bootstrapped
	#    host) it may not have `wireguard` in its kernel. A purpose-baked gateway image
	#    ships it; a generic image needs `linux-modules-extra-<uname -r>` (which carries
	#    wireguard.ko for the stock Ubuntu kernel). Install it if `modprobe` fails, then
	#    load + persist so wg0 can be created now and survives a reboot (mirrors the
	#    host-side modprobe in bootstrap-server.py). Fail loud if wireguard is STILL absent
	#    after the install — a gateway without WireGuard cannot terminate a customer.
	if not run_ok("sudo modprobe wireguard"):
		run(
			"sudo bash -c {}",
			"apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
			"linux-modules-extra-$(uname -r) wireguard-tools >/dev/null 2>&1 || true",
		)
		run("sudo modprobe wireguard")  # raises if still unavailable — the load-bearing gate
	run("sudo bash -c {}", "echo wireguard > /etc/modules-load.d/60-atlas-wireguard.conf", check=False)

	# 1. Device + MTU (create-or-replace).
	if not run_ok("sudo ip link show {}", GATEWAY_DEVICE):
		run("sudo " + link_add_command())
	run("sudo " + link_mtu_command(mtu))

	# 2. Mint the key ONCE (reuse on re-run so customer configs never go stale), then set
	#    it + the listen port. umask/genkey needs a shell.
	if not os.path.exists(GATEWAY_KEY_PATH):
		run("sudo bash -c {}", genkey_command())
	run("sudo " + set_key_command(port))

	# 3. Link up.
	run("sudo " + link_up_command())

	# 4. Forward the private plane. The gateway decrypts a customer packet on wg0 and must
	#    FORWARD it out eth0 to its host's wg-mesh (and forward the VM's reply back). So
	#    enable IPv6 forwarding and route fdaa::/16 at the host (fe80::1 via eth0) exactly
	#    like a tenant guest's atlas-network.service does — the gateway is an fdaa:: router,
	#    not an endpoint. Persist forwarding so it survives a reboot.
	run("sudo sysctl -w net.ipv6.conf.all.forwarding=1")
	run(
		"sudo bash -c {}",
		"echo 'net.ipv6.conf.all.forwarding=1' > /etc/sysctl.d/60-atlas-gateway.conf",
		check=False,
	)
	run("sudo " + route_private_plane_command())

	# 5. The static same_48 guard on wg0 tc ingress (idempotent: add the qdisc if absent;
	#    clear any prior filters so a re-run doesn't STACK duplicates, then add fresh).
	if not run_ok("sudo bash -c {}", f"tc qdisc show dev {GATEWAY_DEVICE} | grep -q clsact"):
		run("sudo " + clsact_command())
	run("sudo bash -c {}", f"tc filter del dev {GATEWAY_DEVICE} ingress 2>/dev/null || true")
	# If the .o is missing (unbuilt image) fail loud — the guard is load-bearing, a gateway
	# without it must not accept customers.
	run("sudo " + attach_guard_command())

	# 6. The host-local input drop in the guest's own `inet gateway` table. Create the
	#    table + input chain if absent (idempotent guard), then add the drop tolerating a
	#    duplicate on a re-run (nft rejects an identical rule with a non-zero exit).
	if not run_ok("sudo bash -c {}", f"nft list table {GUEST_TABLE} >/dev/null 2>&1"):
		for command in create_table_commands():
			run("sudo " + command)
	run("sudo " + input_drop_command(), check=False)


def tear_down_gateway() -> None:
	"""Delete wg0 (takes its peers + the tc qdisc/filter with it). Best-effort so a
	`systemctl stop`/`restart` starts clean, symmetric with the mesh/VM teardown paths.
	The nft input drop is left (harmless; re-asserted idempotently on the next bring-up)."""
	run("sudo " + link_del_command(), check=False)
