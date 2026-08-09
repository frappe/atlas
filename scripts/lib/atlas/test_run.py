"""Unit tests for the `{}`-placeholder command engine in `atlas._run`.

Run with bare `python3 -m unittest atlas.test_run` from scripts/lib: no Frappe,
no host, no subprocess. These cover the rendering/quoting contract that makes
`run("sudo ip link set {} up", tap)` safe — the property the whole single-string
convention rests on:

  - literals pass through verbatim (incl. nft `{ … }` brace clauses — the key
    reason the engine is custom and NOT str.format);
  - every param becomes EXACTLY ONE argv token, no matter what it contains
    (the injection battery);
  - arity mismatches raise loud;
  - a `{}` *inside a param value* is data, never a placeholder.
"""

import unittest

from atlas._run import _render, _substitute


class TestSubstitute(unittest.TestCase):
	def test_literals_pass_through(self):
		self.assertEqual(_substitute("sudo systemctl stop nginx", ()), "sudo systemctl stop nginx")

	def test_one_param_is_quoted(self):
		self.assertEqual(_substitute("ip link set {} up", ("tap0",)), "ip link set tap0 up")

	def test_param_with_space_is_quoted_to_one_token(self):
		self.assertEqual(_substitute("echo {}", ("a b",)), "echo 'a b'")

	def test_nft_brace_clause_is_left_verbatim(self):
		# The whole point of the custom engine: nft's `{ … }` survives with ZERO
		# escaping (str.format would have demanded `{{ }}`).
		clause = "add chain inet atlas forward { type filter hook forward priority filter; policy accept; }"
		self.assertEqual(_substitute(clause, ()), clause)

	def test_brace_in_param_value_is_not_a_placeholder(self):
		# A literal `{}` inside a *param* is data, quoted like anything else.
		self.assertEqual(_substitute("echo {}", ("{}",)), "echo '{}'")

	def test_too_few_params_raises(self):
		with self.assertRaises(TypeError):
			_substitute("a {} {}", ("x",))

	def test_too_many_params_raises(self):
		with self.assertRaises(TypeError):
			_substitute("a {}", ("x", "y"))

	def test_param_for_no_placeholder_raises(self):
		with self.assertRaises(TypeError):
			_substitute("a", ("x",))

	def test_non_string_param_is_stringified(self):
		self.assertEqual(_substitute("--port {}", (443,)), "--port 443")


class TestRender(unittest.TestCase):
	def test_splits_into_argv(self):
		self.assertEqual(
			_render("sudo ip link set {} up", ("tap0",)), ["sudo", "ip", "link", "set", "tap0", "up"]
		)

	def test_param_with_space_stays_one_argv_token(self):
		self.assertEqual(_render("a {} b", ("x y",)), ["a", "x y", "b"])

	def test_nft_clause_as_param_is_one_argv_token(self):
		# Trap 2: nft's brace clause passed as a {} param reaches nft as ONE argv
		# element, braces and `;` intact — no shell, no re-tokenization.
		clause = "{ type filter hook forward priority filter; policy accept; }"
		argv = _render("nft add chain inet atlas forward {}", (clause,))
		self.assertEqual(argv[-1], clause)
		self.assertEqual(argv[:-1], ["nft", "add", "chain", "inet", "atlas", "forward"])

	def test_injection_battery_each_survives_as_one_token(self):
		# A malicious/awkward value can never break out of its slot, become a new
		# argv token, or invoke a shell — there is no shell, and quoting makes it one.
		for evil in [
			"a; rm -rf /",
			"a | tee /etc/passwd",
			"$(whoami)",
			"`id`",
			"a && reboot",
			"' ; echo pwned ; '",
			'" double',
			"../../etc/shadow",
			"a\tb",
			"a\nb",
			"--flag=value with spaces",
			"{ nft brace }",
		]:
			argv = _render("echo {}", (evil,))
			self.assertEqual(argv, ["echo", evil], f"value did not survive as one token: {evil!r}")

	def test_two_params(self):
		self.assertEqual(
			_render("ip -6 route replace {}/128 dev {}", ("2400:dead::1", "tap0")),
			["ip", "-6", "route", "replace", "2400:dead::1/128", "dev", "tap0"],
		)


if __name__ == "__main__":
	unittest.main()
