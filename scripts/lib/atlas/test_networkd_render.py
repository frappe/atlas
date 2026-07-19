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
from atlas.networkd.render import render_wg_desired


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
			"h1": member("h1", "", "fdaa:0:0:1::1"),       # empty key — seed before handshake
			"h2": member("h2", "BBB", "fdaa:0:0:2::1"),   # known key
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


if __name__ == "__main__":
	unittest.main()
