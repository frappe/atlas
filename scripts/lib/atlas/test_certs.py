"""Unit tests for the issue-cert helpers — argv construction, path layout, and
openssl-date parsing.

Run with bare `python3 -m unittest atlas.test_certs` from scripts/lib: no certbot,
no openssl, no host. These cover everything in the issue-cert task except the two
subprocess calls (certbot, openssl), which the entry point makes via _run.run."""

import os
import shlex
import unittest

from atlas import certs


def _argv(domain, acme, email, authenticator, certbot_args=None):
	return shlex.split(certs.certbot_command(domain, acme, email, authenticator, certbot_args))


DOMAIN = "blr1.frappe.dev"
ACME = "https://acme-staging-v02.api.letsencrypt.org/directory"
EMAIL = "ops@frappe.dev"


class TestCertbotArgv(unittest.TestCase):
	def test_issues_the_wildcard_for_the_domain(self):
		argv = _argv(DOMAIN, ACME, EMAIL, "route53")
		self.assertTrue(argv[0].endswith("/certbot"))
		self.assertEqual(argv[1], "certonly")
		self.assertIn("--non-interactive", argv)
		# The cert is the wildcard *.<domain>, requested via -d.
		d_index = argv.index("-d")
		self.assertEqual(argv[d_index + 1], "*.blr1.frappe.dev")

	def test_renders_the_dns_authenticator_flag(self):
		# The plain authenticator name becomes the --dns-<name> certbot flag.
		argv = _argv(DOMAIN, ACME, EMAIL, "route53")
		self.assertIn("--dns-route53", argv)

	def test_uses_provider_supplied_certbot_args(self):
		argv = _argv(
			DOMAIN,
			ACME,
			EMAIL,
			"powerdns",
			[
				"--authenticator",
				"dns-pdns",
				"--dns-pdns-credentials",
				"/home/atlas/.atlas/certbot/blr1.frappe.dev/powerdns.ini",
			],
		)
		self.assertNotIn("--dns-powerdns", argv)
		self.assertEqual(argv[argv.index("--authenticator") + 1], "dns-pdns")
		self.assertEqual(
			argv[argv.index("--dns-pdns-credentials") + 1],
			"/home/atlas/.atlas/certbot/blr1.frappe.dev/powerdns.ini",
		)

	def test_no_credentials_in_argv(self):
		# Credentials travel via the environment; nothing AWS-shaped is in argv.
		argv = _argv(DOMAIN, ACME, EMAIL, "route53")
		self.assertFalse(any("AWS" in token or "secret" in token.lower() for token in argv))

	def test_account_email_and_server_are_passed(self):
		argv = _argv(DOMAIN, ACME, EMAIL, "route53")
		email_flag = argv.index("-m", argv.index("--agree-tos"))
		self.assertEqual(argv[email_flag + 1], EMAIL)
		self.assertEqual(argv[argv.index("--server") + 1], ACME)

	def test_config_dir_is_per_domain_under_atlas_home(self):
		argv = _argv(DOMAIN, ACME, EMAIL, "route53")
		config = argv[argv.index("--config-dir") + 1]
		self.assertTrue(config.endswith(os.path.join(".atlas", "certbot", DOMAIN)))


class TestCertPaths(unittest.TestCase):
	def test_powerdns_credentials_path_is_absolute(self):
		self.assertTrue(os.path.isabs(certs.powerdns_credentials_path(DOMAIN)))
		self.assertTrue(certs.powerdns_credentials_path(DOMAIN).endswith(os.path.join(DOMAIN, "powerdns.ini")))

	def test_pem_paths_live_under_the_domain_live_dir(self):
		self.assertTrue(certs.fullchain_path(DOMAIN).endswith(os.path.join("live", DOMAIN, "fullchain.pem")))
		self.assertTrue(certs.privkey_path(DOMAIN).endswith(os.path.join("live", DOMAIN, "privkey.pem")))


class TestOpensslDates(unittest.TestCase):
	def test_parses_not_before_and_not_after(self):
		out = "notBefore=Jun  8 00:00:00 2026 GMT\nnotAfter=Sep  6 23:59:59 2026 GMT\n"
		not_before, not_after = certs.parse_openssl_dates(out)
		self.assertEqual(not_before, "Jun  8 00:00:00 2026 GMT")
		self.assertEqual(not_after, "Sep  6 23:59:59 2026 GMT")

	def test_raises_when_dates_missing(self):
		with self.assertRaises(ValueError):
			certs.parse_openssl_dates("some unrelated output\n")


if __name__ == "__main__":
	unittest.main()
