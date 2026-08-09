"""Pure helpers for the issue-cert task — certbot argv + cert-path layout.

Kept out of issue-cert.py so the argv construction and the on-disk layout are
unit-testable with no certbot and no host (the same split the LVM/network task
libs use: pure string logic here, the one subprocess in the entry point). The
controller-side layout lives under the Atlas home (`~/.atlas/certbot`), a sibling
of the SSH `known_hosts` dir, so all controller-local Atlas state sits together.
"""

from __future__ import annotations

import os
import shlex
import sys

from atlas._run import _substitute


def atlas_home() -> str:
	"""The Atlas controller home, `~/.atlas`. Matches the SSH transport's
	`KNOWN_HOSTS_PATH` parent so controller state is colocated."""
	return os.path.join(os.path.expanduser("~"), ".atlas")


def certbot_executable() -> str:
	return os.path.join(os.path.dirname(sys.executable), "certbot")


def certbot_config_dir(domain: str) -> str:
	"""certbot's --config-dir for this domain. Per-domain so accounts/renewal
	state never collide across regions."""
	return os.path.join(atlas_home(), "certbot", domain)


def live_dir(domain: str) -> str:
	"""Where certbot writes the live symlinks for `*.<domain>`. certbot names the
	lineage after the first -d, with the leading `*.` stripped to `<domain>`."""
	return os.path.join(certbot_config_dir(domain), "live", domain)


def powerdns_credentials_path(domain: str) -> str:
	return os.path.abspath(os.path.join(certbot_config_dir(domain), "powerdns.ini"))


def powerdns_certbot_args(domain: str) -> list[str]:
	return [
		"--authenticator",
		"dns-pdns",
		"--dns-pdns-credentials",
		powerdns_credentials_path(domain),
	]


def fullchain_path(domain: str) -> str:
	return os.path.join(live_dir(domain), "fullchain.pem")


def privkey_path(domain: str) -> str:
	return os.path.join(live_dir(domain), "privkey.pem")


def certbot_command(
	domain: str,
	acme_directory_url: str,
	account_email: str,
	dns_authenticator: str,
	certbot_args: list[str] | None = None,
) -> str:
	"""The full certbot command line to issue (or renew) `*.<domain>`
	non-interactively over DNS-01, rendered as a single auto-quoted string for
	`_run.run`. `dns_authenticator` is the DNS plugin name (e.g. `route53`);
	when `certbot_args` is omitted it is rendered as `--dns-<name>`. Providers with
	a different certbot CLI shape pass explicit `certbot_args`. Credentials travel
	via the environment or a controller-local 0600 file, never as secret argv
	values. Idempotent: certbot renews-or-skips a still-valid lineage."""
	config = certbot_config_dir(domain)
	dns_args = certbot_args or _certbot_args_for(domain, dns_authenticator)
	dns_arg_string = " ".join(shlex.quote(arg) for arg in dns_args)
	return (
		_substitute(
			"{} certonly --non-interactive --agree-tos -m {} --server {}",
			(certbot_executable(), account_email, acme_directory_url),
		)
		+ " "
		+ dns_arg_string
		+ " "
		+ _substitute(
			"-d {} --config-dir {} --work-dir {} --logs-dir {} --keep-until-expiring",
			(
				f"*.{domain}",
				config,
				os.path.join(config, "work"),
				os.path.join(config, "logs"),
			),
		)
	)


def _certbot_args_for(domain: str, dns_authenticator: str) -> list[str]:
	if dns_authenticator == "powerdns":
		return powerdns_certbot_args(domain)
	return [f"--dns-{dns_authenticator}"]


def parse_openssl_dates(stdout: str) -> tuple[str, str]:
	"""Parse `openssl x509 -noout -dates` output into (not_before, not_after) as
	the raw OpenSSL date strings (e.g. `Jun  8 00:00:00 2026 GMT`). The controller
	normalizes these to its Datetime fields via frappe.utils.get_datetime, which
	parses this format. Raises ValueError if either line is missing."""
	not_before = not_after = None
	for line in stdout.splitlines():
		if line.startswith("notBefore="):
			not_before = line[len("notBefore=") :].strip()
		elif line.startswith("notAfter="):
			not_after = line[len("notAfter=") :].strip()
	if not_before is None or not_after is None:
		raise ValueError(f"could not parse notBefore/notAfter from openssl output: {stdout!r}")
	return not_before, not_after
