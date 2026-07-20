# TLS & Domain Layer

The reverse proxy ([12-proxy.md](./12-proxy.md)) terminates TLS for
`*.<region>.frappe.dev` with a wildcard cert it receives through
`atlas.atlas.proxy.push_cert(vm, fullchain, privkey)`. That function was always a
**consumer with no producer** — nothing in Atlas issued the PEMs it expects. This
layer is the producer: it tracks root domains, issues their regional wildcard cert
via Let's Encrypt over a DNS-01 challenge, and pushes the result onto every proxy
VM in the fleet.

The shape mirrors the compute Provider abstraction
([01-architecture.md](./01-architecture.md)): two small registries (DNS, TLS),
each an ABC with one implementation per vendor type, resolved by **type** so
callers never branch on the vendor. The active types both live on `Atlas
Settings` — the DNS vendor on `dns_provider_type`, the TLS issuer on
`tls_provider_type` — with no `Domain Provider` / `TLS Provider` DocTypes.

## The flow

```
Root Domain ──Issue / Renew Certificate──▶ TLS Certificate.issue()
                          │  TlsProvider.issue(domain, dns_provider)
                          │  → issue-cert.py Task on the CONTROLLER (certbot DNS-01)
                          ▼
                       PEMs on the controller's disk (fullchain_path, privkey_path)
                          │
                          ▼  _push_to_proxies(): for vm in proxy._proxy_vms()
                       atlas.atlas.proxy.push_cert(vm, fullchain, privkey)   ← EXISTING
                          │     ▼
                          │  nginx reload on each proxy guest
                          ▼  then publish the public routing record:
                       dns_provider.upsert_wildcard(domain, fleet A+AAAA)
                          ▼
                       *.<domain> A → proxy reserved IPv4s, AAAA → proxy /128s
```

One `Root Domain` row == one region == one wildcard. `Root Domain.region` freezes
the region (the wildcard domain suffix) at insert. Atlas is single-region, so the
proxy fleet is the whole set of `Virtual Machine.is_proxy=1` rows (`proxy._proxy_vms`)
— issuance never needs to know which VMs are proxies, and there is no per-region
filter to apply.

## Custom domains: SNI passthrough, the VM holds the cert

The flow above is for the **regional wildcard** (`*.<region>.frappe.dev`), which
the proxy terminates. A **custom domain** — an FQDN the customer owns
(`shop.acme.com`), routed to one site VM via the `Custom Domain` DocType
([18-bench-self-routing.md](./18-bench-self-routing.md)) — does **not** terminate
at the proxy and Atlas issues **no** cert for it. The proxy reads the SNI at L4
(nginx `ssl_preread`, no decrypt) and forwards the **raw TLS stream** to the
backend VM's `:443`; the **VM terminates TLS with its own Let's Encrypt cert**.
There is no per-domain cert in the Atlas TLS layer, no `push_cert` for a custom
domain, no `TLS Certificate` row — `Custom Domain.status` is informational only.

**Why passthrough, not a second issuer.** The trust boundary is symmetric: each
party terminates the TLS whose private key it owns. The proxy owns the wildcard
key (DNS-01) and can't share it down — a VM holding it could impersonate every
sibling subdomain. The VM owns its custom-domain key and won't share it up. So the
proxy terminates wildcard subdomains and passes custom domains through; neither key
crosses the boundary.

**The VM self-issues over HTTP-01.** The site VM already runs an in-guest nginx
that terminates `:443` with a real per-site SAN cert and serves ACME HTTP-01
challenges from its own webroot (it is built to run standalone on an
internet-facing VM). DNS-01 from the VM is rejected — it would need a
certbot-supported DNS provider *and* the customer's DNS credentials in the guest,
neither guaranteed. HTTP-01 only needs the challenge to reach the VM's `:80`.

**The proxy forks :443 (SNI) and :80 (Host header)** — see
[12-proxy.md § The stream front-door](./12-proxy.md#the-stream-front-door-sni-passthrough-for-custom-domains)
for the topology. The one subtlety that lives in *this* layer: the proxy must
answer `*.<region>.frappe.dev` ACME challenges **itself** (serve `:80`
`/.well-known/acme-challenge/` locally for any host under the wildcard suffix) and
passthrough ACME to the VM **only** for custom domains — so that no tenant VM can
ever satisfy a challenge for the regional wildcard and have a CA issue it a
`*.<region>.frappe.dev` cert. The wildcard-vs-custom test is the same host-suffix
predicate the router uses.

**No readiness gate — both maps fill on registration.** A custom domain enters
**both** the proxy's `:80` ACME-passthrough map (so the VM can issue) and its
`:443` SNI-passthrough map the moment it is registered. If the VM's cert isn't
issued yet, a `:443` handshake is forwarded to a VM that can't complete it — the
client sees a transient TLS cert error (wrong-cert / authority warning) that
self-heals the moment the VM's cert lands. That is harmless: pure SNI passthrough,
the proxy never decrypts and no other tenant is affected, so gating the `:443` map
on a cert-confirmation signal isn't worth its machinery. (Pilot keeps on-script
users off the domain until TLS is issued; an off-script user who points DNS early
gets the transient error.) An SNI lookup miss is only an unknown / deregistered
name; a *named* miss terminates on the self-signed placeholder cert and serves a
branded "Domain not configured" page (the client clicks through a cert warning,
since we hold no trusted cert for a domain we don't control — the wildcard-subdomain
miss keeps its own warning-free page under the valid wildcard cert), while an
SNI-less connection (bare IP / probe) is dropped at L4. (See
[llm/references/custom-domain-sni-passthrough.md](../llm/references/custom-domain-sni-passthrough.md)
and [llm/references/drop-custom-domain-readiness-gate.md](../llm/references/drop-custom-domain-readiness-gate.md)
for the full rationale.)

## Abstractions

Two registries under `atlas/atlas/`, each modeled on `atlas/atlas/providers/`:

- **`dns/`** — the DNS seam. `DnsProvider(ABC)`: `authenticate()`,
  `credential_env()` (vendor secrets as env for `issue-cert.py` / certbot),
  `certbot_authenticator()` (stable provider name, e.g. `route53`),
  `certbot_args(domain)` (the exact certbot authenticator argv), and
  `upsert_wildcard(domain, targets)` (publish the public `*.<domain>` A/AAAA
  records that point the regional wildcard at the proxy fleet — A → the proxies'
  reserved IPv4s, AAAA → their `/128`s, round-robin). `for_dns_provider_type(type)`
  resolves the active `Atlas Settings.dns_provider_type` to an instance.
  `Route53DnsProvider` and `PowerDNSDnsProvider` are implemented; Cloudflare is a
  reserved Select option.

  The challenge TXT records are certbot's job (Atlas never writes them); the
  durable `*.<domain>` record is Atlas's, reconciled by `TLS Certificate`'s
  `_push_to_proxies` on every issue/renew/push (so a rebuilt proxy's new `/128`
  or a reattached reserved IP is reflected). Without it the cert proves identity
  but `<sub>.<domain>` resolves to nothing.
- **`tls/`** — the issuer seam. `TlsProvider(ABC)`: `authenticate()` and
  `issue(domain, dns_provider) -> IssuedCert` (on-disk PEM paths + validity
  window). `for_tls_provider_type(type)` resolves the active
  `Atlas Settings.tls_provider_type`. `LetsEncryptProvider` is implemented;
  `ZeroSslProvider` is a stub (`frappe.throw`); `SelfManagedTlsProvider`
  expects operator-supplied PEMs.

Atlas talks to DNS/TLS vendors only through these interfaces.

## The issue-cert Task runs on the controller

Certificate issuance is the first **controller-local** Task: the ACME client runs
where the PEMs land (the controller, which the proxy control plane reaches from),
and there is no remote host to stage a script onto. So:

- `scripts/issue-cert.py` is an ordinary typed-CLI Task
  ([04-tasks.md](./04-tasks.md)) — `IssueCertInputs.from_args()` in,
  `IssueCertResult.emit()` (the one `ATLAS_RESULT=` line) out — but it is invoked
  by `atlas.atlas.local_task.run_local_task` as a **local subprocess**, not over
  SSH. It is excluded from `scripts_catalog.allowed_scripts()` (the host run-task
  gate) via `CONTROLLER_ONLY`, so it never appears as a host Task or in the
  operator picker, but `resolve()` still finds it for the local runner.
- A `Task` row is still recorded, so a cert issuance shows up in the same audit
  list as every host/guest op.
- The DNS provider passes a stable authenticator name plus provider-specific
  certbot args through repeatable `--certbot-arg` flags. Route53 renders
  `--dns-route53`; PowerDNS renders the third-party plugin's authenticator and a
  credentials-file path.
- Vendor credentials travel through the subprocess **environment** and, when a
  plugin requires it, a controller-local `0600` credentials file. Secret values are
  never placed in argv, so they never appear in `ps`.

certbot + openssl + the selected DNS plugin are a **controller-host dependency**
(documented here; install on the Atlas controller). Route53 needs
`certbot-dns-route53` + `boto3`; PowerDNS needs `certbot-dns-powerdns`. They are *not* a server- or
script-runtime dependency, so the server-side "stdlib only" rule
([04-tasks.md](./04-tasks.md), principle #5) is intact: `scripts/lib/atlas/certs.py`
is pure stdlib string logic, and the two subprocess calls (certbot, openssl) live
in the entry point.

On-disk layout, controller-local: `~/.atlas/certbot/<domain>/` (certbot
config/work/logs), with the live PEMs at
`~/.atlas/certbot/<domain>/live/<domain>/{fullchain,privkey}.pem`. Sibling of the
SSH `~/.atlas/known_hosts`, so all controller-local Atlas state sits together. The
`TLS Certificate` row stores only the **paths** — private-key bytes stay out of
the DB, mirroring `Atlas Settings.ssh_private_key_path`.

## Renewal

- **Manual:** **Issue / Renew Certificate** on `Root Domain` (creates/locates the
  cert and issues), **Issue/Renew** + **Push to Proxies** on `TLS Certificate`.
- **Scheduled:** a `daily` `scheduler_events` hook →
  `atlas.atlas.doctype.tls_certificate.tls_certificate.renew_expiring`: every
  `Active` cert whose `expires_on` is within 30 days is re-issued **and**
  re-pushed, then its status returns to `Active`. Mirrors the proxy reconcile
  philosophy — the desired state (a fresh cert on every proxy) is continuously
  restored. certbot is idempotent (`--keep-until-expiring` renews-or-skips), so a
  renewal that isn't due yet is a cheap no-op.

A push to one wedged proxy never blocks the others: `_push_to_proxies` logs the
failure and moves on, exactly like `proxy.reconcile_proxies`.

## First-run order

Layered on top of the proxy first-run ([12-proxy.md](./12-proxy.md)):

1. **DNS Settings** — either Route53 Settings (IAM key/secret with `route53:*` on
   the zone) or PowerDNS Settings (Authoritative HTTP API URL/key/server id).
2. **Atlas Settings** — `dns_provider_type = Route53` or `PowerDNS` (the active
   DNS vendor) + `tls_provider_type = Let's Encrypt` (the active issuer).
3. **Lets Encrypt Settings** — ACME directory (staging while testing) + account
   email. (ToS agreement is implicit: certbot is always run with `--agree-tos`.)
4. **Root Domain** — one row per region: `domain = <region>.frappe.dev`,
   `region`. The DNS + TLS vendor types are denormalized onto the row from the
   active vendors at insert. Click **Issue / Renew Certificate**.

After issuance the regional wildcard is on every proxy VM in the fleet and nginx
has reloaded; the proxy now serves `https://*.<region>.frappe.dev` with a real
cert.

> The DocType name is **"Lets Encrypt Settings"** (no apostrophe): Frappe scrubs a
> DocType name into a Python module path, and `Let's Encrypt Settings` scrubs to
> `let's_encrypt_settings` — an apostrophe in a module path is unimportable. The
> `Atlas Settings.tls_provider_type` Select value keeps the apostrophe
> (`Let's Encrypt`) since that is data, not a module.

## Verification

The split follows the project's host-facts-vs-unit-logic rule
([README.md § Testing](./README.md#testing)):

- **Unit (no host, the bulk of coverage):** the registries resolve a vendor type
  to its class and reject an unknown type (`for_dns_provider_type` /
  `for_tls_provider_type`, twins of
  `providers/test_registry.py`); DNS provider `credential_env()` /
  `certbot_authenticator()` / `certbot_args()` and the `LetsEncryptProvider` certbot argv compose
  correctly against a mocked local runner (**no real certbot**); `Root Domain`
  autoname/immutability and `*.<domain>` derivation; the `TLS Certificate` status
  machine, `renew_expiring` window, and `_push_to_proxies` fan-out to the proxy
  fleet (mock `push_cert`); `scripts/lib/atlas/certs.py` argv +
  `ATLAS_RESULT` parse. Run: `bench --site atlas.tests.local run-tests --app atlas`.
- **E2E (host fact, real ACME):** `tls_issuance` is the only e2e that drives the
  real producer chain — Let's Encrypt **staging** → DNS-01 → certbot →
  `_push_to_proxies` → off-droplet HTTPS — on top of the proxy infra. It needs a
  live DNS zone and the controller-host deps, and skips cleanly
  (`MissingConfig`, before any billable provision) when the e2e fixture has no
  `tls` block (`$ATLAS_E2E_CONFIG`, see the README). `proxy_vm` uses a self-signed stand-in cert, not this
  chain. The new desk buttons (Issue/Renew, Push to Proxies, Test Connection on
  Route53 Settings / PowerDNS Settings / Lets Encrypt Settings) are exercised through the HTTP layer
  in `desk_buttons`.
