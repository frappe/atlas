#!/usr/bin/env python3
# Issue (or renew) the wildcard cert *.<domain> via certbot over an ACME DNS-01
# challenge, then emit the on-disk PEM paths and validity window as the typed
# ATLAS_RESULT= line the controller parses.
#
# UNLIKE every other script here, this one runs on the ATLAS CONTROLLER, not on a
# Server host: ACME issuance is a controller concern (the PEMs land where the
# control plane can read them to push to the proxy fleet), so there is no host to
# stage onto. It is still invoked through the same typed-CLI Task contract — the
# controller-local runner (atlas.atlas.local_task) calls
#   issue-cert.py --domain blr1.frappe.dev --acme-directory-url ... \
#                 --account-email ops@... --certbot-arg --dns-route53
# and the DNS vendor credentials arrive in the environment (e.g. AWS_ACCESS_KEY_ID
# or POWERDNS_API_KEY), never in argv. certbot + the DNS plugin are a
# controller-host dependency, not a
# server-runtime one, so spec principle #5's stdlib-only server rule is intact.
#
# Idempotent: certbot renews-or-skips a still-valid lineage (--keep-until-expiring).

import dataclasses
import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import atlas.certs as certs
from atlas._run import run
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class IssueCertInputs(TaskInputs):
	"""Issue *.<domain> via certbot DNS-01 on the controller."""

	command: typing.ClassVar[str] = "issue-cert"
	domain: str  # the wildcard zone, e.g. blr1.frappe.dev → issues *.blr1.frappe.dev
	acme_directory_url: str  # ACME server directory (LE production or staging)
	account_email: str  # ACME registration / expiry-notice email
	# The DNS plugin NAME (e.g. route53), from the DNS provider. Kept for the
	# typed contract and for providers whose certbot args are derived in this script.
	dns_authenticator: str
	# Full certbot authenticator argv from the DNS provider. Repeatable. Empty keeps
	# the legacy `--dns-<dns_authenticator>` rendering.
	certbot_arg: list[str] = dataclasses.field(default_factory=list)


@dataclass(frozen=True)
class IssueCertResult(TaskResult):
	fullchain_path: str
	privkey_path: str
	not_before: str
	not_after: str


def _write_powerdns_credentials(domain: str) -> None:
	api_url = os.environ.get("POWERDNS_API_URL")
	api_key = os.environ.get("POWERDNS_API_KEY")
	server_id = os.environ.get("POWERDNS_SERVER_ID") or "localhost"
	if not api_url or not api_key:
		sys.exit("POWERDNS_API_URL and POWERDNS_API_KEY are required for PowerDNS DNS-01")
	path = certs.powerdns_credentials_path(domain)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as handle:
		handle.write(f"dns_pdns_endpoint = {api_url}\n")
		handle.write(f"dns_pdns_api_key = {api_key}\n")
		handle.write(f"dns_pdns_server_id = {server_id}\n")
		handle.write(f"dns_pdns_disable_notify = false\n")
	os.chmod(path, 0o600)


def main() -> None:
	inputs = IssueCertInputs.from_args()
	if inputs.dns_authenticator == "powerdns":
		_write_powerdns_credentials(inputs.domain)

	run(
		certs.certbot_command(
			domain=inputs.domain,
			acme_directory_url=inputs.acme_directory_url,
			account_email=inputs.account_email,
			dns_authenticator=inputs.dns_authenticator,
			certbot_args=inputs.certbot_arg,
		)
	)

	fullchain = certs.fullchain_path(inputs.domain)
	privkey = certs.privkey_path(inputs.domain)
	if not os.path.isfile(fullchain):
		sys.exit(f"certbot reported success but {fullchain} is missing")

	dates = run("openssl x509 -noout -dates -in {}", fullchain)
	not_before, not_after = certs.parse_openssl_dates(dates)

	IssueCertResult(
		fullchain_path=fullchain,
		privkey_path=privkey,
		not_before=not_before,
		not_after=not_after,
	).emit()
	print(f"Issued *.{inputs.domain}; valid until {not_after}.")


if __name__ == "__main__":
	main()
