"""Unit tests for `networkd.render` — the canonical WgDesired renderer (spec
§16.2) + the §16.3 non-overlap invariant.

Run from `scripts/lib`: `python3 -m unittest atlas.test_networkd_render`. The
canonical bytes are asserted against the shape the existing
`atlas/atlas/host_mesh.py:render_wg_mesh_config` emits, so an ANCP-rendered
config byte-compares against one the controller last pushed on a host migrating
from the predecessor path.
"""

import unittest

from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	OwnershipTable,
	owning_advertisement,
)
from atlas.networkd.render import _assert_no_input_overlap, render_wg_desired


def _ip_counts(out: str) -> dict[str, int]:
	"""Count how many peers each /128 appears under across the rendered
	AllowedIPs lines (a /128 with count > 1 is a §16.3 overlap)."""
	counts: dict[str, int] = {}
	for line in out.splitlines():
		if line.startswith("AllowedIPs ="):
			body = line.split("= ", 1)[1].strip()
			if not body:
				continue
			for ip in body.split(", "):
				counts[ip] = counts.get(ip, 0) + 1
	return counts


def member(host_id: str, key: str, mesh: str, endpoint: str = "2001:db9::7") -> MembershipRecord:
	return MembershipRecord(
		host_id=host_id,
		kind=MembershipKind.MEMBER,
		state=MemberState.ALIVE,
		endpoint=endpoint,
		wg_public_key=key,
		mesh_address=mesh,
		generation=1,
	)


class TestRenderShape(unittest.TestCase):
	def test_interface_header_and_trailing_newline(self):
		out = render_wg_desired("h1", {}, OwnershipTable())
		self.assertEqual(out, "[Interface]\nListenPort = 51820\n\n")

	def test_self_host_excluded(self):
		m = {
			"h1": member("h1", "AAA", "fdaa:0:0:1::1"),
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),
		}
		out = render_wg_desired("h1", m, OwnershipTable())
		self.assertIn("PublicKey = BBB", out)
		self.assertNotIn("PublicKey = AAA", out)

	def test_peers_sorted_by_pubkey(self):
		# Byte-canonical: peers appear in pubkey-sorted order, not insertion.
		# self_host_id ("h0") is NOT in the members map so none are skipped.
		m = {
			"h1": member("h1", "CCC", "fdaa:0:0:1::1"),
			"h2": member("h2", "AAA", "fdaa:0:0:2::1"),
			"h3": member("h3", "BBB", "fdaa:0:0:3::1"),
		}
		out = render_wg_desired("h0", m, OwnershipTable())
		positions = [out.index(f"PublicKey = {k}") for k in ("AAA", "BBB", "CCC")]
		self.assertEqual(positions, sorted(positions))

	def test_allowedips_includes_peer_mesh_address(self):
		# spec §16.2: each peer's AllowedIPs includes that peer's own infra /128
		# so the host↔host bus can dial it, even with zero owned /128s.
		m = {
			"h1": member("h1", "AAA", "fdaa:0:0:1::1"),
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),
		}
		out = render_wg_desired("h1", m, OwnershipTable())
		self.assertIn("AllowedIPs = fdaa:0:0:2::1/128", out)

	def test_endpoint_wraps_with_brackets_and_port(self):
		m = {"h2": member("h2", "BBB", "fdaa:0:0:2::1", endpoint="2001:db9::aa")}
		out = render_wg_desired("h1", m, OwnershipTable())
		self.assertIn("Endpoint = [2001:db9::aa]:51820", out)

	def test_listen_port_default_51820(self):
		out = render_wg_desired("h1", {}, OwnershipTable())
		self.assertIn("ListenPort = 51820", out)


class TestEmptyKeySkip(unittest.TestCase):
	def test_peer_with_empty_key_omitted(self):
		m = {
			"h1": member("h1", "", "fdaa:0:0:1::1"),  # empty key — seed before handshake
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),  # known key
		}
		out = render_wg_desired("h0", m, OwnershipTable())
		pubkey_lines = [l for l in out.splitlines() if l.startswith("PublicKey = ")]
		self.assertEqual(pubkey_lines, ["PublicKey = BBB"])

	def test_all_empty_keys_produces_interface_only(self):
		m = {
			"h1": member("h1", "", "fdaa:0:0:1::1"),
			"h2": member("h2", "", "fdaa:0:0:2::1"),
		}
		out = render_wg_desired("h0", m, OwnershipTable())
		self.assertEqual(out, "[Interface]\nListenPort = 51820\n\n")


class TestOwnedAllowedIPs(unittest.TestCase):
	def test_owned_ips_routed_to_owner(self):
		m = {
			"h1": member("h1", "AAA", "fdaa:0:0:1::1"),
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),
			"h3": member("h3", "CCC", "fdaa:0:0:3::1"),
		}
		owner_of = {"fdaa:1::1": "h2", "fdaa:1::2": "h2", "fdaa:1::3": "h3"}
		out = render_wg_desired("h1", m, OwnershipTable(owner_of=owner_of))
		# h2's stanza carries both /128s it owns + its own mesh /128.
		h2_block = out.split("PublicKey = BBB")[1].split("[Peer]")[0]
		self.assertIn("fdaa:1::1/128", h2_block)
		self.assertIn("fdaa:1::2/128", h2_block)
		self.assertIn("fdaa:0:0:2::1/128", h2_block)
		# h3 owns one /128.
		h3_block = out.split("PublicKey = CCC")[1].split("[Peer]")[0]
		self.assertIn("fdaa:1::3/128", h3_block)

	def test_conflict_ip_dropped_from_all(self):
		# §16.3 / §7.3: a conflicting /128 appears in NO peer's AllowedIPs.
		m = {
			"h1": member("h1", "AAA", "fdaa:0:0:1::1"),
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),
		}
		owner_of = {"fdaa:1::3": "h2"}  # only the non-conflicting /128 routed
		ownership = OwnershipTable(owner_of=owner_of, conflicts=frozenset({"fdaa:9::9"}))
		out = render_wg_desired("h1", m, ownership)
		self.assertNotIn("fdaa:9::9", out)

	def test_owner_not_in_members_drops_silently(self):
		# An owner that was GC'd upstream but still in `owner_of` does not crash
		# the render and does not emit a phantom peer; the /128 simply isn't
		# advertised this round. Anti-entropy / GC reconciles the next round.
		m = {"h1": member("h1", "AAA", "fdaa:0:0:1::1")}
		owner_of = {"fdaa:1::1": "h-zombie"}
		out = render_wg_desired("h1", m, OwnershipTable(owner_of=owner_of))
		self.assertNotIn("fdaa:1::1", out)


class TestNonOverlapInvariant(unittest.TestCase):
	def test_distinct_owners_never_overlap(self):
		# Even with many peers + many /128s, each /128 lands in at most one
		# peer's AllowedIPs. The render self-asserts this (`_assert_no_input_overlap`).
		m = {f"h{i}": member(f"h{i}", f"K{i}", f"fdaa:0:0:{i}::1") for i in range(5)}
		owner_of = {f"fdaa:1::{i}": f"h{i}" for i in range(5)}
		out = render_wg_desired("h0", m, OwnershipTable(owner_of=owner_of))
		# Count occurrences of each /128 across the rendered AllowedIPs lines.
		ip_counts = {}
		for line in out.splitlines():
			if line.startswith("AllowedIPs ="):
				for ip in line.split("= ", 1)[1].split(", "):
					ip_counts[ip] = ip_counts.get(ip, 0) + 1
		dup = {ip: c for ip, c in ip_counts.items() if c > 1}
		self.assertEqual(dup, {}, f"rendered overlapping AllowedIPs: {dup}")


class TestMeshAddressOverlap(unittest.TestCase):
	"""H2 — a peer's mesh_address/128 is folded into the SAME cross-peer overlap
	accounting as owned /128s (§16.3). Before the fix mesh_address was appended
	AFTER the overlap check, so a mesh_address == a victim /128 produced the same
	/128 in two peers' AllowedIPs → WireGuard cryptokey-routing misdelivery."""

	def test_two_peers_sharing_a_mesh_address_drop_it(self):
		# Two honest hosts collide on a mesh_address (a birthday collision, or a
		# compromised host copying a victim's). The shared /128 must appear in
		# ZERO peers (dropped as a conflict), and the final config passes the
		# non-overlap assertion.
		collide = "fdaa:0:0:9::1"
		m = {
			"h1": member("h1", "AAA", collide),
			"h2": member("h2", "BBB", collide),
		}
		out = render_wg_desired("h0", m, OwnershipTable())
		self.assertNotIn(f"{collide}/128", out)
		counts = _ip_counts(out)
		self.assertEqual({ip: c for ip, c in counts.items() if c > 1}, {})

	def test_mesh_address_equal_to_a_tenant_owned_128_is_dropped(self):
		# A malicious host signs a Membership Record whose mesh_address equals a
		# victim tenant's owned /128. Folding mesh_address into the overlap pass
		# means the /128 lands in >1 peer → dropped from ALL peers, no
		# misdelivery.
		victim = "fdaa:1::7"
		m = {
			"h1": member("h1", "AAA", "fdaa:0:0:1::1"),  # legit owner of `victim`
			"h2": member("h2", "BBB", victim),  # attacker: mesh_address == victim /128
		}
		owner_of = {victim: "h1"}
		out = render_wg_desired("h0", m, OwnershipTable(owner_of=owner_of))
		self.assertNotIn(f"{victim}/128", out)
		counts = _ip_counts(out)
		self.assertEqual({ip: c for ip, c in counts.items() if c > 1}, {})

	def test_non_colliding_mesh_addresses_still_route(self):
		# The fix must not over-drop: distinct mesh_addresses each still render.
		m = {
			"h1": member("h1", "AAA", "fdaa:0:0:1::1"),
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),
		}
		out = render_wg_desired("h0", m, OwnershipTable())
		self.assertIn("fdaa:0:0:1::1/128", out)
		self.assertIn("fdaa:0:0:2::1/128", out)

	def test_peer_whose_only_128_collides_renders_empty_allowedips(self):
		# A peer whose sole /128 (its mesh_address) collided renders an empty
		# AllowedIPs rather than a misdelivering one — the [Peer] survives (still
		# reachable for keepalive), it just advertises no routes this round.
		collide = "fdaa:0:0:9::1"
		m = {
			"h1": member("h1", "AAA", collide),
			"h2": member("h2", "BBB", collide),
		}
		out = render_wg_desired("h0", m, OwnershipTable())
		h1_block = out.split("PublicKey = AAA")[1].split("[Peer]")[0]
		self.assertIn("AllowedIPs = \n", h1_block + "\n")

	def test_assert_no_input_overlap_catches_a_duplicate(self):
		# Direct unit of the invariant hook: two peers carrying the same /128
		# trips the assertion (defends a future folding bug).
		with self.assertRaises(AssertionError):
			_assert_no_input_overlap(
				{"h1": ["fdaa::7/128"], "h2": ["fdaa::7/128"]}, OwnershipTable()
			)


if __name__ == "__main__":
	unittest.main()
