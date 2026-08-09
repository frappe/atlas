# Bootstrap a fresh Atlas site

Stand up Atlas on a fresh site in one command. The bootstrap configures
`Atlas Settings`, provisions a server with your cloud provider, syncs the base
image, and (optionally) bakes a golden bench image, stands up the edge proxy,
and issues a wildcard TLS certificate.

It drives the same whitelisted methods the operator Desk buttons call, so the
result is identical to clicking through the first-run order in
[spec/README.md](./spec/README.md#first-run-on-a-fresh-site).

> **This provisions real, billable infrastructure** in your cloud account
> (a droplet, and — for the self-serve flow — a build VM, a proxy VM, and a
> reserved IPv4). Tear it down when you are done.

## Necessary steps

1. **Configure the site.** The bootstrap reads `atlas_*` keys from site config.
   Set the provider, SSH key, region, and vendor catalog:

   ```bash
   SITE=bootstrap.local

   bench --site $SITE set-config atlas_provider_type DigitalOcean
   bench --site $SITE set-config atlas_ssh_private_key_path ~/.ssh/id_rsa
   bench --site $SITE set-config atlas_ssh_key_id <DO_SSH_KEY_ID>
   bench --site $SITE set-config atlas_do_token <DO_TOKEN>
   bench --site $SITE set-config atlas_do_region blr1
   bench --site $SITE set-config atlas_do_default_size s-2vcpu-4gb-intel
   bench --site $SITE set-config atlas_do_default_image ubuntu-24-04-x64
   ```

   Use plain `set-config` for string values; `set-config -p` parses the value
   as a Python literal and rejects bare strings like `DigitalOcean`.

2. **Start the bench** (web + worker). The bootstrap enqueues background jobs it
   waits on, so a worker **must** be running. On macOS the worker needs
   `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (already set in the Procfile) or
   provisioning hangs.

   ```bash
   bench start
   ```

3. **Run the bootstrap.**

   ```bash
   bench --site $SITE execute atlas.bootstrap.run
   ```

   `run` is compute-only: settings → provider → server → base image → one test
   VM. It stops at the first VM and provisions nothing else.

## Recommended sizing

**Use the smallest box that boots for a bootstrap.** Compute-only `run` only
boots a 512 MB test VM, so a small host is plenty:

| Provider     | Bootstrap (`run`)  | Self-serve (`run_self_serve`)       |
| ------------ | ------------------ | ----------------------------------- |
| DigitalOcean | `s-1vcpu-2gb`      | `s-2vcpu-4gb-intel` or larger       |

The golden bench bake (`run_self_serve`) needs more room — a Frappe clone + uv
venv + node deps overflow the 4 GB base image — so size up only when you run the
full flow. Avoid large droplets (e.g. `s-8vcpu-32gb-amd`) for a plain
bootstrap; they bill far more than the test VM needs.

---

<details>
<summary>Full provisioning (proxy + TLS) and all config keys</summary>

### Entry points

| Command                                                       | What it stands up |
| ------------------------------------------------------------ | ----------------- |
| `atlas.bootstrap.run`                                         | Compute only: server → base image → one test VM. |
| `atlas.bootstrap.run_with_proxy`                             | `run` + the TLS layer (Route 53 + Let's Encrypt + Root Domain) and the regional wildcard cert. Needs the `atlas_tls_*` keys. |
| `atlas.bootstrap.run_self_serve`                            | The whole signup flow, left running: compute + golden image + proxy VM + reserved IPv4 + wildcard cert. Needs the `atlas_tls_*` keys, plus `certbot`, `openssl`, and the selected certbot DNS plugin on the controller (`certbot-dns-route53` + `boto3` for Route53, `certbot-dns-pdns` for PowerDNS). |

```bash
bench --site $SITE execute atlas.bootstrap.run_self_serve
```

`run_self_serve` is heavy (one droplet + a build VM + a proxy VM + one reserved
IPv4, all left running) and slow (the golden bake is apt + clone + uv + node).

### Provider config keys

DigitalOcean also needs `atlas_do_token`, `atlas_do_region`,
`atlas_do_default_size`, `atlas_do_default_image` (shown above). Scaleway and
Self-Managed have their own key sets. The TLS tail (`run_with_proxy` /
`run_self_serve`) reads `atlas_tls_domain`, `atlas_tls_region`,
`atlas_dns_provider_type` (defaults to `Route53`), provider credentials
(`atlas_route53_access_key_id` / `atlas_route53_secret_access_key` or
`atlas_powerdns_api_url` / `atlas_powerdns_api_key` / optional
`atlas_powerdns_server_id`), `atlas_acme_account_email`, and
`atlas_acme_directory_url` (defaults to Let's Encrypt **staging** so an unattended
run never burns production quota).

The authoritative, fully-commented list of every key for every provider lives in
the module docstring of [`atlas/bootstrap.py`](./atlas/bootstrap.py), and the
first-run build order is in
[spec/README.md](./spec/README.md#first-run-on-a-fresh-site).

</details>
