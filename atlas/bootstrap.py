"""Bootstrap a fresh Atlas site end-to-end.

Run with:

    bench --site <site> execute atlas.bootstrap.run

This sets up `Atlas Settings`, vendor-specific Settings, the active
`Provider` row, the size/image catalog rows, provisions a Server,
registers the default Virtual Machine Image, syncs it to the server,
and provisions one Virtual Machine. All inputs come from the site
config so the script takes no arguments.

A `bench worker` must be running — `provision_server` and `sync_to_server`
both enqueue background jobs that this script waits on.

Site config keys (set with `bench --site <site> set-config -p <key> <value>`):

    atlas_provider_type           "DigitalOcean", "Scaleway", or "Self-Managed"
    atlas_ssh_private_key_path    absolute path to the SSH private key on disk
                                  (0600, readable by the Frappe user)
    atlas_ssh_key_id              vendor's handle for the uploaded SSH key.
                                  DigitalOcean: required — accepts DO's numeric key
                                  id or its SHA-256 fingerprint. Scaleway: optional —
                                  the IAM SSH-key UUID; if unset the provider
                                  registers atlas_ssh_public_key with IAM at
                                  provision time. Self-Managed: unused.
    atlas_ssh_public_key          optional OpenSSH public key body. If omitted it
                                  is DERIVED from atlas_ssh_private_key_path
                                  (ssh-keygen -y) — self-serve needs it set on
                                  Atlas Settings because the Site clone path reads
                                  it, so the derivation makes a key-path-only
                                  bootstrap stand up signup without extra config.

DigitalOcean providers also need:

    atlas_do_token                DO personal access token
    atlas_do_region               e.g. "blr1"
    atlas_do_default_size         vendor-native slug, e.g. "s-2vcpu-4gb-intel"
                                  (Atlas prefixes "DigitalOcean/" internally)
    atlas_do_default_image        vendor-native slug, e.g. "ubuntu-24-04-x64"

Scaleway providers also need (Elastic Metal bare metal — discover() is
load-bearing here: it is the only source of the per-zone offer_id / os_id UUIDs
provision() resolves, so a Scaleway bootstrap fails loud if discover() fails,
unlike DO's best-effort catalog refresh):

    atlas_scw_secret_key          Scaleway IAM API key Secret Key (the
                                  X-Auth-Token value — NOT the Access Key)
    atlas_scw_project_id          Project UUID every resource is scoped to
    atlas_scw_zone                one Elastic Metal zone, e.g. "fr-par-2"
    atlas_scw_default_size        vendor-native offer name, case-sensitive,
                                  e.g. "EM-A610R-NVME" (Atlas prefixes
                                  "Scaleway/" internally)
    atlas_scw_default_image       vendor-native OS slug, case-sensitive,
                                  e.g. "Ubuntu_24.04" (Atlas prefixes
                                  "Scaleway/" internally)
    atlas_scw_organization_id     optional — filters the project lookup and
                                  labels the authenticate result
    atlas_scw_billing             optional — "hourly" (default) or "monthly"

Self-Managed providers also need:

    atlas_self_managed_ipv4                  the host's IPv4 (SSH endpoint)
    atlas_self_managed_ipv6                  the host's IPv6
    atlas_self_managed_ipv6_prefix           the prefix routed to the host
    atlas_self_managed_ipv6_vm_range         the subnet Atlas allocates VM IPs from

Optional VM inputs:

    atlas_vm_ssh_public_key       PEM contents or path to a public key
                                  (defaults to ~/.ssh/id_ed25519.pub)

Optional TLS tail (run via `atlas.bootstrap.run_with_proxy`):

`run()` stops at the first VM (compute only). `run_with_proxy()` runs `run()` and
then, IF the TLS config keys below are all present, seeds the domain + TLS layer
(Domain Provider, Route53 Settings, TLS Provider, Lets Encrypt Settings, Root
Domain) and issues the regional wildcard via Let's Encrypt over Route 53 DNS-01 —
the same chain the desk's **Issue / Renew Certificate** button drives. The cert is
pushed to every proxy VM in the region (none yet at bootstrap, so the push is a
no-op until a proxy exists). Requires certbot + certbot-dns-route53 + openssl +
boto3 on the controller (spec/13-tls.md). If the keys are absent the tail is
skipped with a printed note — `run_with_proxy` then behaves like `run`.

    atlas_tls_domain                 the wildcard zone, e.g. blr1.frappe.dev
                                     (its Route 53 hosted zone must already exist)
    atlas_tls_region                 region the wildcard fronts (default: the DO region)
    atlas_route53_access_key_id      IAM key with route53:* on the zone
    atlas_route53_secret_access_key  …its secret
    atlas_route53_region             AWS API region (default us-east-1)
    atlas_acme_account_email         ACME registration / expiry-notice email
    atlas_acme_directory_url         ACME directory (default: LE STAGING — set the
                                     production URL for a trusted cert)

Self-serve, one shot (run via `atlas.bootstrap.run_self_serve`):

`run_self_serve()` stands up the WHOLE signup flow on a fresh site in one call —
compute + golden image + proxy + TLS + email — and leaves it running so `/signup`
works end to end (spec/14-self-serve.md). It is the durable sibling of the
`self_serve_site` e2e: it drives the same proven controller APIs
(`bench_image.build_bench`, `proxy.build_proxy`, `Root Domain.issue_certificate`,
`reserved_ip.allocate`/`attach`) but with NO teardown — the server, the proxy VM
(with a reserved IPv4 + the pushed wildcard cert), and the golden snapshot all
persist. The flow, in dependency order:

  1. `run()`                      — settings → provider → server → base images → a VM
  2. `bake_golden_image(server)`  — build bench in a guest, snapshot it, wire
                                    `Atlas Settings.default_bench_snapshot` (the
                                    image self-serve site VMs clone from)
  3. `ensure_tls_layer(config)`   — seed Domain/TLS providers + Root Domain (the
                                    row `Site.before_insert` reads region+FQDN from)
  4. `ensure_proxy(server, …)`    — proxy VM → `build_proxy` → reserved IPv4
  5. issue + push the wildcard    — `issue_certificate` then `push_to_proxies`
                                    (needs the proxy + reserved IP to exist first,
                                    which is why it runs AFTER step 4, not in
                                    `run_with_proxy`'s pre-proxy order)
  6. `ensure_outbound_email()`    — the SMTP account the verification mail sends from

It is billable: one droplet + a build VM + a proxy VM + one DO reserved IPv4, all
left running. It needs the TLS config keys (`atlas_tls_*`), certbot + boto3 on the
controller, and the DO credentials — the same prerequisites as the e2e. Run it on
the operator's turn:

    bench --site <site> execute atlas.bootstrap.run_self_serve

The older `run_with_self_serve()` is kept as the *settings-only* tail (wire an
already-baked snapshot + email, skip the billable bake/proxy) for the case where
the golden image + proxy already exist; `run_self_serve()` is the from-scratch
one-shot the wiped-site bootstrap uses.

    atlas_default_bench_snapshot     golden bench Virtual Machine Snapshot name. If
                                     set + Available, `run_self_serve` reuses it and
                                     SKIPS the bake; else it bakes a fresh one. (The
                                     settings-only `run_with_self_serve` adopts the
                                     newest Available golden-bench* if this is unset.)
    atlas_smtp_host                  outbound SMTP server (omit to skip email setup)
    atlas_smtp_port                  SMTP port (default 587)
    atlas_smtp_login                 SMTP username
    atlas_smtp_password              SMTP password
    atlas_smtp_from                  From address (default: the SMTP login)
"""

import os
import time

import frappe
import frappe.utils.password

PROVIDER_NAME = "bootstrap-provider"
IMAGE_NAME = "ubuntu-24.04"
MINIMAL_IMAGE_NAME = "ubuntu-24.04-minimal"

DOMAIN_PROVIDER_NAME = "bootstrap-route53"
TLS_PROVIDER_NAME = "bootstrap-letsencrypt"

# Golden bench build VM sizing — a Frappe clone + uv venv + node deps overflow the
# 4 GB base image, so the build VM (and therefore the snapshot, and every site VM
# cloned from it) gets a roomier disk + RAM. Mirrors bench_image.GOLDEN_DISK_GB /
# GOLDEN_MEMORY_MB (the e2e bake) — keep the two in sync.
GOLDEN_DISK_GB = 12
GOLDEN_MEMORY_MB = 2048
GOLDEN_SNAPSHOT_TITLE = "golden-bench"

# Proxy VM sizing. The proxy runs nginx+Lua only (no site DB), so it is small; it
# carries `is_proxy=1` + `region` so build_proxy and the cert push find it.
PROXY_MEMORY_MB = 1024
PROXY_DISK_GB = 4

# Let's Encrypt staging — no rate limits, untrusted cert. The TLS tail defaults
# here so an unattended bootstrap never burns LE production issuance quota; set
# atlas_acme_directory_url to the production URL for a trusted cert.
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"

# Ubuntu cloud images (noble), pinned to a dated release for immutability.
# The dated path never changes under us; the floating `release/` pointer does.
# `kernel_sha256` is the digest of the DOWNLOADED packed vmlinuz — sync-image.sh
# decompresses the zstd payload to a raw vmlinux on the server (the extracted
# kernel is a derived artifact, not separately pinned). See spec/08-images.md.
_NOBLE_RELEASE = "https://cloud-images.ubuntu.com/releases/noble/release-20260518"
_NOBLE_MINIMAL_RELEASE = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260521"

DEFAULT_IMAGE = {
	"image_name": IMAGE_NAME,
	"title": "Ubuntu 24.04 server cloud image",
	"kernel_url": f"{_NOBLE_RELEASE}/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-server",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_RELEASE}/ubuntu-24.04-server-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-server.ext4",
	"rootfs_sha256": "bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74",
	"default_disk_gigabytes": 4,
}

# The minimal flavor lives under a different upstream tree and ships the same
# generic kernel as server (identical digest). Seeded as a second image row so
# operators can pick the smaller rootfs.
MINIMAL_IMAGE = {
	"image_name": MINIMAL_IMAGE_NAME,
	"title": "Ubuntu 24.04 minimal cloud image",
	"kernel_url": f"{_NOBLE_MINIMAL_RELEASE}/unpacked/ubuntu-24.04-minimal-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-minimal",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_MINIMAL_RELEASE}/ubuntu-24.04-minimal-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-minimal.ext4",
	"rootfs_sha256": "a288f0bd499e1a747f86fda8ec9822dd99a4e3c0721d89ffd9dd57608ff21072",
	"default_disk_gigabytes": 4,
}


def run() -> None:
	"""End-to-end: settings → provider → server → image → virtual machine."""
	server_name = run_compute()
	provision_virtual_machine(server_name)


def run_compute(reuse_server: bool = True) -> str:
	"""Settings → provider → server → base images synced. Returns the Server name.

	The compute infrastructure WITHOUT the smoke VM — the shared prefix of `run()`
	and `run_self_serve()`. `run()` adds a throwaway smoke VM on top (the original
	compute-works proof); `run_self_serve()` skips it (the golden build VM + proxy VM
	already prove provisioning, and a durable bootstrap shouldn't strand an unused
	billable VM).

	`reuse_server` (default) adopts an existing Active Server instead of provisioning
	a fresh one, so a re-run after a mid-bootstrap failure doesn't strand a second
	billable droplet. Pass False to force a brand-new server."""
	provider = ensure_provider()
	server_name = _existing_active_server() if reuse_server else None
	if server_name:
		print(f"[bootstrap] reusing existing Active Server {server_name!r}")
	else:
		server_name = provision_server(provider)
		wait_for_active_server(server_name, timeout_seconds=_active_timeout(provider))
	ensure_image()
	sync_image(server_name)
	return server_name


def _existing_active_server() -> str | None:
	"""The newest Active Server, or None. Adopted by `run_compute(reuse_server=True)`
	so a re-run continues on the server a prior run already stood up."""
	rows = frappe.get_all(
		"Server", filters={"status": "Active"}, pluck="name", order_by="creation desc", limit=1
	)
	return rows[0] if rows else None


def run_with_proxy() -> None:
	"""`run()` plus the TLS tail: seed the domain + TLS layer and issue the regional
	wildcard cert (see the module docstring for the config keys).

	The compute bootstrap (`run`) always happens. The TLS tail runs only when the
	`atlas_tls_domain` + Route 53 + ACME keys are all present; otherwise it prints a
	note and returns — so this is a safe drop-in for `run` on any site."""
	run()
	tls_config = _read_tls_config()
	if tls_config is None:
		print(
			"[bootstrap] no TLS config (atlas_tls_domain etc.) — skipping the TLS tail. "
			"Compute bootstrap is complete."
		)
		return
	ensure_tls_layer(tls_config)
	issue_certificate(tls_config["domain"])


def run_self_serve(force_bake: bool = False) -> None:
	"""From-scratch one-shot: stand up the ENTIRE signup flow and leave it running.

	The durable sibling of the `self_serve_site` e2e (it drives the same proven
	controller APIs but tears nothing down). Dependency-ordered so each step's
	prerequisite already exists when it runs — in particular the cert issue+push is
	deferred until AFTER the proxy VM + its reserved IP exist (unlike
	`run_with_proxy`, which issues the cert pre-proxy when the push is a no-op).

	`force_bake=True` re-bakes the golden image even if an Available one is already
	configured (proves the from-scratch `build.sh` bake); the default reuses a
	configured Available snapshot and skips the slow bake.

	Billable, leaves infra up. Requires the TLS config keys + certbot/boto3 (it
	throws via `_read_tls_config` / the bake / the cert if a prerequisite is
	missing, surfacing the gap before stranding half-built infra)."""
	tls_config = _read_tls_config()
	if tls_config is None:
		frappe.throw(
			"run_self_serve needs the TLS config (atlas_tls_domain + Route53 + ACME keys) — "
			"the signup flow routes through the regional wildcard. Set them, or use run() for "
			"compute-only bootstrap."
		)

	# 1. Compute: settings → provider → server → base images (no smoke VM — the
	#    golden build VM + proxy VM below prove provisioning).
	server_name = run_compute()

	# 2. Golden bench image — the snapshot site VMs clone from. Wires
	#    Atlas Settings.default_bench_snapshot.
	bake_golden_image(server_name, force=force_bake)

	# 3. TLS layer rows (Root Domain etc.) BEFORE the proxy: Site.before_insert and
	#    build_proxy/cert-push all read the region off the Root Domain.
	ensure_tls_layer(tls_config)

	# 4. Proxy VM: build the stack + attach a reserved IPv4 (the public v4 front door).
	proxy_vm_name = ensure_proxy(server_name, tls_config["region"], tls_config["domain"])

	# 5. Issue the regional wildcard + push it to the proxy (cert + wildcard DNS).
	#    Now that the proxy + reserved IP exist, the push actually lands.
	issue_certificate(tls_config["domain"])
	push_certificate_to_proxies(tls_config["domain"])

	# 6. Outbound email so the verification mail sends (skips with a note if unset).
	ensure_outbound_email()

	_print_self_serve_summary(server_name, proxy_vm_name, tls_config["domain"])


def restore_credentials() -> None:
	"""Re-write the credential fields the unit suite clobbers, from site config.

	The shared dev DB is also the test DB: a unit run leaves fake values in the
	Singles (`set_atlas_settings`/`set_digitalocean_settings` write `dop_v1_fake`,
	`atlas-test-ssh-key.pem`, `key-id-123`), so the next *real* provision/build/e2e
	fails with a bogus token or an unusable key (memory: real-provision-traps #4).
	This restores the real values from `common_site_config.json` —
	`atlas_ssh_private_key_path`, optional `atlas_ssh_key_id` / `atlas_ssh_public_key`,
	and the active provider's secret — without `ensure_provider`'s catalog
	discover() network call. Which provider secret is restored is driven by
	`atlas_provider_type` (DigitalOcean → `atlas_do_token`; Scaleway →
	`atlas_scw_secret_key`; Self-Managed → none). Run it before any host turn:

	    bench --site <site> execute atlas.bootstrap.restore_credentials

	Idempotent; safe to re-run. Fails loud (`require_config`) if a required key is
	missing, since a half-restored credential set is worse than a clean error."""
	provider_type = require_config("atlas_provider_type")
	# Store the EXPANDED absolute path. Config often holds `~/.ssh/id_rsa`; the
	# production reader (get_ssh_key_from_disk) expanduser()s it, but a stored raw
	# `~` is a trap for any path that reads the field literally and a confusing
	# "invalid format / file not found" if the tilde survives — expand once here so
	# the Single always holds a real absolute path that points at an existing key.
	key_path = os.path.expanduser(require_config("atlas_ssh_private_key_path"))
	if not os.path.isfile(key_path):
		frappe.throw(f"atlas_ssh_private_key_path expands to {key_path!r}, which is not a file")
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_private_key_path",
		key_path,
		update_modified=False,
	)
	# ssh_key_id is required for DO (its key handle) but optional for Scaleway (the
	# provider self-registers the public key with IAM if unset) and unused for
	# Self-Managed — so only require it for DO, restore it best-effort otherwise.
	if provider_type == "DigitalOcean":
		ssh_key_id = require_config("atlas_ssh_key_id")
	else:
		ssh_key_id = frappe.conf.get("atlas_ssh_key_id")
	if ssh_key_id:
		frappe.db.set_single_value("Atlas Settings", "ssh_key_id", ssh_key_id, update_modified=False)
	public_key = _resolve_fleet_public_key()
	if public_key:
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", public_key, update_modified=False)
	if provider_type == "DigitalOcean":
		frappe.utils.password.set_encrypted_password(
			"DigitalOcean Settings",
			"DigitalOcean Settings",
			require_config("atlas_do_token"),
			"api_token",
		)
	elif provider_type == "Scaleway":
		frappe.utils.password.set_encrypted_password(
			"Scaleway Settings",
			"Scaleway Settings",
			require_config("atlas_scw_secret_key"),
			"secret_key",
		)
	frappe.db.commit()
	print(f"[bootstrap] restored Atlas/{provider_type} credentials from site config")


def ensure_provider() -> "frappe.model.document.Document":
	provider_type = require_config("atlas_provider_type")
	if provider_type not in ("DigitalOcean", "Scaleway", "Self-Managed"):
		frappe.throw(
			f"atlas_provider_type must be DigitalOcean, Scaleway or Self-Managed, got {provider_type!r}"
		)

	# Ensure the Provider row exists, then write the Singles.
	if not frappe.db.exists("Provider", PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Provider",
				"provider_name": PROVIDER_NAME,
				"provider_type": provider_type,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		print(f"[bootstrap] created Provider {PROVIDER_NAME!r} ({provider_type})")
	else:
		print(f"[bootstrap] reusing Provider {PROVIDER_NAME!r}")

	# Atlas Settings — provider link + SSH triplet.
	frappe.db.set_single_value("Atlas Settings", "provider", PROVIDER_NAME, update_modified=False)
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_private_key_path",
		require_config("atlas_ssh_private_key_path"),
		update_modified=False,
	)
	if provider_type == "DigitalOcean":
		frappe.db.set_single_value(
			"Atlas Settings",
			"ssh_key_id",
			require_config("atlas_ssh_key_id"),
			update_modified=False,
		)
	public_key = _resolve_fleet_public_key()
	if public_key:
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", public_key, update_modified=False)

	if provider_type == "DigitalOcean":
		region = require_config("atlas_do_region")
		size_slug = require_config("atlas_do_default_size")
		image_slug = require_config("atlas_do_default_image")

		# Seed the catalog rows the Settings will Link to.
		_ensure_provider_size(provider_type, size_slug)
		_ensure_provider_image(provider_type, image_slug)

		frappe.db.set_single_value("DigitalOcean Settings", "region", region, update_modified=False)
		frappe.db.set_single_value(
			"DigitalOcean Settings",
			"default_size",
			f"DigitalOcean/{size_slug}",
			update_modified=False,
		)
		frappe.db.set_single_value(
			"DigitalOcean Settings",
			"default_image",
			f"DigitalOcean/{image_slug}",
			update_modified=False,
		)
		frappe.utils.password.set_encrypted_password(
			"DigitalOcean Settings",
			"DigitalOcean Settings",
			require_config("atlas_do_token"),
			"api_token",
		)

		# Seed the wider catalog so the Refresh Catalog button is exercising
		# real data, not just the slugs the operator named in site config.
		from atlas.atlas.doctype.provider.provider import upsert_catalog
		from atlas.atlas.providers.digitalocean import DigitalOceanProvider

		try:
			capabilities = DigitalOceanProvider().discover()
			upsert_catalog(provider_type, capabilities)
		except Exception as exception:
			print(f"[bootstrap] WARN: catalog discover() failed: {exception}")

	elif provider_type == "Scaleway":
		_seed_scaleway_settings()

	frappe.db.commit()
	return frappe.get_doc("Provider", PROVIDER_NAME)


def _seed_scaleway_settings() -> None:
	"""Seed `Scaleway Settings` + the catalog for a Scaleway (Elastic Metal)
	bootstrap, mirroring the e2e `ensure_scaleway_provider` seed.

	Unlike DO — whose `discover()` is best-effort gravy on top of the named
	slugs — Scaleway's `discover()` is LOAD-BEARING: it is the only source of the
	per-zone `offer_id` / `os_id` UUIDs that `provision()` reads back out of each
	catalog row's `provider_metadata` (the create/install calls take UUIDs, not
	slugs). So we discover BEFORE wiring the defaults, fail loud if it fails, and
	then verify the named default size/image rows actually exist in the freshly
	upserted catalog (a typo'd slug or wrong casing — e.g. EM-A610R-NVME vs
	-NVMe — is an operator mistake worth surfacing now, not at provision time).

	The IAM SSH key is uploaded at provision time, so unlike DO there is no
	`ssh_key_id` to seed here: the provider registers `Atlas Settings.ssh_public_key`
	with IAM if `ssh_key_id` is unset (ensure_provider derives the public key from
	the private key path). An operator who already has a cached IAM key UUID can set
	`atlas_ssh_key_id` to reuse it."""
	import frappe.utils.password

	zone = require_config("atlas_scw_zone")
	project_id = require_config("atlas_scw_project_id")
	size_slug = require_config("atlas_scw_default_size")
	image_slug = require_config("atlas_scw_default_image")

	frappe.db.set_single_value("Scaleway Settings", "zone", zone, update_modified=False)
	frappe.db.set_single_value("Scaleway Settings", "project_id", project_id, update_modified=False)
	organization_id = frappe.conf.get("atlas_scw_organization_id")
	if organization_id:
		frappe.db.set_single_value(
			"Scaleway Settings", "organization_id", organization_id, update_modified=False
		)
	frappe.db.set_single_value(
		"Scaleway Settings",
		"billing",
		frappe.conf.get("atlas_scw_billing") or "hourly",
		update_modified=False,
	)
	frappe.utils.password.set_encrypted_password(
		"Scaleway Settings", "Scaleway Settings", require_config("atlas_scw_secret_key"), "secret_key"
	)
	# default_size/default_image are reqd Links — set them only after discover()
	# upserts the rows below, so the Link target exists when the Single saves.
	if frappe.conf.get("atlas_ssh_key_id"):
		frappe.db.set_single_value(
			"Atlas Settings", "ssh_key_id", frappe.conf.get("atlas_ssh_key_id"), update_modified=False
		)
	frappe.db.commit()

	# Discover the live per-zone catalog (the offer_id / os_id UUIDs). Load-bearing —
	# let the exception propagate so a bad key/zone fails the bootstrap loudly here
	# rather than at the first opaque provision().
	from atlas.atlas.doctype.provider.provider import upsert_catalog
	from atlas.atlas.providers.scaleway import ScalewayProvider

	capabilities = ScalewayProvider().discover()
	upsert_catalog("Scaleway", capabilities)
	frappe.db.commit()

	size_name = f"Scaleway/{size_slug}"
	image_name = f"Scaleway/{image_slug}"
	if not frappe.db.exists("Provider Size", size_name):
		frappe.throw(
			f"Provider Size {size_name!r} not in the discovered catalog — check atlas_scw_default_size "
			f"against the live zone offers (casing matters, e.g. EM-A610R-NVME)."
		)
	if not frappe.db.exists("Provider Image", image_name):
		frappe.throw(
			f"Provider Image {image_name!r} not in the discovered catalog — check atlas_scw_default_image "
			f"against the live zone OS list (casing matters, e.g. Ubuntu_24.04)."
		)
	frappe.db.set_single_value("Scaleway Settings", "default_size", size_name, update_modified=False)
	frappe.db.set_single_value("Scaleway Settings", "default_image", image_name, update_modified=False)
	frappe.db.commit()
	print(f"[bootstrap] seeded Scaleway Settings (zone={zone}, size={size_name}, image={image_name})")


def _ensure_provider_size(provider_type: str, slug: str) -> None:
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Size", name):
		return
	import json

	frappe.get_doc(
		{
			"doctype": "Provider Size",
			"provider_type": provider_type,
			"slug": slug,
			"enabled": 1,
			"provider_metadata": json.dumps({}),
		}
	).insert(ignore_permissions=True)


def _ensure_provider_image(provider_type: str, slug: str) -> None:
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Image", name):
		return
	import json

	frappe.get_doc(
		{
			"doctype": "Provider Image",
			"provider_type": provider_type,
			"slug": slug,
			"enabled": 1,
			"provider_metadata": json.dumps({}),
		}
	).insert(ignore_permissions=True)


def provision_server(provider: "frappe.model.document.Document") -> str:
	title = f"bootstrap-server-{int(time.time())}"
	if provider.provider_type == "Self-Managed":
		server_name = provider.provision_server(
			title,
			ipv4_address=require_config("atlas_self_managed_ipv4"),
			ipv6_address=require_config("atlas_self_managed_ipv6"),
			ipv6_prefix=require_config("atlas_self_managed_ipv6_prefix"),
			ipv6_virtual_machine_range=require_config("atlas_self_managed_ipv6_vm_range"),
		)
	else:
		# DigitalOcean + Scaleway: the vendor reads its own default size/image off
		# its Settings Single, so the call needs only the title. (Scaleway's create
		# is async — the Server lands Pending and the worker polls describe() to
		# Active, which wait_for_active_server already handles via its longer timeout.)
		server_name = provider.provision_server(title)
	frappe.db.commit()
	print(f"[bootstrap] provisioning Server {title!r} (name={server_name!r}; background job enqueued)")
	return server_name


def _active_timeout(provider: "frappe.model.document.Document") -> int:
	"""How long to wait for a freshly-provisioned Server to reach Active.

	A DO droplet is up in seconds; a Scaleway Elastic Metal box is a real bare-metal
	OS install that takes minutes (up to ~1h worst case). So derive the wait from the
	provider implementation's own `ready_timeout_seconds` (DO=600, Scaleway=3600) —
	the same source the worker's describe()-poll uses — plus headroom for the
	host-side bootstrap-server.py Task that runs AFTER the vendor reports ready.
	Floored at the historical 900s default so DO is never waited on for LESS than
	before."""
	from atlas.atlas import providers

	impl = providers.for_provider(provider.name)
	vendor_ready = getattr(impl, "ready_timeout_seconds", 600)
	return max(900, vendor_ready + 600)


def wait_for_active_server(server_name: str, timeout_seconds: int = 900) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Server", server_name, "status")
		print(f"[bootstrap] Server {server_name!r} status = {status}")
		if status == "Active":
			return
		if status == "Broken":
			frappe.throw(f"Server {server_name} ended in status Broken — check the Task list")
		time.sleep(10)
	frappe.throw(f"Server {server_name} did not become Active within {timeout_seconds}s")


def ensure_image() -> "frappe.model.document.Document":
	if frappe.db.exists("Virtual Machine Image", IMAGE_NAME):
		print(f"[bootstrap] reusing Virtual Machine Image {IMAGE_NAME!r}")
		return frappe.get_doc("Virtual Machine Image", IMAGE_NAME)
	image = frappe.get_doc({"doctype": "Virtual Machine Image", **DEFAULT_IMAGE, "is_active": 1}).insert(
		ignore_permissions=True
	)
	frappe.db.commit()
	print(f"[bootstrap] created Virtual Machine Image {image.name!r}")
	return image


def sync_image(server_name: str, timeout_seconds: int = 900) -> None:
	"""Sync IMAGE_NAME to `server_name` and wait for it. Race-free + idempotent.

	`Virtual Machine Image.after_insert` ALREADY enqueues a sync to every Active
	server (so `ensure_image()` on a fresh row fires one before this is even called).
	Enqueuing a *second* sync here makes two workers race on the same image dir —
	the loser fails `sha256sum -c` because the winner already renamed the kernel
	`.part` to its final name. So: adopt an existing in-flight / recent-success sync
	Task for this image+server if there is one, and only enqueue a fresh sync when
	none exists (the re-run case where the image row already existed, so
	`after_insert` did not fire). Either way, wait on the one tracked Task."""
	task_name = _latest_sync_task(server_name)
	if task_name:
		status = frappe.db.get_value("Task", task_name, "status")
		print(f"[bootstrap] adopting existing sync Task {task_name!r} (status {status}) for {server_name!r}")
	else:
		image = frappe.get_doc("Virtual Machine Image", IMAGE_NAME)
		task_name = image.sync_to_server(server_name)
		print(f"[bootstrap] syncing image to {server_name!r} (Task {task_name!r})")
	wait_for_task(task_name, timeout_seconds)


def _latest_sync_task(server_name: str) -> str | None:
	"""The most recent sync-image.py Task for IMAGE_NAME on `server_name` that is
	still in flight (Pending/Running) or already Succeeded — i.e. one whose result
	`sync_image` can wait on / adopt. A prior Failure is NOT adopted (re-sync it).
	Matching is by the IMAGE_NAME variable so a multi-image server isn't confused."""
	rows = frappe.get_all(
		"Task",
		filters={"server": server_name, "script": "sync-image.py"},
		fields=["name", "status", "variables"],
		order_by="creation desc",
		limit=20,
	)
	for row in rows:
		if row.status not in ("Pending", "Running", "Success"):
			continue
		variables = row.variables or ""
		if f'"IMAGE_NAME": "{IMAGE_NAME}"' in variables or f"'IMAGE_NAME': '{IMAGE_NAME}'" in variables:
			return row.name
	return None


def provision_virtual_machine(server_name: str) -> str:
	virtual_machine = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "bootstrap test vm",
			"server": server_name,
			"image": IMAGE_NAME,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": load_vm_ssh_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	print(f"[bootstrap] created Virtual Machine {virtual_machine.name!r}")
	task_name = _wait_for_provision_task(virtual_machine.name)
	print(f"[bootstrap] provisioning Virtual Machine (Task {task_name!r})")
	wait_for_task(task_name, timeout_seconds=300)
	return virtual_machine.name


def _wait_for_provision_task(virtual_machine_name: str, timeout_seconds: int = 60) -> str:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		rows = frappe.get_all(
			"Task",
			filters={
				"virtual_machine": virtual_machine_name,
				"script": "provision-vm.py",
			},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)
		if rows:
			return rows[0]
		time.sleep(2)
	frappe.throw(f"No provision Task appeared for {virtual_machine_name!r} within {timeout_seconds}s")


def wait_for_task(task_name: str, timeout_seconds: int) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		task = frappe.get_doc("Task", task_name)
		if task.status in ("Success", "Failure"):
			break
		time.sleep(5)
	else:
		frappe.throw(f"Task {task_name} did not finish within {timeout_seconds}s")
	if task.status != "Success":
		frappe.throw(f"Task {task_name} ended in {task.status}: {(task.stderr or '')[:500]}")


# --- TLS tail ------------------------------------------------------------


def _read_tls_config() -> dict | None:
	"""Read the Route 53 + ACME inputs from site config, or None if the TLS tail
	wasn't requested. Returns None only when `atlas_tls_domain` is unset (the
	opt-in switch); if it's set but a companion key is missing we `throw`, because a
	half-configured TLS tail is an operator mistake, not an opt-out."""
	domain = frappe.conf.get("atlas_tls_domain")
	if not domain:
		return None
	return {
		"domain": domain,
		"region": frappe.conf.get("atlas_tls_region") or require_config("atlas_do_region"),
		"access_key_id": require_config("atlas_route53_access_key_id"),
		"secret_access_key": require_config("atlas_route53_secret_access_key"),
		"aws_region": frappe.conf.get("atlas_route53_region", "us-east-1"),
		"account_email": require_config("atlas_acme_account_email"),
		"acme_directory_url": frappe.conf.get("atlas_acme_directory_url", LETS_ENCRYPT_STAGING),
	}


def ensure_tls_layer(config: dict) -> None:
	"""Seed the domain + TLS layer from config, idempotently — the same rows the
	desk first-run order creates (spec/13-tls.md): Domain Provider, Route53
	Settings, TLS Provider, Lets Encrypt Settings, Root Domain."""
	import frappe.utils.password

	frappe.db.set_single_value(
		"Route53 Settings", "access_key_id", config["access_key_id"], update_modified=False
	)
	frappe.db.set_single_value("Route53 Settings", "region", config["aws_region"], update_modified=False)
	frappe.utils.password.set_encrypted_password(
		"Route53 Settings", "Route53 Settings", config["secret_access_key"], "secret_access_key"
	)
	frappe.db.set_single_value(
		"Lets Encrypt Settings", "acme_directory_url", config["acme_directory_url"], update_modified=False
	)
	frappe.db.set_single_value(
		"Lets Encrypt Settings", "account_email", config["account_email"], update_modified=False
	)
	frappe.db.set_single_value("Lets Encrypt Settings", "agree_tos", 1, update_modified=False)

	if not frappe.db.exists("Domain Provider", DOMAIN_PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "Domain Provider",
				"provider_name": DOMAIN_PROVIDER_NAME,
				"provider_type": "Route53",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		print(f"[bootstrap] created Domain Provider {DOMAIN_PROVIDER_NAME!r}")
	if not frappe.db.exists("TLS Provider", TLS_PROVIDER_NAME):
		frappe.get_doc(
			{
				"doctype": "TLS Provider",
				"provider_name": TLS_PROVIDER_NAME,
				"provider_type": "Let's Encrypt",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		print(f"[bootstrap] created TLS Provider {TLS_PROVIDER_NAME!r}")
	if not frappe.db.exists("Root Domain", config["domain"]):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": config["domain"],
				"region": config["region"],
				"domain_provider": DOMAIN_PROVIDER_NAME,
				"tls_provider": TLS_PROVIDER_NAME,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		print(f"[bootstrap] created Root Domain {config['domain']!r} (region {config['region']!r})")
	else:
		print(f"[bootstrap] reusing Root Domain {config['domain']!r}")
	frappe.db.commit()


def issue_certificate(domain: str) -> str:
	"""Click Issue / Renew Certificate on the Root Domain — issue the regional
	wildcard via certbot DNS-01 (the producer chain) and push to any proxy VMs in
	the region. Returns the TLS Certificate name."""
	print(f"[bootstrap] issuing *.{domain} via Let's Encrypt over Route 53 DNS-01 ...")
	cert_name = frappe.get_doc("Root Domain", domain).issue_certificate()
	frappe.db.commit()
	status, expires_on = frappe.db.get_value("TLS Certificate", cert_name, ["status", "expires_on"])
	if status != "Active":
		frappe.throw(f"TLS Certificate {cert_name} ended in status {status}, expected Active")
	print(f"[bootstrap] issued {cert_name} for *.{domain} (status {status}, expires {expires_on})")
	return cert_name


def push_certificate_to_proxies(domain: str) -> list[str]:
	"""Re-push the domain's Active wildcard cert to every proxy VM in its region and
	publish the wildcard DNS (A → reserved IPv4, AAAA → proxy /128).

	`issue_certificate` already pushes on issue, but in `run_self_serve` the cert is
	issued right after the proxy comes up; this explicit push is the belt-and-braces
	re-push (idempotent) that also (re)publishes the wildcard now the proxy's
	reserved IP is attached. Returns the proxy VM names pushed to."""
	cert_name = frappe.db.get_value("TLS Certificate", {"root_domain": domain, "status": "Active"}, "name")
	if not cert_name:
		frappe.throw(f"no Active TLS Certificate for {domain!r} to push — issue it first")
	pushed = frappe.get_doc("TLS Certificate", cert_name).push_to_proxies()
	frappe.db.commit()
	print(f"[bootstrap] pushed {cert_name} + published wildcard for *.{domain} to {pushed or '(no proxies)'}")
	return pushed


# --- golden bench image bake ---------------------------------------------


def bake_golden_image(server_name: str, force: bool = False) -> str:
	"""Bake the golden bench image on `server_name` and wire it as
	`Atlas Settings.default_bench_snapshot` (the image self-serve site VMs clone
	from). Returns the snapshot name.

	Reuse-or-bake: if `default_bench_snapshot` already points at an Available
	snapshot and `force` is False, reuse it (the slow apt+clone+uv+node bake is
	skipped). Otherwise provision a build VM, build bench inside it over guest-SSH
	(`bench_image.build_bench` — the proven controller path, robust to recycled IPs
	+ mid-build resets), stop it, and snapshot it. The build VM is left Stopped (it
	is e2e/bake scratch; terminate it once the snapshot is set if you want the RAM
	back — the snapshot is the durable artifact)."""
	from atlas.atlas import bench_image

	configured = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	if configured and not force:
		status = frappe.db.get_value("Virtual Machine Snapshot", configured, "status")
		if status == "Available":
			print(f"[bootstrap] reusing golden bench snapshot {configured} (Available); skipping bake")
			return configured
		print(f"[bootstrap] configured snapshot {configured} is {status!r}, not Available — re-baking")

	image_name = ensure_image().name
	sync_image(server_name)  # idempotent; ensures the base rootfs/kernel are on the server

	vm = _provision_durable_vm(
		server_name,
		title="golden bench — build",
		image=image_name,
		memory_megabytes=GOLDEN_MEMORY_MB,
		disk_gigabytes=GOLDEN_DISK_GB,
		vcpus=2,
	)
	print(
		f"[bootstrap] golden build VM {vm.name} Running (v6={vm.ipv6_address}); building bench in guest ..."
	)

	# Build bench-cli + `bench init` + the baked site.local inside the guest (slow:
	# apt + clone Frappe + uv venv + node). The detached-build + forget_host
	# machinery lives in build_bench (memory: real-provision-traps M-7).
	bench_image.build_bench(vm.name)
	print("[bootstrap] bench built in the guest; stopping + snapshotting ...")

	vm.stop()
	frappe.db.commit()
	_wait_for_vm_status(vm.name, "Stopped", timeout_seconds=180)
	vm.reload()
	snapshot_name = vm.snapshot(title=GOLDEN_SNAPSHOT_TITLE)
	frappe.db.commit()
	_wait_for_snapshot_available(snapshot_name, timeout_seconds=600)

	frappe.db.set_single_value(
		"Atlas Settings", "default_bench_snapshot", snapshot_name, update_modified=False
	)
	frappe.db.commit()
	print(f"[bootstrap] golden bench snapshot {snapshot_name} baked + wired as default_bench_snapshot")
	return snapshot_name


# --- proxy VM stand-up ----------------------------------------------------


def ensure_proxy(server_name: str, region: str, domain: str) -> str:
	"""Provision a proxy VM on `server_name`, build the nginx+Lua stack inside it,
	and attach a reserved IPv4 — the public front door subdomains route through.
	Returns the proxy VM name.

	Idempotent-ish: if a Running `is_proxy` VM already exists on this server in this
	region, reuse it (re-build + re-ensure a reserved IP) rather than provisioning a
	second. The cert push is a SEPARATE step (`push_certificate_to_proxies`) the
	caller runs after issuing, because the cert must exist first."""
	from atlas.atlas import proxy

	existing = frappe.get_all(
		"Virtual Machine",
		filters={"server": server_name, "is_proxy": 1, "region": region, "status": "Running"},
		pluck="name",
		limit=1,
	)
	if existing:
		proxy_vm_name = existing[0]
		print(f"[bootstrap] reusing existing proxy VM {proxy_vm_name} on {server_name}")
	else:
		image_name = ensure_image().name
		vm = _provision_durable_vm(
			server_name,
			title=f"proxy — {region}",
			image=image_name,
			memory_megabytes=PROXY_MEMORY_MB,
			disk_gigabytes=PROXY_DISK_GB,
			vcpus=1,
			is_proxy=True,
			region=region,
		)
		proxy_vm_name = vm.name
		print(
			f"[bootstrap] proxy VM {proxy_vm_name} Running (v6={vm.ipv6_address}); building proxy stack ..."
		)

	# Build the proxy stack in the guest (compiles nginx+Lua; detached + forget_host
	# handled inside build_proxy). Idempotent — build.sh re-runs cleanly.
	proxy.build_proxy(proxy_vm_name)
	print(f"[bootstrap] proxy stack built on {proxy_vm_name}; ensuring a reserved IPv4 ...")

	_ensure_reserved_ipv4(server_name, proxy_vm_name)
	return proxy_vm_name


def _ensure_reserved_ipv4(server_name: str, vm_name: str) -> str:
	"""Allocate a DO reserved IPv4 for the server and attach it to the proxy VM
	(vendor assign + host 1:1-NAT). Returns the Reserved IP row name. If the VM
	already has one attached, reuse it (a re-run must not allocate a second
	billable IP)."""
	from atlas.atlas.doctype.reserved_ip import reserved_ip as reserved_ip_module

	attached = frappe.get_all("Reserved IP", filters={"virtual_machine": vm_name}, pluck="name", limit=1)
	if attached:
		ipv4 = frappe.db.get_value("Reserved IP", attached[0], "ip_address")
		print(f"[bootstrap] proxy {vm_name} already has reserved IPv4 {ipv4} ({attached[0]})")
		return attached[0]

	reserved = reserved_ip_module.allocate(server_name)
	frappe.db.commit()
	frappe.get_doc("Reserved IP", reserved).attach(vm_name)
	frappe.db.commit()
	ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")
	print(f"[bootstrap] reserved IPv4 {ipv4} attached to proxy {vm_name} ({reserved})")
	return reserved


# --- durable VM / snapshot helpers (no e2e/test imports) ------------------


def _provision_durable_vm(
	server_name: str,
	title: str,
	image: str,
	memory_megabytes: int,
	disk_gigabytes: int,
	vcpus: int = 1,
	is_proxy: bool = False,
	region: str | None = None,
) -> "frappe.model.document.Document":
	"""Insert a Virtual Machine with the FLEET key (Atlas Settings.ssh_public_key) so
	the control plane (`connection_for_guest`) can SSH in, commit so the
	`after_insert` boot job runs on the worker, and wait for Running.

	Uses the fleet key (not an ephemeral e2e key) because in a durable bootstrap the
	only SSH consumer is the control plane — same key `Site._provision_backing_vm`
	clones site VMs with. Requires a running worker (the boot is a background job)."""
	public_key = frappe.db.get_single_value("Atlas Settings", "ssh_public_key")
	if not public_key:
		frappe.throw(
			"Atlas Settings.ssh_public_key is unset — a build/proxy VM needs the fleet key in "
			"authorized_keys for the control plane to SSH in. Run ensure_provider / restore_credentials "
			"first (they derive it from the private key)."
		)
	fields = {
		"doctype": "Virtual Machine",
		"title": title,
		"server": server_name,
		"image": image,
		"vcpus": vcpus,
		"memory_megabytes": memory_megabytes,
		"disk_gigabytes": disk_gigabytes,
		"ssh_public_key": public_key,
	}
	if is_proxy:
		fields["is_proxy"] = 1
		fields["region"] = region
	vm = frappe.get_doc(fields).insert(ignore_permissions=True)
	frappe.db.commit()
	print(f"[bootstrap] inserted VM {vm.name!r} ({title}); waiting for boot ...")
	_wait_for_vm_running(vm.name)
	vm.reload()
	if vm.status != "Running":
		frappe.throw(f"VM {vm.name} ended in status {vm.status}, expected Running")
	return vm


def _wait_for_vm_running(vm_name: str, timeout_seconds: int = 1500) -> None:
	"""Poll the VM's COMMITTED status to Running (rollback() each loop to read the
	worker's per-step writes). The boot is a separate background job, so this waits
	across a commit boundary, not inline — the proven shape. Long
	default: a cold dev box cloning + booting a 12 GB rootfs can take many minutes."""
	deadline = time.monotonic() + timeout_seconds
	last_status = None
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Virtual Machine", vm_name, "status")
		if status != last_status:
			elapsed = int(time.monotonic() - (deadline - timeout_seconds))
			print(f"[bootstrap] VM {vm_name} status={status!r} (t+{elapsed}s)")
			last_status = status
		if status == "Running":
			return
		if status in ("Broken", "Failed", "Terminated"):
			_dump_vm_tasks(vm_name)
			frappe.throw(f"VM {vm_name} reached {status} during provisioning — check the Task list")
		time.sleep(5)
	_dump_vm_tasks(vm_name)
	frappe.throw(
		f"VM {vm_name} did not reach Running within {timeout_seconds}s "
		"(is a worker running? on macOS it needs OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES)"
	)


def _wait_for_vm_status(vm_name: str, target: str, timeout_seconds: int = 180) -> None:
	"""Poll a VM to an arbitrary committed status (e.g. Stopped before snapshot)."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		if frappe.db.get_value("Virtual Machine", vm_name, "status") == target:
			return
		time.sleep(3)
	frappe.throw(f"VM {vm_name} did not reach {target} within {timeout_seconds}s")


def _wait_for_snapshot_available(snapshot_name: str, timeout_seconds: int = 600) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Virtual Machine Snapshot", snapshot_name, "status")
		if status == "Available":
			return
		if status == "Failed":
			frappe.throw(f"Snapshot {snapshot_name} reached Failed")
		time.sleep(3)
	frappe.throw(f"Snapshot {snapshot_name} not Available within {timeout_seconds}s")


def _dump_vm_tasks(vm_name: str) -> None:
	for task in frappe.get_all(
		"Task",
		filters={"virtual_machine": vm_name},
		fields=["name", "script", "status", "creation"],
		order_by="creation desc",
		limit=5,
	):
		print(f"[bootstrap]   task {task.name} script={task.script} status={task.status} ({task.creation})")


def _print_self_serve_summary(server_name: str, proxy_vm_name: str, domain: str) -> None:
	snapshot = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	reserved = frappe.get_all(
		"Reserved IP", filters={"virtual_machine": proxy_vm_name}, pluck="name", limit=1
	)
	reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved[0], "ip_address") if reserved else "(none)"
	proxy_ipv6 = frappe.db.get_value("Virtual Machine", proxy_vm_name, "ipv6_address")
	print("")
	print("=" * 64)
	print("SELF-SERVE SIGNUP FLOW STANDING — infra LEFT RUNNING (bills until torn down).")
	for label, value in (
		("server", server_name),
		("golden snapshot", snapshot),
		("proxy VM", proxy_vm_name),
		("proxy v6", proxy_ipv6),
		("reserved v4", reserved_ipv4),
		("wildcard", f"*.{domain}"),
	):
		print(f"  {label:<16} {value}")
	print("")
	print(f"  /signup now provisions sites at <sub>.{domain} (v4 + v6).")
	print("=" * 64)


def run_with_self_serve() -> None:
	"""`run_with_proxy()` plus the self-serve tail: wire the golden bench snapshot
	and outbound email so a fresh site can take a public `/signup`.

	The compute + proxy + TLS bootstrap (`run_with_proxy`) always happens first —
	it seeds the Root Domain that `Site.before_insert` resolves the region + FQDN
	suffix from (spec/14, Contract A), so self-serve has no separate domain step.
	Then two settings the signup flow needs:

	  - `Atlas Settings.default_bench_snapshot` — the golden image a Site's backing
	    VM clones from (spec/08-images.md). Wired from `atlas_default_bench_snapshot` if set,
	    else from the most recent Available `golden-bench*` snapshot if one exists.
	  - the outbound Email Account — so the verification email actually sends
	    (`request_site` only queues it; spec/14 calls outbound email an operator
	    prerequisite). Configured from the `atlas_smtp_*` keys.

	Each step skips with a printed note when its inputs are absent, so this is a
	safe drop-in for `run_with_proxy` — mirroring how the TLS tail degrades.

	What this does NOT do (deliberately — both are billable host runs): bake the
	golden bench snapshot, and provision the edge proxy VM. A fresh dev brings the
	signup flow up in three steps:
	  1. Bake the golden image once (leaves an Available golden-bench snapshot):
	       bench --site <site> execute atlas.tests.e2e.use_cases.bench_image.run_smoke
	  2. Run this (adopts that snapshot, seeds TLS + email):
	       bench --site <site> execute atlas.bootstrap.run_with_self_serve
	  3. Stand up a proxy VM in the region (so subdomains route + get TLS) — the
	     proxy_vm use case, or the desk flow in spec/12-proxy.md.
	Then `/signup` works end to end. A site VM needs ~2 GB RAM, so size the host
	for the number of concurrent sites you expect."""
	run_with_proxy()
	ensure_default_bench_snapshot()
	ensure_outbound_email()


def ensure_default_bench_snapshot() -> None:
	"""Point `Atlas Settings.default_bench_snapshot` at an Available golden bench
	snapshot. Prefers the explicitly configured `atlas_default_bench_snapshot`;
	otherwise adopts the newest Available snapshot whose title starts `golden-bench`
	(what the bake e2e leaves). Skips with a printed pointer if none exists — the
	bake is a billable host run (spec/08-images.md), not something to trigger from bootstrap."""
	configured = frappe.conf.get("atlas_default_bench_snapshot")
	if configured:
		status = frappe.db.get_value("Virtual Machine Snapshot", configured, "status")
		if status != "Available":
			frappe.throw(
				f"atlas_default_bench_snapshot {configured!r} is not an Available snapshot (status {status})"
			)
		frappe.db.set_single_value(
			"Atlas Settings", "default_bench_snapshot", configured, update_modified=False
		)
		frappe.db.commit()
		print(f"[bootstrap] default_bench_snapshot = {configured} (configured)")
		return

	candidates = frappe.get_all(
		"Virtual Machine Snapshot",
		filters={"status": "Available", "title": ("like", "golden-bench%")},
		fields=["name", "title"],
		order_by="creation desc",
		limit=1,
	)
	if not candidates:
		print(
			"[bootstrap] no golden bench snapshot found — self-serve signup will fail until one exists. "
			"Bake it (billable host run):\n"
			"    bench --site <site> execute atlas.tests.e2e.use_cases.bench_image.run_smoke\n"
			"  then re-run, or set atlas_default_bench_snapshot to its name."
		)
		return
	snapshot = candidates[0]["name"]
	frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", snapshot, update_modified=False)
	frappe.db.commit()
	print(f"[bootstrap] default_bench_snapshot = {snapshot} (adopted newest Available golden-bench)")


def ensure_outbound_email() -> None:
	"""Configure the default outbound Email Account from the `atlas_smtp_*` keys so
	the signup verification email sends (request_site only queues it). Skips with a
	note if the keys are absent — like the TLS tail, bootstrap stays runnable on a
	site with no SMTP yet (the queue entry is then a harmless no-op)."""
	host = frappe.conf.get("atlas_smtp_host")
	if not host:
		print(
			"[bootstrap] no atlas_smtp_host — skipping outbound email setup. "
			"Verification emails will queue but not send until an Email Account is configured."
		)
		return
	login = require_config("atlas_smtp_login")
	password = require_config("atlas_smtp_password")
	from_address = frappe.conf.get("atlas_smtp_from") or login
	port = int(frappe.conf.get("atlas_smtp_port", 587))

	name = "Atlas Outbound"
	if frappe.db.exists("Email Account", name):
		account = frappe.get_doc("Email Account", name)
	else:
		account = frappe.new_doc("Email Account")
		account.email_account_name = name
	account.update(
		{
			"email_id": from_address,
			"smtp_server": host,
			"smtp_port": port,
			# Frappe only reads login_id when login_id_is_different is set; otherwise
			# it logs in as email_id. Flag it only when the SMTP user differs from From.
			"login_id_is_different": 1 if login != from_address else 0,
			"login_id": login,
			"password": password,
			"use_tls": 1,
			"enable_outgoing": 1,
			"default_outgoing": 1,
			"awaiting_password": 0,
		}
	)
	account.save(ignore_permissions=True)
	frappe.db.commit()
	print(f"[bootstrap] outbound Email Account {name!r} configured ({from_address} via {host}:{port})")


def require_config(key: str) -> str:
	value = frappe.conf.get(key)
	if not value:
		frappe.throw(
			f"site config missing {key!r}. Set with: bench --site <site> set-config -p {key} <value>"
		)
	return value


def _resolve_fleet_public_key() -> str | None:
	"""The OpenSSH public key for `Atlas Settings.ssh_public_key`.

	Prefer an explicit `atlas_ssh_public_key`; otherwise DERIVE it from the private
	key at `atlas_ssh_private_key_path` (ssh-keygen -y). This field is load-bearing
	for self-serve: `Site._provision_backing_vm` clones the golden snapshot with
	`ssh_public_key=<this>`, and a blank value makes the clone's VM insert throw
	`MandatoryError: ssh_public_key` — so a self-serve bootstrap that only sets the
	private key path (the common case) would otherwise fail the first signup. Returns
	None only if neither the config key nor a readable private key is present."""
	configured = frappe.conf.get("atlas_ssh_public_key")
	if configured:
		return configured
	key_path = frappe.conf.get("atlas_ssh_private_key_path")
	if not key_path:
		return None
	expanded = os.path.expanduser(key_path)
	if not os.path.isfile(expanded):
		return None
	import subprocess

	result = subprocess.run(["ssh-keygen", "-y", "-f", expanded], capture_output=True, text=True)
	return result.stdout.strip() if result.returncode == 0 else None


def load_key(value: str) -> str:
	"""Accept either inline PEM contents or a path to a key file."""
	if value.lstrip().startswith("-----BEGIN") or value.lstrip().startswith("ssh-"):
		return value
	path = os.path.expanduser(value)
	if not os.path.isfile(path):
		frappe.throw(f"key file not found at {path!r}")
	with open(path) as handle:
		return handle.read().strip()


def load_vm_ssh_public_key() -> str:
	configured = frappe.conf.get("atlas_vm_ssh_public_key")
	if configured:
		return load_key(configured)
	default_path = os.path.expanduser("~/.ssh/id_ed25519.pub")
	if not os.path.isfile(default_path):
		frappe.throw(
			"no SSH public key for the VM. Set atlas_vm_ssh_public_key in site "
			f"config or place one at {default_path!r}"
		)
	with open(default_path) as handle:
		return handle.read().strip()
