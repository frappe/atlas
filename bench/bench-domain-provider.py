#!/usr/bin/env python3
# In-guest domain provider for self-service subdomain routing (spec/18 Component D) —
# run INSIDE a bench VM, installed by build.sh at /usr/local/bin/bench-domain-provider.
# This is the pilot (formerly bench-cli) `bench-domain-provider` plug-in: pilot looks
# this binary up on PATH, calls it per-verb, and reads ONLY its exit code + stdout JSON
# (docs/domain-provider.md). It is the thin "push" half of the one-way model — the guest
# TELLS the controller what changed; the controller never reads the guest back. The
# controller stays the single authoritative writer of the fleet-wide-unique Subdomain
# table; this binary only carries the guest's word, and every rule (uniqueness, reserved,
# brand denylist, per-VM cap, own-VM scoping) is arbitrated controller-side.
#
# Verbs (the contract pilot drives — domain-provider.md):
#
#   bench-domain-provider generate-dns-records <site> <domain>
#       PRE-FLIGHT, read-only — report the DNS records the user adds at THEIR provider so
#       the name points here. A wildcard subdomain we already route needs NO records, so
#       this prints {} and exits 0. (Custom-domain record recipes are Phase 2.) Fail-open.
#
#   bench-domain-provider register <domain>
#       BEFORE `bench new-site` — the AUTHORITATIVE reservation. Peels the region wildcard
#       suffix off <domain> to the bare label, POSTs register(label). Exit 0 = route live;
#       exit 2 = declined (taken/reserved/at_limit/invalid OR a non-wildcard domain, which
#       Phase 1 does not route); exit 1 = transport failure. FAIL-CLOSED: an unreachable
#       controller blocks the create (don't let a site exist with no route).
#
#   bench-domain-provider deregister <domain>
#       AFTER `bench drop-site`, AND as the rollback when new-site fails — best-effort,
#       ALWAYS exit 0 (a non-zero would throw on an otherwise-successful drop).
#
#   bench-domain-provider wildcard-domains
#       HOST-LEVEL — the wildcard pattern(s) sites here may be named under. Prints a JSON
#       list (["*.<region>.frappe.dev"]). Fail-soft (blank + exit 0 on outage).
#
#   bench-domain-provider proxy-servers
#       HOST-LEVEL — the edge proxies' public IPs that front this bench. Prints a JSON
#       list; pilot then locks its nginx to them (allow … ; deny all;) and trusts their
#       XFF. Fail-soft (blank + exit 0 on outage).
#
# Exit-code convention (domain-provider.md "Errors"): 0 = success / deliberate fail-soft;
# 1 = transport/config failure; 2 = declined (taken/reserved/at-limit/invalid); 64 =
# usage error. pilot only distinguishes zero vs non-zero, but the finer codes are the
# convention this binary follows.
#
# Caller resolution is by SOURCE ADDRESS — the binary carries NO VM-identifying argument
# and the POST MUST go over IPv6 (the controller resolves the VM from the request's source
# /128). A v4 POST arrives NAT'd with no per-VM source, so the binary pins the connection
# to AF_INET6 and treats "no v6 route to the controller" as a transport error, never a v4
# retry.
#
# Identity is ONE non-secret file the controller injects (spec/18 "Identity"):
#   /etc/atlas-routing.env — ATLAS_BASE_URL=<trusted-edge base url> the guest POSTs to
# No VM UUID, no token. When the file is ABSENT the binary no-ops cleanly (register exits
# 0, host queries print blank), so an ordinary (non-Atlas) bench is unaffected.
#
# Stdlib only (the guest has no Atlas package).

import http.client
import json
import socket
import sys
import urllib.parse

ROUTING_ENV_PATH = "/etc/atlas-routing.env"

_METHOD = "atlas.atlas.bench_routing.{}"

# Exit codes (domain-provider.md): pilot reads only zero vs non-zero, but we follow the
# documented convention so a human running the binary by hand gets a useful code.
EX_OK = 0
EX_TRANSPORT = 1  # unreachable / no v6 route / config failure
EX_DECLINED = 2  # taken / reserved / at-limit / invalid / not a routable wildcard name
EX_USAGE = 64  # unknown verb / wrong arg count

# Short timeouts: register runs inline in the interactive `bench new-site` flow (the user
# waits on the answer); the rest are best-effort/host queries.
_TIMEOUT_SECONDS = 10


# --- typed failures (the verb wrappers decide fail-open vs fail-closed) -----------


class NotConfigured(Exception):
	"""/etc/atlas-routing.env absent — the no-op signal (not an Atlas-routed bench)."""


class TransportError(Exception):
	"""Unreachable / no v6 route / timeout / bad JSON / an unknown wire status."""


# --- the IPv6-only HTTP transport -------------------------------------------------


class _IPv6HTTPConnection(http.client.HTTPConnection):
	"""Force the connection to the IPv6 address family. Caller resolution matches the
	request's source /128 against Virtual Machine.ipv6_address, so the POST MUST reach
	the controller over IPv6 — a v4 POST arrives NAT'd with no per-VM source to resolve.
	We resolve the host to its AAAA and connect over AF_INET6 only; if there is no v6
	route (no AAAA / connect fails), that is a TransportError, NEVER a v4 fallback."""

	def connect(self) -> None:
		try:
			infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET6, socket.SOCK_STREAM)
		except OSError as error:
			raise TransportError(f"no IPv6 route to {self.host}:{self.port} ({error})") from error
		if not infos:
			raise TransportError(f"controller {self.host} has no AAAA (IPv6) address")
		last: Exception | None = None
		for _family, socktype, proto, _canon, sockaddr in infos:
			try:
				self.sock = socket.socket(socket.AF_INET6, socktype, proto)
				self.sock.settimeout(self.timeout)
				self.sock.connect(sockaddr)
				return
			except OSError as error:
				last = error
				if self.sock:
					self.sock.close()
		raise TransportError(f"could not connect to {self.host} over IPv6 ({last})")


class _IPv6HTTPSConnection(http.client.HTTPSConnection):
	"""The TLS variant — same AF_INET6-only connect, wrapped in the SSL context the base
	class builds. (Production posts to the trusted-edge FQDN over https.)"""

	def connect(self) -> None:
		try:
			infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET6, socket.SOCK_STREAM)
		except OSError as error:
			raise TransportError(f"no IPv6 route to {self.host}:{self.port} ({error})") from error
		if not infos:
			raise TransportError(f"controller {self.host} has no AAAA (IPv6) address")
		last: Exception | None = None
		for _family, socktype, proto, _canon, sockaddr in infos:
			sock = None
			try:
				sock = socket.socket(socket.AF_INET6, socktype, proto)
				sock.settimeout(self.timeout)
				sock.connect(sockaddr)
				# `self._context` is HTTPSConnection's default SSL context, which verifies
				# the cert + hostname — the trust-root transport posts to the edge FQDN.
				self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
				return
			except OSError as error:
				# ssl.SSLError subclasses OSError, so a TLS handshake failure lands here
				# too — close the connected socket so a failed handshake never leaks an fd.
				last = error
				if sock:
					sock.close()
		raise TransportError(f"could not connect to {self.host} over IPv6 ({last})")


def _read_base_url() -> str:
	"""The controller base URL from /etc/atlas-routing.env (ATLAS_BASE_URL=…). Raises
	NotConfigured when the file is absent/blank — the signal that makes the verb no-op
	cleanly (this is not an Atlas-routed bench)."""
	try:
		with open(ROUTING_ENV_PATH) as handle:
			content = handle.read()
	except OSError as error:
		raise NotConfigured(f"{ROUTING_ENV_PATH} absent ({error})") from error
	for line in content.splitlines():
		line = line.strip()
		if line.startswith("ATLAS_BASE_URL="):
			value = line.split("=", 1)[1].strip()
			if value:
				return value
	raise NotConfigured(f"{ROUTING_ENV_PATH} has no ATLAS_BASE_URL")


def _post(base_url: str, method: str, params: dict) -> dict:
	"""POST to a whitelisted Frappe method over IPv6 and return its `message` payload.

	Frappe wraps a whitelisted return value as `{"message": <value>}`; we unwrap it.
	Form-encoded body, so `frappe.form_dict` and the @rate_limit key see the params
	exactly as the SPA/signup paths send them. Every transport-level failure (no v6
	route, timeout, non-2xx, bad JSON) is a TransportError the caller decides on."""
	parsed = urllib.parse.urlsplit(base_url)
	host = parsed.hostname
	if not host:
		raise TransportError(f"malformed ATLAS_BASE_URL {base_url!r}")
	if parsed.scheme == "https":
		port = parsed.port or 443
		connection = _IPv6HTTPSConnection(host, port, timeout=_TIMEOUT_SECONDS)
	else:
		port = parsed.port or 80
		connection = _IPv6HTTPConnection(host, port, timeout=_TIMEOUT_SECONDS)
	body = urllib.parse.urlencode(params)
	path = f"/api/method/{urllib.parse.quote(_METHOD.format(method), safe='.')}"
	try:
		connection.request(
			"POST",
			path,
			body=body,
			headers={
				"Content-Type": "application/x-www-form-urlencoded",
				"Accept": "application/json",
				"Host": host,
			},
		)
		response = connection.getresponse()
		raw = response.read().decode()
		if response.status >= 400:
			raise TransportError(f"{method} HTTP {response.status}: {raw[:300]}")
		payload = json.loads(raw)
	except TransportError:
		raise
	except (OSError, ValueError) as error:
		raise TransportError(f"{method} failed ({error})") from error
	finally:
		connection.close()
	return payload.get("message", payload) if isinstance(payload, dict) else payload


# --- label peeling (full FQDN → bare label the controller stores) -----------------


def _peel_label(domain: str, suffix: str) -> str | None:
	"""The bare label the controller's `register(label)` wants, peeled off the full FQDN
	pilot hands us (`app.blr1.frappe.dev` → `app`).

	`suffix` is the region wildcard's fixed part — `.blr1.frappe.dev` (from the
	`*.blr1.frappe.dev` pattern). A name UNDER the wildcard ends with that suffix and has
	a label before it; we strip the suffix and return what remains. Anything else — a name
	that doesn't end with the suffix (a custom external domain), or the bare suffix with no
	label — is NOT a Phase-1 wildcard subdomain, so we return None and the caller declines
	(custom-domain routing is Phase 2). A multi-label remainder (`a.b`) is returned as-is
	and the controller rejects it as `invalid` — fail-closed by deferral."""
	domain = (domain or "").strip().lower().rstrip(".")
	if not suffix:
		return None
	if domain == suffix.lstrip(".") or not domain.endswith(suffix):
		return None
	label = domain[: -len(suffix)]
	return label or None


def _region_suffix(base_url: str) -> str:
	"""The region wildcard's fixed suffix (`.blr1.frappe.dev`), derived from the
	controller's `wildcard_domains()`. The peel and the suffix come from the SAME source
	pilot's `matches_wildcard` gates on, so a name pilot routes to us is a name we can peel.
	Returns "" when the controller offers no wildcard (then the caller declines)."""
	result = _post(base_url, "wildcard_domains", {})
	patterns = (result or {}).get("domains") or []
	for pattern in patterns:
		if pattern.startswith("*"):
			return pattern[1:]  # "*.blr1.frappe.dev" -> ".blr1.frappe.dev"
	return ""


# --- the verbs --------------------------------------------------------------------


def _cmd_generate_dns_records(site: str, domain: str) -> int:
	"""Pre-flight (read-only): report the DNS records the user must add. A wildcard
	subdomain we already route needs NONE, so print `{}` and exit 0. The custom-domain
	record recipe (CNAME→site, A+AAAA→proxy, CAA→LE) is Phase 2. Fail-OPEN per the doc
	(the real gate is register): a no-config or transport blip still prints `{}`/exits 0
	so the Add-Domain UI isn't broken by a momentary outage."""
	try:
		base_url = _read_base_url()
		suffix = _region_suffix(base_url)
	except NotConfigured:
		print("{}")
		return EX_OK
	except TransportError as error:
		print(f"bench-domain-provider: generate-dns-records soft outage ({error})", file=sys.stderr)
		print("{}")
		return EX_OK
	# A wildcard subdomain we route: no records for the user to add. A non-wildcard name
	# would need the Phase-2 recipe; until then report nothing and let `register` decline.
	if _peel_label(domain, suffix) is None:
		print(f"bench-domain-provider: {domain} is not a routable wildcard subdomain", file=sys.stderr)
		return EX_DECLINED
	print("{}")
	return EX_OK


def _cmd_register(domain: str) -> int:
	"""register before new-site — the AUTHORITATIVE reservation. Peel the wildcard suffix
	to the bare label and POST register(label). FAIL-CLOSED (the deliberate change from
	atlas-route's fail-open): a transport error → exit 1 → pilot aborts the create, so a
	site never exists with no provisioned route. NotConfigured → exit 0 (not an Atlas
	bench; pilot proceeds with its built-in behaviour). A non-wildcard domain declines
	(exit 2) — Phase 1 routes only wildcard subdomains."""
	try:
		base_url = _read_base_url()
		suffix = _region_suffix(base_url)
		label = _peel_label(domain, suffix)
		if label is None:
			print(
				f"bench-domain-provider: {domain} is not a routable subdomain of {suffix.lstrip('.') or 'this host'}",
				file=sys.stderr,
			)
			return EX_DECLINED
		result = _post(base_url, "register", {"label": label})
	except NotConfigured as error:
		print(f"bench-domain-provider: no routing config ({error}); skipping register", file=sys.stderr)
		return EX_OK
	except TransportError as error:
		# FAIL-CLOSED: don't let the site get created if the route wasn't provisioned.
		print(f"bench-domain-provider: register transport failure ({error})", file=sys.stderr)
		return EX_TRANSPORT
	status = (result or {}).get("status")
	if status == "ok":
		suffix_echo = (result or {}).get("suffix") or suffix.lstrip(".")
		print(f"bench-domain-provider: reserved {label}.{suffix_echo}", file=sys.stderr)
		return EX_OK
	print(f"bench-domain-provider: {_decline_message(status, label, result)}", file=sys.stderr)
	return EX_DECLINED


def _cmd_deregister(domain: str) -> int:
	"""deregister after drop / as rollback — best-effort, ALWAYS exit 0. Peel the label
	and POST deregister(label). NotConfigured / TransportError are swallowed: a non-zero
	here would throw on an otherwise-successful drop (the lost route is the owner-cleared
	residual). A non-wildcard domain we never routed is likewise a clean exit 0."""
	try:
		base_url = _read_base_url()
		suffix = _region_suffix(base_url)
		label = _peel_label(domain, suffix)
		if label is None:
			return EX_OK
		_post(base_url, "deregister", {"label": label})
	except NotConfigured:
		return EX_OK
	except TransportError as error:
		print(
			f"bench-domain-provider: deregister failed ({error}); a stale route can be cleared later",
			file=sys.stderr,
		)
	return EX_OK


def _cmd_wildcard_domains() -> int:
	"""Host-level: print the wildcard pattern(s) sites here may be named under. Fail-SOFT
	per the doc — a no-config or transport blip prints blank + exits 0 (a non-zero raises
	an error and breaks pilot's Add-Domain UI)."""
	try:
		base_url = _read_base_url()
		result = _post(base_url, "wildcard_domains", {})
	except (NotConfigured, TransportError) as error:
		print(f"bench-domain-provider: wildcard-domains soft outage ({error})", file=sys.stderr)
		return EX_OK
	print(json.dumps((result or {}).get("domains") or []))
	return EX_OK


def _cmd_proxy_servers() -> int:
	"""Host-level: print the edge proxies' public IPs that front this bench. Fail-SOFT —
	blank + exit 0 on a no-config / transport blip (a non-zero breaks pilot's
	setup-nginx). When non-empty, pilot locks its nginx down to exactly these."""
	try:
		base_url = _read_base_url()
		result = _post(base_url, "proxy_servers", {})
	except (NotConfigured, TransportError) as error:
		print(f"bench-domain-provider: proxy-servers soft outage ({error})", file=sys.stderr)
		return EX_OK
	print(json.dumps((result or {}).get("ips") or []))
	return EX_OK


def _decline_message(status: str | None, label: str, result: dict) -> str:
	"""The operator-facing decline message. Prefers the controller's verbatim `reason`
	(invalid carries one), else a default per status. An unknown status is surfaced too —
	register treats every non-`ok` as a decline (exit 2), the conservative read of a
	controller answer this binary doesn't recognise."""
	reason = (result or {}).get("reason")
	if reason:
		return reason
	if status == "taken":
		return f"subdomain '{label}' is already in use — choose another"
	if status == "reserved":
		return f"subdomain '{label}' is reserved — choose another"
	if status == "at_limit":
		return "this VM has reached its subdomain limit — drop a site or use a bigger VM"
	if status == "invalid":
		return f"subdomain '{label}' is not a valid label"
	return f"register declined ({status!r})"


def main(argv: list) -> int:
	if len(argv) == 4 and argv[1] == "generate-dns-records":
		return _cmd_generate_dns_records(argv[2], argv[3])
	if len(argv) == 3 and argv[1] == "register":
		return _cmd_register(argv[2])
	if len(argv) == 3 and argv[1] == "deregister":
		return _cmd_deregister(argv[2])
	if len(argv) == 2 and argv[1] == "wildcard-domains":
		return _cmd_wildcard_domains()
	if len(argv) == 2 and argv[1] == "proxy-servers":
		return _cmd_proxy_servers()
	print(
		"usage: bench-domain-provider generate-dns-records <site> <domain> | "
		"register <domain> | deregister <domain> | wildcard-domains | proxy-servers",
		file=sys.stderr,
	)
	return EX_USAGE


if __name__ == "__main__":
	sys.exit(main(sys.argv))
