"""The explicit setup contract — one typed, scriptable entry point that writes
every value Atlas needs to bootstrap a provider / TLS, with NO reads from
`frappe.conf`.

Two front-ends feed the same code:

  - `run(config)` — CI / E2E / fast-deploy call this with a plain dict. One JSON
    document = one full environment's config. Never reads site config.
  - the Frappe Setup Wizard — `get_setup_stages(args)` maps the slide answers onto
    the same Layer-1 setters (see `atlas/public/js/setup_wizard.js` + the
    `setup_wizard_*` hooks).

`run(config)` ONLY configures (writes the Singles / Root Domain / Email Account).
The billable, async steps — provision a Server, bake the golden snapshot, stand up
the proxy + reserved IP, issue the cert — stay in `bootstrap.py` / desk buttons and
are deliberately out of scope here (a re-runnable config step must not strand
billable infra). `bootstrap.run*` calls `from_site_config()` → `run()` as its first
step, then layers the provisioning on top.

`config` schema (every block optional except `provider`)::

    {
        "provider": {
            "provider_type": "DigitalOcean" | "Scaleway" | "Self-Managed" | "Fake",
            "region": "blr1",  # THIS Atlas's single region (Atlas Settings.region)
            "ssh_private_key_path": "~/.ssh/id_ed25519",
            "ssh_public_key": "ssh-ed25519 …",  # optional; derived from the key path if omitted
            "default_bench_snapshot": "golden-…",  # optional
            # exactly one vendor sub-block, matching provider_type:
            "digitalocean": {
                "api_token": "dop_v1_…",
                "region": "blr1",  # DO's OWN API region (not Atlas Settings.region)
                "default_size": "s-2vcpu-4gb-intel",
                "default_image": "ubuntu-24-04-x64",
                "ssh_key_id": "12345678",
            },
            "scaleway": {
                "secret_key": "…",
                "project_id": "…",
                "zone": "fr-par-2",  # Scaleway's OWN zone
                "default_size": "EM-A610R-NVME",
                "default_image": "Ubuntu_24.04",
                "organization_id": "…",  # optional
                "billing": "hourly",  # optional
                "ssh_key_id": "…",  # optional
            },
            "self_managed": {  # per-server networking, forwarded to provision_server
                "ipv4_address": "…",
                "ipv6_address": "…",
                "ipv6_prefix": "…",
                "ipv6_virtual_machine_range": "…",
            },
        },
        "tls": {  # optional — seeds Route53/LE/Root Domain
            "domain": "blr1.frappe.dev",
            "region": "blr1",  # the Atlas region the wildcard fronts
            "access_key_id": "…",
            "secret_access_key": "…",
            "aws_region": "us-east-1",  # optional
            "account_email": "ops@…",
            "acme_directory_url": "…",  # optional; defaults to LE staging
        },
    }

The Self-Managed networking under `provider.self_managed` is NOT a Single — it is
inherently per-server and is returned by `self_managed_networking(config)` for the
caller (`bootstrap.provision_server`) to forward to `provision_server`.
"""

from __future__ import annotations

import frappe
from frappe import _


def run(config: dict) -> dict:
	"""Idempotent, explicit. Drive the Layer-1 setters + TLS seeding from
	`config` (a plain dict — NEVER `frappe.conf`). Returns a summary dict of what was
	configured. Calls setters in dependency order; does NOT provision/bake/issue."""
	provider = config.get("provider") or frappe.throw(_("setup config needs a 'provider' block"))
	provider_type = provider.get("provider_type")

	# 1. Vendor-agnostic Single. `region` is THIS Atlas's single region (the source
	#    of truth), distinct from the vendor's own API region/zone set below.
	frappe.get_single("Atlas Settings").setup(
		provider_type=provider_type,
		ssh_private_key_path=provider["ssh_private_key_path"],
		region=provider["region"],
		ssh_public_key=provider.get("ssh_public_key"),
		default_bench_snapshot=provider.get("default_bench_snapshot"),
	)

	# 2. Vendor Single (its OWN region/zone + creds + default-Link catalog rows).
	if provider_type == "DigitalOcean":
		do = provider["digitalocean"]
		frappe.get_single("DigitalOcean Settings").setup(
			api_token=do["api_token"],
			region=do["region"],
			default_size=do["default_size"],
			default_image=do["default_image"],
			ssh_key_id=do.get("ssh_key_id") or None,
		)
	elif provider_type == "Scaleway":
		scw = provider["scaleway"]
		frappe.get_single("Scaleway Settings").setup(
			secret_key=scw["secret_key"],
			project_id=scw["project_id"],
			zone=scw["zone"],
			default_size=scw["default_size"],
			default_image=scw["default_image"],
			organization_id=scw.get("organization_id"),
			billing=scw.get("billing", "hourly"),
			ssh_key_id=scw.get("ssh_key_id"),
		)
	# Self-Managed has no vendor Single — its networking rides the provision payload.

	summary = {"provider_type": provider_type}

	# 3. TLS layer (optional): Route53 + LE setters, the active DNS/TLS vendor types
	#    on Atlas Settings, and the Root Domain row Site.before_insert reads.
	tls = config.get("tls")
	if tls:
		setup_tls_layer(tls)
		summary["tls_domain"] = tls["domain"]

	# nosemgrep: frappe-manual-commit -- setup orchestrator: persist the full config so enqueued bootstrap/provision jobs read it cross-transaction.
	frappe.db.commit()
	return summary


def setup_tls_layer(tls: dict) -> None:
	"""Seed Route53 Settings + Lets Encrypt Settings via their setters, set the active
	DNS/TLS vendor types on Atlas Settings, and create the Root Domain row. Mirrors
	the desk first-run order (spec/13-tls.md). `tls['region']` is the Atlas region the
	wildcard fronts (Root Domain denormalizes it)."""
	frappe.get_single("Route53 Settings").setup(
		access_key_id=tls["access_key_id"],
		secret_access_key=tls["secret_access_key"],
		region=tls.get("aws_region", "us-east-1"),
	)
	frappe.get_single("Lets Encrypt Settings").setup(
		account_email=tls["account_email"],
		acme_directory_url=tls.get("acme_directory_url")
		or "https://acme-staging-v02.api.letsencrypt.org/directory",
	)
	frappe.db.set_single_value("Atlas Settings", "dns_provider_type", "Route53", update_modified=False)
	frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt", update_modified=False)
	if not frappe.db.exists("Root Domain", tls["domain"]):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": tls["domain"],
				"region": tls["region"],
				"is_active": 1,
			}
		).insert(ignore_permissions=True)


def self_managed_networking(config: dict) -> dict | None:
	"""The per-server networking kwargs for a Self-Managed `provision_server`, or None.

	Self-Managed networking is inherently per-server (the box already exists), so it
	is NOT a Single — `run()` does not write it. The caller (bootstrap.provision_server)
	forwards these to `Atlas Settings.provision_server`."""
	provider = config.get("provider") or {}
	if provider.get("provider_type") != "Self-Managed":
		return None
	return provider.get("self_managed")


# --- back-compat adapter: site config → the `config` dict --------------------


def from_site_config() -> dict:
	"""Read the `atlas_*` site-config keys ONE place and build the `config` dict.

	The single remaining reader of `frappe.conf` for setup — every other code path
	goes through `run(config)`. Existing benches keep their `atlas_*` keys; this
	adapter lets `bootstrap.run*` drive the new contract without the operator
	re-entering anything. New benches use the Setup Wizard or call `run()` directly.

	`region` (Atlas Settings.region, the source of truth) prefers the explicit
	`atlas_tls_region`; else the active vendor's OWN region key (`atlas_do_region` /
	`atlas_scw_zone`) as a sensible default — Atlas pins one region per vendor today.
	The vendor sub-block always carries the vendor's OWN region/zone separately (it is
	conceptually independent: a vendor operates in many regions)."""
	from atlas.bootstrap import LETS_ENCRYPT_STAGING, require_config

	provider_type = require_config("atlas_provider_type")
	region = (
		frappe.conf.get("atlas_tls_region")
		or frappe.conf.get("atlas_do_region")
		or frappe.conf.get("atlas_scw_zone")
	)
	if not region:
		frappe.throw(
			_(
				"Set atlas_tls_region (or the active vendor's region key: atlas_do_region / "
				"atlas_scw_zone) — Atlas Settings.region is required."
			)
		)

	provider: dict = {
		"provider_type": provider_type,
		"region": region,
		"ssh_private_key_path": require_config("atlas_ssh_private_key_path"),
		"ssh_public_key": frappe.conf.get("atlas_ssh_public_key"),
		"default_bench_snapshot": frappe.conf.get("atlas_default_bench_snapshot"),
	}

	if provider_type == "DigitalOcean":
		provider["digitalocean"] = {
			"api_token": require_config("atlas_do_token"),
			"region": require_config("atlas_do_region"),
			"default_size": require_config("atlas_do_default_size"),
			"default_image": require_config("atlas_do_default_image"),
			"ssh_key_id": frappe.conf.get("atlas_ssh_key_id"),
		}
	elif provider_type == "Scaleway":
		provider["scaleway"] = {
			"secret_key": require_config("atlas_scw_secret_key"),
			"project_id": require_config("atlas_scw_project_id"),
			"zone": require_config("atlas_scw_zone"),
			"default_size": require_config("atlas_scw_default_size"),
			"default_image": require_config("atlas_scw_default_image"),
			"organization_id": frappe.conf.get("atlas_scw_organization_id"),
			"billing": frappe.conf.get("atlas_scw_billing") or "hourly",
			"ssh_key_id": frappe.conf.get("atlas_ssh_key_id"),
		}
	elif provider_type == "Self-Managed":
		provider["self_managed"] = {
			"ipv4_address": require_config("atlas_self_managed_ipv4"),
			"ipv6_address": require_config("atlas_self_managed_ipv6"),
			"ipv6_prefix": require_config("atlas_self_managed_ipv6_prefix"),
			"ipv6_virtual_machine_range": require_config("atlas_self_managed_ipv6_vm_range"),
		}

	config: dict = {"provider": provider}

	domain = frappe.conf.get("atlas_tls_domain")
	if domain:
		config["tls"] = {
			"domain": domain,
			"region": frappe.conf.get("atlas_tls_region") or require_config("atlas_do_region"),
			"access_key_id": require_config("atlas_route53_access_key_id"),
			"secret_access_key": require_config("atlas_route53_secret_access_key"),
			"aws_region": frappe.conf.get("atlas_route53_region", "us-east-1"),
			"account_email": require_config("atlas_acme_account_email"),
			"acme_directory_url": frappe.conf.get("atlas_acme_directory_url", LETS_ENCRYPT_STAGING),
		}

	return config


# --- Frappe Setup Wizard front-end ------------------------------------------


def get_setup_stages(args: dict) -> list[dict]:
	"""`setup_wizard_stages` hook — map the wizard slide answers onto the Layer-1
	setters. Returns Frappe stage dicts; Frappe commits after all stages succeed, so
	the stage `fn`s MUST NOT `frappe.db.commit()`. Slide values arrive as strings.

	`args` is the merged slide payload (flat keys from `setup_wizard.js`)."""
	stages = [
		{
			"status": _("Configuring provider"),
			"fail_msg": _("Failed to configure the provider"),
			"tasks": [{"fn": _stage_provider, "args": args, "fail_msg": _("Provider setup failed")}],
		}
	]
	if _truthy(args.get("setup_tls")):
		stages.append(
			{
				"status": _("Configuring TLS"),
				"fail_msg": _("Failed to configure TLS"),
				"tasks": [{"fn": _stage_tls, "args": args, "fail_msg": _("TLS setup failed")}],
			}
		)
	return stages


def _stage_provider(args: dict) -> None:
	provider_type = args.get("provider_type")
	frappe.get_single("Atlas Settings").setup(
		provider_type=provider_type,
		ssh_private_key_path=args.get("ssh_private_key_path"),
		region=args.get("region"),
		ssh_public_key=args.get("ssh_public_key") or None,
	)
	if provider_type == "DigitalOcean":
		frappe.get_single("DigitalOcean Settings").setup(
			api_token=args.get("do_api_token"),
			region=args.get("do_region"),
			default_size=args.get("do_default_size"),
			default_image=args.get("do_default_image"),
			ssh_key_id=args.get("do_ssh_key_id") or None,
		)
	elif provider_type == "Scaleway":
		frappe.get_single("Scaleway Settings").setup(
			secret_key=args.get("scw_secret_key"),
			project_id=args.get("scw_project_id"),
			zone=args.get("scw_zone"),
			default_size=args.get("scw_default_size"),
			default_image=args.get("scw_default_image"),
			organization_id=args.get("scw_organization_id") or None,
			billing=args.get("scw_billing") or "hourly",
			ssh_key_id=args.get("scw_ssh_key_id") or None,
		)


LETS_ENCRYPT_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"


def _stage_tls(args: dict) -> None:
	setup_tls_layer(
		{
			"domain": args.get("tls_domain"),
			"region": args.get("region"),
			"access_key_id": args.get("route53_access_key_id"),
			"secret_access_key": args.get("route53_secret_access_key"),
			"aws_region": args.get("route53_region") or "us-east-1",
			"account_email": args.get("acme_account_email"),
			"acme_directory_url": _resolve_acme_url(args),
		}
	)


def _resolve_acme_url(args: dict) -> str | None:
	"""Map the wizard's Certificate Environment Select onto an ACME directory URL.
	`None` lets `setup_tls_layer` apply its staging default."""
	environment = args.get("acme_environment") or ""
	if environment.startswith("Production"):
		return LETS_ENCRYPT_PRODUCTION
	if environment.startswith("Custom"):
		return args.get("acme_directory_url") or None
	return None  # Staging (the default)


def on_complete(args: dict | None = None) -> None:
	"""`setup_wizard_complete` hook — runs after all stages commit. No-op today (the
	Singles are configured; the operator drives provisioning from Atlas Settings)."""


def _truthy(value: object) -> bool:
	"""Frappe posts checkbox values as "0"/"1"/strings/bools — normalize."""
	return str(value).lower() in ("1", "true", "yes", "on")


# --- Wizard "Test Connection / Fetch Catalog" -------------------------------


@frappe.whitelist()
def wizard_discover(provider_type: str, credentials: dict | str | None = None) -> dict:
	"""Probe the vendor with the credentials the operator JUST TYPED (not yet saved)
	and return its live catalog, so the wizard can turn free-text slug boxes into
	pick-lists and show a connection result — BEFORE Complete Setup writes anything.

	Talks to the vendor *client* directly with the ad-hoc creds rather than the
	Provider (which reads the saved Singles). Returns a plain dict:

	    {"ok": bool, "account_label": str|None, "error": str|None,
	     "sizes":  [{"value","label"}], "images": [{"value","label"}],
	     "ssh_keys": [{"value","label"}], "projects": [{"value","label"}]}

	`sizes`/`images` use the vendor-native slug as `value` (what the slug fields
	store). Never raises — a bad credential comes back as `{"ok": False, "error": …}`
	so the button renders a red toast instead of a traceback."""
	import json

	if isinstance(credentials, str):
		credentials = json.loads(credentials) if credentials else {}
	credentials = credentials or {}

	if provider_type == "DigitalOcean":
		return _discover_digitalocean(credentials)
	if provider_type == "Scaleway":
		return _discover_scaleway(credentials)
	# Self-Managed / Fake have no remote catalog to fetch.
	return {
		"ok": True,
		"account_label": None,
		"error": None,
		"sizes": [],
		"images": [],
		"ssh_keys": [],
		"projects": [],
	}


def _discover_digitalocean(credentials: dict) -> dict:
	from atlas.atlas.digitalocean import DigitalOceanClient
	from atlas.atlas.providers.digitalocean import (
		DIGITALOCEAN_MONTHLY_COST_USD,
		KNOWN_DIGITALOCEAN_IMAGES,
		KNOWN_DIGITALOCEAN_SIZES,
	)

	result = {
		"ok": False,
		"account_label": None,
		"error": None,
		# DO's catalog is hand-maintained constants (no live size/image API), so these
		# are available regardless of the token — the auth check below gates the toast.
		"sizes": [
			{"value": slug, "label": _size_label(slug, DIGITALOCEAN_MONTHLY_COST_USD.get(slug))}
			for slug in KNOWN_DIGITALOCEAN_SIZES
		],
		"images": [{"value": slug, "label": slug} for slug in KNOWN_DIGITALOCEAN_IMAGES],
		"ssh_keys": [],
		"projects": [],
	}
	token = credentials.get("api_token")
	if not token:
		result["error"] = _("Enter an API Token, then Test Connection.")
		return result
	try:
		client = DigitalOceanClient(token=token)
		auth = client.verify_credentials()
		result["ssh_keys"] = [
			{"value": str(key["id"]), "label": key.get("name") or str(key["id"])}
			for key in client.list_ssh_keys()
		]
	except Exception as exception:  # any failure becomes a red toast, never a traceback
		result["error"] = str(exception)
		return result
	result["ok"] = True
	rate = (
		f" · {auth['rate_remaining']}/{auth['rate_limit']} API calls left"
		if auth.get("rate_remaining") is not None
		else ""
	)
	result["account_label"] = f"{auth.get('email') or 'DigitalOcean'}{rate}"
	return result


def _discover_scaleway(credentials: dict) -> dict:
	from atlas.atlas.providers.scaleway import _image_from_os, _size_from_offer
	from atlas.atlas.scaleway import ScalewayClient

	result = {
		"ok": False,
		"account_label": None,
		"error": None,
		"sizes": [],
		"images": [],
		"ssh_keys": [],
		"projects": [],
	}
	secret_key = credentials.get("secret_key")
	zone = credentials.get("zone")
	if not (secret_key and zone):
		result["error"] = _("Enter a Secret Key and Zone, then Test Connection.")
		return result
	organization_id = credentials.get("organization_id") or None
	project_id = credentials.get("project_id") or None
	billing = credentials.get("billing") or "hourly"
	client = ScalewayClient(secret_key=secret_key, zone=zone)
	try:
		auth = client.verify_credentials(organization_id)
		result["projects"] = [
			{"value": project["id"], "label": project.get("name") or project["id"]}
			for project in client.list_projects(organization_id)
		]
		result["sizes"] = [
			_size_label_dict(_size_from_offer(offer))
			for offer in client.list_offers(subscription_period=billing)
		]
		result["images"] = [
			{"value": img.slug, "label": img.slug} for img in map(_image_from_os, client.list_os())
		]
		if project_id:
			result["ssh_keys"] = [
				{"value": key["id"], "label": key.get("name") or key["id"]}
				for key in client.list_ssh_keys(project_id)
			]
	except Exception as exception:  # any failure becomes a red toast, never a traceback
		result["error"] = str(exception)
		return result
	result["ok"] = True
	result["account_label"] = auth.get("account_label") or _("Scaleway")
	return result


def _size_label(slug: str, monthly_cost_usd: int | None) -> str:
	return f"{slug} — ${monthly_cost_usd}/mo" if monthly_cost_usd else slug


def _size_label_dict(size) -> dict:
	return {"value": size.slug, "label": _size_label(size.slug, size.monthly_cost_usd or None)}
