"""Live, on-the-wire isolation test for the VPN broker (spec/19-vpn-broker.md).

Unlike `test_wireguard.py` (pure argv/string units, no host), this stands up a
REAL WireGuard tunnel over real packets and proves the three isolation facts a
tunnel client must not be able to violate:

  1. it CAN reach its own VM,
  2. it CANNOT reach a second VM (the `forward` chain's head-inserted drop), and
  3. it CANNOT reach the host itself (the `input` chain drop — the fix this test
     exists to validate; without it a client reaches host-local services).

It reproduces the production host model with four network namespaces, all on one
machine — no droplet, no Frappe, no cloud:

      atlas-e2e-client                 atlas-e2e-host (the "server")
    ┌────────────────┐   UDP/IPv4    ┌──────────────────────────────────────┐
    │ wg-e2ec        │──10.77.0.0/24─│ e2e-th  10.77.0.1   (outer transport) │
    │  overlay ::1   │               │ wg-e2e0 fd00:a71a:5000::/127 (tunnel) │
    │  allowed ::/0  │               │   inet atlas: forward + input chains   │
    │  (HOSTILE: routes              │ e2e-vh1 ── fd00:a71a:f00d::1  [vm1 ns] │
    │   everything in)│               │ e2e-vh2 ── fd00:a71a:f00d::2  [vm2 ns] │
    └────────────────┘               │ a service bound to :: (stands in for   │
                                     │   sshd / the Frappe stack)             │
                                     └──────────────────────────────────────┘

The client's `AllowedIPs` is deliberately `::/0` — a hostile/over-broad client
that tries to push packets to vm2 and the host through the tunnel. The guarantee
is that the host drops them regardless of what the client attempts, so this is
the configuration that actually exercises the host-side rules.

The host tunnel is brought up with the SAME `atlas.wireguard` argv builders that
`apply_tunnel` ships (`link_add_argv`, `wg_set_*_argv`, `drop_rule_argv`,
`accept_rule_argv`, `host_drop_rule_argv`), in the same order — so this drives the
real rule construction, not a paraphrase. The per-VM forward accepts that
`vm-network-up.py` lays down are recreated too, so the test also proves the
tunnel `drop` must be HEAD-inserted to win over them.

Run it (needs root, the wireguard module, and `wg`/`nft`/`ip`):

    cd scripts/lib && sudo python3 -m unittest atlas.test_wireguard_e2e -v

Off-root or without the tools the whole class self-skips, so a plain
`python3 -m unittest` (CI, the dev box) is unaffected. Everything it creates is
namespaced under the `atlas-e2e-*` prefix and torn down in tearDownClass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# Make `import atlas...` work whether invoked as `-m atlas.test_wireguard_e2e`
# from scripts/lib or as a direct path from anywhere (the sys.path dance the other
# scripts use). scripts/lib is two parents up from this file (lib/atlas/<this>).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Imported after the sys.path insert above so the package resolves either way.
from atlas.wireguard import (
	_handles_for,
	accept_rule_argv,
	addr_add_argv,
	drop_rule_argv,
	host_drop_rule_argv,
	link_add_argv,
	link_up_argv,
	wg_set_interface_argv,
	wg_set_peer_argv,
)

# --- topology constants (all namespaced under atlas-e2e-*) -------------------
HOST_NS = "atlas-e2e-host"
VM1_NS = "atlas-e2e-vm1"
VM2_NS = "atlas-e2e-vm2"
CLIENT_NS = "atlas-e2e-client"
ALL_NS = (HOST_NS, VM1_NS, VM2_NS, CLIENT_NS)

WG = "wg-e2e0"  # host tunnel interface (the `wg-<id>` of production)
WG_CLIENT = "wg-e2ec"  # client tunnel interface
PORT = 51820

# Outer transport: a v4 link client<->host (the "server public IPv4" stand-in).
TH, TC = "e2e-th", "e2e-tc"
TH_IP, TC_IP = "10.77.0.1", "10.77.0.2"
ENDPOINT = f"{TH_IP}:{PORT}"

# Overlay /127 — slot 0 of ATLAS_TUNNEL_SUPERNET (atlas/networking.py).
HOST_OVERLAY_CIDR = "fd00:a71a:5000::/127"
HOST_OVERLAY = "fd00:a71a:5000::"  # the host end the client shares the /127 with
CLIENT_OVERLAY = "fd00:a71a:5000::1"  # the client's overlay /128

# The two VMs' public /128s, each routed into its own netns like vm-network-up.py.
VM1_V6 = "fd00:a71a:f00d::1"
VM2_V6 = "fd00:a71a:f00d::2"
VM_RANGE = "fd00:a71a:f00d::/64"  # the client routes this into the tunnel to try both
OVERLAY_RANGE = "fd00:a71a:5000::/64"  # …and this, to try the host end

VH1, VN1 = "e2e-vh1", "e2e-vn1"
VH2, VN2 = "e2e-vh2", "e2e-vn2"
LL_HOST, LL_VM = "fe80::1", "fe80::2"  # per-VM veth link-locals (VM gateway = fe80::1)

SERVICE_PORT = 8080  # a host TCP service bound to :: — the "reach a host service" probe


def _can_run() -> tuple[bool, str]:
	"""Gate the whole class: root, the tooling, and a loadable wireguard module."""
	if os.geteuid() != 0:
		return False, "needs root (network namespaces + wg + nft)"
	for tool in ("ip", "wg", "nft", "ping"):
		if shutil.which(tool) is None:
			return False, f"missing tool: {tool}"
	# Load the module if it isn't already; a kernel without it can't make wg links.
	subprocess.run(["modprobe", "wireguard"], capture_output=True)
	probe = subprocess.run(
		["ip", "link", "add", "e2e-probe", "type", "wireguard"], capture_output=True, text=True
	)
	if probe.returncode != 0:
		return False, f"cannot create a wireguard interface: {probe.stderr.strip()}"
	subprocess.run(["ip", "link", "del", "e2e-probe"], capture_output=True)
	return True, ""


_OK, _SKIP_REASON = _can_run()


@unittest.skipUnless(_OK, _SKIP_REASON)
class TunnelIsolationE2E(unittest.TestCase):
	_keydir = ""
	_service: "subprocess.Popen | None" = None

	# --- small command helpers ------------------------------------------------
	@staticmethod
	def _sh(argv, check=True):
		"""Run a command (no shell). Raise with captured output on failure when
		`check`, so a broken setup step fails loud with the kernel's own message."""
		result = subprocess.run(argv, capture_output=True, text=True)
		if check and result.returncode != 0:
			raise RuntimeError(
				f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stdout}{result.stderr}"
			)
		return result

	@classmethod
	def _ns(cls, ns, *argv, check=True):
		return cls._sh(["ip", "netns", "exec", ns, *argv], check=check)

	@classmethod
	def _host(cls, *argv, check=True):
		return cls._ns(HOST_NS, *argv, check=check)

	def _ping(self, addr):
		"""Ping `addr` from the client over the tunnel; return the exit code (0 =
		reachable). Two packets, 1s each — short, since a drop just times out."""
		return subprocess.run(
			["ip", "netns", "exec", CLIENT_NS, "ping", "-6", "-c", "2", "-W", "1", addr],
			capture_output=True,
			text=True,
		).returncode

	def _tcp_connect(self, addr, port):
		"""Try a TCP connect from the client over the tunnel; return True on connect."""
		code = (
			"import socket,sys\n"
			"s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM); s.settimeout(2)\n"
			f"sys.exit(0 if (lambda: (s.connect(('{addr}',{port})) or True))() else 1)\n"
		)
		result = subprocess.run(
			["ip", "netns", "exec", CLIENT_NS, "python3", "-c", code], capture_output=True, text=True
		)
		return result.returncode == 0

	# --- lifecycle ------------------------------------------------------------
	@classmethod
	def setUpClass(cls):
		try:
			cls._build()
		except Exception:
			cls._teardown()  # unittest does NOT call tearDownClass if setUpClass raises
			raise

	@classmethod
	def tearDownClass(cls):
		cls._teardown()

	@classmethod
	def _build(cls):
		cls._keydir = tempfile.mkdtemp(prefix="atlas-e2e-keys-")
		host_key_path, host_pub = cls._genkey("host")
		client_key_path, client_pub = cls._genkey("client")

		# 1. Namespaces. lo up in each so local sockets behave.
		for ns in ALL_NS:
			cls._sh(["ip", "netns", "add", ns])
			cls._ns(ns, "ip", "link", "set", "lo", "up")

		# 2. veth pairs, created in the root ns then moved into place.
		for a, b, ns_a, ns_b in (
			(TH, TC, HOST_NS, CLIENT_NS),
			(VH1, VN1, HOST_NS, VM1_NS),
			(VH2, VN2, HOST_NS, VM2_NS),
		):
			cls._sh(["ip", "link", "add", a, "type", "veth", "peer", "name", b])
			cls._sh(["ip", "link", "set", a, "netns", ns_a])
			cls._sh(["ip", "link", "set", b, "netns", ns_b])

		# 3. Outer transport (client <-> host over IPv4).
		cls._host("ip", "addr", "add", f"{TH_IP}/24", "dev", TH)
		cls._host("ip", "link", "set", TH, "up")
		cls._ns(CLIENT_NS, "ip", "addr", "add", f"{TC_IP}/24", "dev", TC)
		cls._ns(CLIENT_NS, "ip", "link", "set", TC, "up")

		# 4. The host forwards between the tunnel and the VM veths.
		cls._host("sysctl", "-q", "-w", "net.ipv6.conf.all.forwarding=1")

		# 5. Each VM in its own netns, with the host routing its /128 in over the
		#    veth — the v6 half of vm-network-up.py (fe80::1 is the VM's gateway).
		for vm_ns, vh, vn, vm_v6 in ((VM1_NS, VH1, VN1, VM1_V6), (VM2_NS, VH2, VN2, VM2_V6)):
			cls._host("ip", "link", "set", vh, "up")
			cls._host("ip", "-6", "addr", "add", f"{LL_HOST}/64", "dev", vh, "nodad")
			cls._host("ip", "-6", "route", "replace", f"{vm_v6}/128", "via", LL_VM, "dev", vh)
			cls._ns(vm_ns, "ip", "link", "set", vn, "up")
			cls._ns(vm_ns, "ip", "-6", "addr", "add", f"{LL_VM}/64", "dev", vn, "nodad")
			cls._ns(vm_ns, "ip", "-6", "addr", "add", f"{vm_v6}/128", "dev", vn)
			cls._ns(vm_ns, "ip", "-6", "route", "replace", "default", "via", LL_HOST, "dev", vn)

		# 6. The inet atlas scaffold + the broad per-VM forward accepts that
		#    vm-network-up.py lays down (the rules the tunnel drop must beat).
		cls._host("nft", "add table inet atlas")
		cls._host(
			"nft", "add chain inet atlas forward { type filter hook forward priority filter; policy accept; }"
		)
		for vh, vm_v6 in ((VH1, VM1_V6), (VH2, VM2_V6)):
			cls._host("nft", f"add rule inet atlas forward ip6 daddr {vm_v6} oifname {vh} accept")
			cls._host("nft", f"add rule inet atlas forward ip6 saddr {vm_v6} iifname {vh} accept")

		# 7. Bring the host tunnel up with the SHIPPING argv builders, in apply_tunnel's
		#    order: interface + key/port + the one peer + overlay address + up, then the
		#    isolation rules — head-insert drop, head-insert accept (so the chain ends
		#    [accept, drop, …per-VM…]), then the input chain + its host-drop.
		cls._host(*link_add_argv(WG))
		cls._host(*wg_set_interface_argv(WG, PORT, host_key_path))
		cls._host(*wg_set_peer_argv(WG, client_pub, CLIENT_OVERLAY))
		cls._host(*addr_add_argv(WG, HOST_OVERLAY_CIDR))
		cls._host(*link_up_argv(WG))
		cls._host("nft", *drop_rule_argv(WG))
		cls._host("nft", *accept_rule_argv(WG, VM1_V6))
		cls._host(
			"nft", "add chain inet atlas input { type filter hook input priority filter; policy accept; }"
		)
		cls._host("nft", *host_drop_rule_argv(WG))

		# 8. The hostile client: AllowedIPs ::/0, routing vm1, vm2 AND the host overlay
		#    into the tunnel so every probe is actually attempted.
		cls._ns(CLIENT_NS, "ip", "link", "add", WG_CLIENT, "type", "wireguard")
		cls._ns(CLIENT_NS, "wg", "set", WG_CLIENT, "private-key", client_key_path)
		cls._ns(CLIENT_NS, "wg", "set", WG_CLIENT, "peer", host_pub,
			"endpoint", ENDPOINT, "allowed-ips", "::/0", "persistent-keepalive", "5")  # fmt: skip
		cls._ns(CLIENT_NS, "ip", "-6", "addr", "add", f"{CLIENT_OVERLAY}/128", "dev", WG_CLIENT)
		cls._ns(CLIENT_NS, "ip", "link", "set", WG_CLIENT, "up")
		cls._ns(CLIENT_NS, "ip", "-6", "route", "add", VM_RANGE, "dev", WG_CLIENT)
		cls._ns(CLIENT_NS, "ip", "-6", "route", "add", OVERLAY_RANGE, "dev", WG_CLIENT)

		# 9. A host service bound to :: — the thing the input drop must hide.
		server = (
			"import socket\n"
			"s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)\n"
			"s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
			f"s.bind(('::',{SERVICE_PORT})); s.listen(8)\n"
			"while True:\n c,_=s.accept(); c.close()\n"
		)
		cls._service = subprocess.Popen(["ip", "netns", "exec", HOST_NS, "python3", "-c", server])

		# 10. Warm the handshake: ping our own VM until it answers (or give up and let
		#     test_1 report the failure with diagnostics).
		for _ in range(20):
			if cls._reach(VM1_V6) == 0:
				break
			time.sleep(0.5)
		cls._dump()

	@classmethod
	def _genkey(cls, name):
		priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True).stdout.strip()
		path = os.path.join(cls._keydir, f"{name}.key")
		with open(path, "w") as handle:
			handle.write(priv + "\n")
		os.chmod(path, 0o600)
		pub = subprocess.run(
			["wg", "pubkey"], input=priv + "\n", capture_output=True, text=True, check=True
		).stdout.strip()
		return path, pub

	@classmethod
	def _reach(cls, addr):  # classmethod copy of _ping for use during warmup
		return subprocess.run(
			["ip", "netns", "exec", CLIENT_NS, "ping", "-6", "-c", "1", "-W", "1", addr], capture_output=True
		).returncode

	@classmethod
	def _dump(cls):
		"""Print the live host ruleset + wg state, so a pasted run shows what was
		actually applied (handy when feeding output back)."""
		print("\n----- host inet atlas ruleset (live) -----", file=sys.stderr)
		print(cls._host("nft", "list", "table", "inet", "atlas", check=False).stdout, file=sys.stderr)
		print("----- wg show (host) -----", file=sys.stderr)
		print(cls._host("wg", "show", check=False).stdout, file=sys.stderr)

	@classmethod
	def _teardown(cls):
		if cls._service is not None:
			cls._service.terminate()
			try:
				cls._service.wait(timeout=3)
			except subprocess.TimeoutExpired:
				cls._service.kill()
			cls._service = None
		for ns in ALL_NS:
			subprocess.run(["ip", "netns", "del", ns], capture_output=True)
		# Defensive: reap any veth ends left in the root ns by a half-built setup.
		for link in (TH, TC, VH1, VN1, VH2, VN2):
			subprocess.run(["ip", "link", "del", link], capture_output=True)
		if cls._keydir and os.path.isdir(cls._keydir):
			shutil.rmtree(cls._keydir, ignore_errors=True)
			cls._keydir = ""

	# --- the isolation facts --------------------------------------------------
	def test_1_reaches_own_vm(self):
		"""The tunnel works: the client reaches the VM it was issued for."""
		self.assertEqual(0, self._ping(VM1_V6), "client should reach its own VM over the tunnel")

	def test_2_cannot_reach_other_vm(self):
		"""The forward drop (head-inserted above vm2's own accept) blocks a second VM."""
		self.assertNotEqual(0, self._ping(VM2_V6), "tunnel must NOT reach a second VM")

	def test_3_cannot_reach_host_via_overlay_ping(self):
		"""The input drop blocks a packet aimed at the host's own overlay address."""
		self.assertNotEqual(0, self._ping(HOST_OVERLAY), "tunnel must NOT reach the host itself")

	def test_4_cannot_reach_host_service(self):
		"""A real host service bound to :: (sshd / the Frappe stack stand-in) is
		unreachable over the tunnel."""
		self.assertFalse(
			self._tcp_connect(HOST_OVERLAY, SERVICE_PORT), "tunnel must NOT reach a host service bound to ::"
		)

	def test_5_input_drop_is_what_blocks_the_host(self):
		"""Causation: pull the input drop and the host becomes reachable; restore it
		(with the shipping builder) and it's blocked again. Proves the fix is load-bearing."""
		self.assertNotEqual(
			0, self._ping(HOST_OVERLAY), "host should be blocked with the input drop in place"
		)
		listing = self._host("nft", "-a", "list", "chain", "inet", "atlas", "input").stdout
		handles = list(_handles_for(listing, WG))
		self.assertTrue(handles, "expected an input-chain drop for the tunnel interface")
		for handle in handles:
			self._host("nft", "delete", "rule", "inet", "atlas", "input", "handle", handle)
		try:
			self.assertEqual(
				0, self._ping(HOST_OVERLAY), "WITHOUT the input drop the host IS reachable over the tunnel"
			)
		finally:
			self._host("nft", *host_drop_rule_argv(WG))  # restore the fix
		self.assertNotEqual(0, self._ping(HOST_OVERLAY), "re-applying the input drop blocks the host again")

	def test_6_forward_drop_is_what_blocks_other_vm(self):
		"""The transit half of the same story: pull the forward drop and vm2 leaks
		through its own per-VM accept; restore it and vm2 is blocked again."""
		self.assertNotEqual(0, self._ping(VM2_V6), "vm2 should be blocked with the forward drop in place")
		listing = self._host("nft", "-a", "list", "chain", "inet", "atlas", "forward").stdout
		drop_handles = [line.split()[-1] for line in listing.splitlines() if WG in line and "drop" in line]
		self.assertTrue(drop_handles, "expected a forward-chain drop for the tunnel interface")
		for handle in drop_handles:
			self._host("nft", "delete", "rule", "inet", "atlas", "forward", "handle", handle)
		try:
			self.assertEqual(
				0, self._ping(VM2_V6), "WITHOUT the forward drop vm2 leaks via its own per-VM accept"
			)
		finally:
			self._host("nft", *drop_rule_argv(WG))  # restore the head-inserted drop
		self.assertNotEqual(0, self._ping(VM2_V6), "re-applying the forward drop blocks vm2 again")


if __name__ == "__main__":
	unittest.main()
