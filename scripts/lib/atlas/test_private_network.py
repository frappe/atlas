"""Unit tests for the host-side private-plane isolation rules (design §4/§5).

Run with bare `python3 -m unittest atlas.test_private_network` from scripts/lib: no
Frappe, no site, no host, no nft. These cover the rule TEXT (which the idempotency
guards match against `nft list` output), the nft command builders, and the
apply/remove sweep helpers — all without touching the host. The rule text was
verified byte-for-byte against real `nft list` output on a Scaleway host.
"""

import shlex
import unittest

from atlas import private_network as pn

VETH = "atlas-h1234567"
PRIV = "fdaa:f2b4:b043:0:b2c0:16db:1382:4247"
T48 = "fdaa:f2b4:b043::/48"


class TestRuleText(unittest.TestCase):
	"""The rule TEXT must match nft's canonical `list` output exactly, or the
	idempotency guards in apply_private_network re-insert a duplicate every run."""

	def test_terminal_drop_text(self):
		self.assertEqual(pn.terminal_drop_text(), "ip6 daddr fdaa::/16 drop")

	def test_anti_spoof_text(self):
		# Source MUST be exactly this VM's /128 — the != form nft prints.
		self.assertEqual(
			pn.anti_spoof_text(VETH, PRIV),
			'iifname "atlas-h1234567" ip6 daddr fdaa::/16 ip6 saddr != '
			"fdaa:f2b4:b043:0:b2c0:16db:1382:4247 drop",
		)

	def test_same_tenant_egress_text(self):
		self.assertEqual(
			pn.same_tenant_egress_text(VETH, PRIV, T48),
			'iifname "atlas-h1234567" ip6 saddr fdaa:f2b4:b043:0:b2c0:16db:1382:4247 '
			"ip6 daddr fdaa:f2b4:b043::/48 accept",
		)

	def test_infra_destination_text_is_canonicalized(self):
		# The infra /48 fdaa:0:0::/48 prints as fdaa::/48 in nft output; the guard
		# text MUST use the canonical form or it never matches (proven on a host).
		self.assertIn("ip6 daddr fdaa::/48 accept", pn.infra_destination_text(VETH, PRIV))
		self.assertNotIn("fdaa:0:0::/48", pn.infra_destination_text(VETH, PRIV))

	def test_cross_host_delivery_text_constrains_source_to_tenant_48(self):
		# Design §4b rule 5 folded in: a mesh-decap'd packet is accepted into this veth
		# ONLY from the VM's own tenant /48, so a cross-tenant inner source falls to the
		# terminal drop. The `saddr $t48` constraint is load-bearing for isolation.
		self.assertEqual(
			pn.cross_host_delivery_text(VETH, PRIV, T48),
			'iifname "wg-mesh" oifname "atlas-h1234567" '
			f"ip6 saddr {T48} ip6 daddr fdaa:f2b4:b043:0:b2c0:16db:1382:4247 accept",
		)

	def test_per_vm_texts_order(self):
		texts = pn.per_vm_texts(VETH, PRIV, T48)
		self.assertEqual(len(texts), 4)
		self.assertEqual(texts[0], pn.anti_spoof_text(VETH, PRIV))
		self.assertEqual(texts[3], pn.cross_host_delivery_text(VETH, PRIV, T48))


class TestCommands(unittest.TestCase):
	"""The nft command builders: values auto-quoted via _substitute, keywords kept as
	separate tokens (so run('sudo nft ' + cmd) shlex-splits to the right argv)."""

	def test_terminal_drop_command_is_add_tail(self):
		# `add` (tail) keeps the terminal drop LAST, below every inserted per-VM allow.
		self.assertEqual(
			shlex.split(pn._terminal_drop_command()),
			["add", "rule", "inet", "atlas", "forward", "ip6", "daddr", "fdaa::/16", "drop"],
		)

	def test_anti_spoof_command_keeps_ne_token(self):
		# The != operator must survive as its own token, and the veth/addr as single
		# tokens (a spoofable interface name with a space would still be one token).
		argv = shlex.split(pn._anti_spoof_command(VETH, PRIV))
		self.assertIn("insert", argv)
		self.assertIn("!=", argv)
		self.assertIn(VETH, argv)
		self.assertIn(PRIV, argv)

	def test_infra_command_uses_uncanonical_input(self):
		# The COMMAND passes fdaa:0:0::/48 (nft accepts + canonicalizes it); the GUARD
		# text uses fdaa::/48. Both are correct — this asserts the split is deliberate.
		argv = shlex.split(pn._infra_destination_command(VETH, PRIV))
		self.assertIn("fdaa:0:0::/48", argv)

	def test_per_vm_commands_count(self):
		self.assertEqual(len(pn._per_vm_commands(VETH, PRIV, T48)), 4)


class TestHandleSweep(unittest.TestCase):
	"""_handles_for scrapes `nft -a` handles for THIS vm's rules only — keyed on the
	private /128 or the veth, never the host-wide terminal drop."""

	def _listing(self) -> str:
		# A realistic `nft -a list chain` for a host running one VM + the terminal drop.
		return (
			"table inet atlas {\n"
			"	chain forward {\n"
			"		type filter hook forward priority filter; policy accept;\n"
			f'		iifname "wg-mesh" oifname "{VETH}" ip6 daddr {PRIV} accept # handle 6\n'
			f'		iifname "{VETH}" ip6 saddr {PRIV} ip6 daddr fdaa::/48 accept # handle 5\n'
			f'		iifname "{VETH}" ip6 saddr {PRIV} ip6 daddr {T48} accept # handle 4\n'
			f'		iifname "{VETH}" ip6 daddr fdaa::/16 ip6 saddr != {PRIV} drop # handle 3\n'
			"		ip6 daddr fdaa::/16 drop # handle 2\n"
			"	}\n"
			"}\n"
		)

	def test_sweeps_all_four_per_vm_rules(self):
		handles = list(pn._handles_for(self._listing(), PRIV, VETH))
		self.assertEqual(sorted(handles), ["3", "4", "5", "6"])

	def test_never_sweeps_the_terminal_drop(self):
		# handle 2 (the host-wide fdaa::/16 drop) mentions neither the /128 nor the veth.
		handles = list(pn._handles_for(self._listing(), PRIV, VETH))
		self.assertNotIn("2", handles)

	def test_dark_vm_veth_only_still_sweeps_cross_host_rule(self):
		# A dark VM (no public /128) is torn down by veth match — the cross-host rule 4
		# carries the /128 AND the veth, so both keys find it; assert the veth key alone
		# (a different VM's /128 passed) still catches every rule that names the veth.
		handles = list(pn._handles_for(self._listing(), "fdaa:dead:beef::1", VETH))
		self.assertEqual(sorted(handles), ["3", "4", "5", "6"])


if __name__ == "__main__":
	unittest.main()
