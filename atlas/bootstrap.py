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

    atlas_provider_type           "DigitalOcean" or "Self-Managed"
    atlas_ssh_private_key_path    absolute path to the SSH private key on disk
                                  (0600, readable by the Frappe user)
    atlas_ssh_key_id              vendor's handle for the uploaded SSH key
                                  (DigitalOcean only; required for DO — accepts
                                  DO's numeric key id or its SHA-256 fingerprint).
    atlas_ssh_public_key          optional OpenSSH public key body for vendors
                                  that upload at provision time

DigitalOcean providers also need:

    atlas_do_token                DO personal access token
    atlas_do_region               e.g. "blr1"
    atlas_do_default_size         vendor-native slug, e.g. "s-2vcpu-4gb-intel"
                                  (Atlas prefixes "DigitalOcean/" internally)
    atlas_do_default_image        vendor-native slug, e.g. "ubuntu-24-04-x64"

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

Optional self-serve tail (run via `atlas.bootstrap.run_with_self_serve`):

`run_with_proxy()` plus wiring the golden bench snapshot + outbound email so a
site can take a public `/signup` (spec/14-self-serve.md). Both steps skip with a
note when absent, so this stays a safe drop-in for `run_with_proxy`.

    atlas_default_bench_snapshot     golden bench Virtual Machine Snapshot name
                                     (else: newest Available golden-bench* is adopted)
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
	provider = ensure_provider()
	server_name = provision_server(provider)
	wait_for_active_server(server_name)
	ensure_image()
	sync_image(server_name)
	provision_virtual_machine(server_name)


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


def restore_credentials() -> None:
	"""Re-write the four credential fields the unit suite clobbers, from site config.

	The shared dev DB is also the test DB: a unit run leaves fake values in the
	Singles (`set_atlas_settings`/`set_digitalocean_settings` write `dop_v1_fake`,
	`atlas-test-ssh-key.pem`, `key-id-123`), so the next *real* provision/build/e2e
	fails with a bogus token or an unusable key (memory: real-provision-traps #4).
	This restores the real values from `common_site_config.json` —
	`atlas_ssh_private_key_path`, `atlas_ssh_key_id`, optional `atlas_ssh_public_key`,
	and the DO `atlas_do_token` — without `ensure_provider`'s catalog discover()
	network call. Run it before any host turn:

	    bench --site <site> execute atlas.bootstrap.restore_credentials

	Idempotent; safe to re-run. Fails loud (`require_config`) if a key is missing,
	since a half-restored credential set is worse than a clean error."""
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_private_key_path",
		require_config("atlas_ssh_private_key_path"),
		update_modified=False,
	)
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_key_id",
		require_config("atlas_ssh_key_id"),
		update_modified=False,
	)
	public_key = frappe.conf.get("atlas_ssh_public_key")
	if public_key:
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", public_key, update_modified=False)
	frappe.utils.password.set_encrypted_password(
		"DigitalOcean Settings",
		"DigitalOcean Settings",
		require_config("atlas_do_token"),
		"api_token",
	)
	frappe.db.commit()
	print("[bootstrap] restored Atlas/DigitalOcean credentials from site config")


def ensure_provider() -> "frappe.model.document.Document":
	provider_type = require_config("atlas_provider_type")
	if provider_type not in ("DigitalOcean", "Self-Managed"):
		frappe.throw(f"atlas_provider_type must be DigitalOcean or Self-Managed, got {provider_type!r}")

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
	public_key = frappe.conf.get("atlas_ssh_public_key")
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

	frappe.db.commit()
	return frappe.get_doc("Provider", PROVIDER_NAME)


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
	if provider.provider_type == "DigitalOcean":
		server_name = provider.provision_server(title)
	else:
		server_name = provider.provision_server(
			title,
			ipv4_address=require_config("atlas_self_managed_ipv4"),
			ipv6_address=require_config("atlas_self_managed_ipv6"),
			ipv6_prefix=require_config("atlas_self_managed_ipv6_prefix"),
			ipv6_virtual_machine_range=require_config("atlas_self_managed_ipv6_vm_range"),
		)
	frappe.db.commit()
	print(f"[bootstrap] provisioning Server {title!r} (name={server_name!r}; background job enqueued)")
	return server_name


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
	image = frappe.get_doc("Virtual Machine Image", IMAGE_NAME)
	task_name = image.sync_to_server(server_name)
	print(f"[bootstrap] syncing image to {server_name!r} (Task {task_name!r})")
	wait_for_task(task_name, timeout_seconds)


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


def run_with_self_serve() -> None:
	"""`run_with_proxy()` plus the self-serve tail: wire the golden bench snapshot
	and outbound email so a fresh site can take a public `/signup`.

	The compute + proxy + TLS bootstrap (`run_with_proxy`) always happens first —
	it seeds the Root Domain that `Site.before_insert` resolves the region + FQDN
	suffix from (spec/14, Contract A), so self-serve has no separate domain step.
	Then two settings the signup flow needs:

	  - `Atlas Settings.default_bench_snapshot` — the golden image a Site's backing
	    VM clones from (plan 01). Wired from `atlas_default_bench_snapshot` if set,
	    else from the most recent Available `golden-bench*` snapshot if one exists.
	  - the outbound Email Account — so the verification email actually sends
	    (`request_site` only queues it; spec/14 calls outbound email an operator
	    prerequisite). Configured from the `atlas_smtp_*` keys.

	Each step skips with a printed note when its inputs are absent, so this is a
	safe drop-in for `run_with_proxy` — mirroring how the TLS tail degrades."""
	run_with_proxy()
	ensure_default_bench_snapshot()
	ensure_outbound_email()


def ensure_default_bench_snapshot() -> None:
	"""Point `Atlas Settings.default_bench_snapshot` at an Available golden bench
	snapshot. Prefers the explicitly configured `atlas_default_bench_snapshot`;
	otherwise adopts the newest Available snapshot whose title starts `golden-bench`
	(what the bake e2e leaves). Skips with a printed pointer if none exists — the
	bake is a billable host run (plan 01), not something to trigger from bootstrap."""
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
			"Bake it (billable host run, plan 01):\n"
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
