# Install and configure the HTTP proxy

Atlas normally installs and configures each proxy. Use this guide to install a node by hand or to check the installed services.

## Requirements

Use Ubuntu 24.04 and `root` or `sudo`. Give the VM access to the Ubuntu, OpenResty, and deadsnakes package servers. The control daemon needs Python 3.14.

Each node needs a public IPv4 address, a stable `proxy-NNN.<wildcard-domain>` A record, and HTTPS access to every configured peer. The regional wildcard certificate must cover the node address and `proxy.<wildcard-domain>`.

## Install

1. Copy `services/http-proxy/` to the VM.
2. Write `/etc/atlas/proxy-control.toml` with mode `0600` and owner `root`.
3. Run `sudo ./nginx/setup.sh`.
4. Check `openresty.service`, `atlas-proxy-control.socket`, and `atlas-proxy-control.service`.

The setup script creates the locked `frappe` account, installs OpenResty and the daemon, and creates a placeholder certificate. A repeated run keeps the installed package and restarts OpenResty.

Logrotate checks the OpenResty logs in `/var/log/nginx` every hour. It rotates a log every day, or earlier when it is larger than 1 GiB, and keeps 7 old copies. It compresses each old copy except the newest. After a rotation it sends `USR1` to OpenResty, so OpenResty opens a new log file without a reload.

Before the daemon can become ready, write the complete configuration described [below](#configuration-file).

## Checks

```sh
sudo systemctl status openresty.service \
  atlas-proxy-control.socket atlas-proxy-control.service
curl -fsS -o /dev/null http://127.0.0.1:9000/healthz
curl -fsS -o /dev/null http://127.0.0.1:9000/readyz
journalctl -u atlas-proxy-control.service -f
```

`/healthz` checks OpenResty and its routes. `/readyz` also checks cluster synchronization and the leader.

## Configuration file

The following example configures `proxy-001` in a three-node cluster:

```toml
[control]
domain = "proxy.example.com"
node_domain = "proxy-001.example.com"
admin_socket = "/run/nginx/admin.sock"
cert_dir = "/var/lib/nginx/certs"

[auto_proxy]
address_prefix = "fdaa:1"
host_prefixes = ["site-", "*-vm-"]

[auth]
password_hash = "$2b$12$replace-with-the-current-hash"
previous_password_hash = "$2b$12$replace-with-the-old-hash"
previous_password_valid_until = 1788800000
jwks_url = "https://atlas-42.example.com/api/atlas/jwks.json"
jwks_audience_id = "atlas-proxy:42"
jwks_issuers = ["central", "atlas:42"]

[cluster]
node_id = "proxy-001"
password = "replace-with-the-current-regional-password"
previous_password = "replace-with-the-previous-regional-password"
previous_password_valid_until = 1788800000
state_path = "/var/lib/nginx/cluster-state.json"
peers = [
  { node_id = "proxy-001", address = "https://proxy-001.example.com" },
  { node_id = "proxy-002", address = "https://proxy-002.example.com" },
  { node_id = "proxy-003", address = "https://proxy-003.example.com" },
]

[tls]
wildcard_domain = "*.example.com"
fullchain_pem = '''
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
'''
private_key_pem = '''
-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
'''
```

`control.domain` is the regional address. `control.node_domain` is the local node address. Both names must be one label below the wildcard zone. The apply command writes both labels to `/var/lib/nginx/control-subdomain`, so OpenResty sends them to the daemon before it checks the site map.

`auto_proxy.address_prefix` contains the first two hextets of the regional VM mesh address. `auto_proxy.host_prefixes` lists literal prefixes. A leading `*` matches any non-empty prefix before the remaining text. Leave the section out to turn static routing off. Read [OpenResty data plane](openresty.md) for the label form.

`auth` protects the public map API. The daemon accepts the current password hash and the earlier hash until `previous_password_valid_until`. This value is a Unix time in seconds.

The daemon gets Central and regional Atlas keys from `jwks_url`. The token audience must match `jwks_audience_id`. An issuer must occur in `jwks_issuers` and match the key namespace.

`cluster` protects internal peer routes. It contains the raw regional passwords. It accepts the previous password until `previous_password_valid_until`. The peer array must include the local `node_id` and unique node IDs. It supports at most 5 entries. Every peer address must use HTTPS.

`tls` is required. The apply command refuses a missing certificate, a key that does not match, or a certificate that does not cover the wildcard name.

## Apply a changed configuration

Write the new file atomically, then run:

```sh
sudo /opt/atlas/proxy-control/bin/proxy-control
sudo systemctl restart atlas-proxy-control.service
```

The apply command installs the certificate, writes the region and control label files, and reloads OpenResty. Restart the daemon after a peer, password, or cluster setting changes.

## Join a node

The [proxy provisioning guide](provisioning.md) gives the safe order for peer updates, `/readyz`, DNS publication, and removal. Follow that order after a manual install.
