# Atlas reverse proxy

This service is a dedicated Atlas VM running stock OpenResty. It terminates the
regional wildcard TLS traffic for Frappe sites and routes each hostname to a site
VM over IPv6. Registered custom domains keep their own certificates: the proxy
reads SNI and passes their TLS connection through without decrypting it.

## Layout

```text
nginx/setup.sh                 reproducible guest image setup
nginx/nginx.conf               OpenResty configuration
nginx/lua/http/                HTTP routing, persistence and API
nginx/lua/stream/              SNI routing, passthrough and private bridge
nginx/pages/                   branded HTML pages installed into nginx
nginx/systemd/openresty.service.d/ service drop-in
control/main.py                 authenticated FastAPI control daemon
docs/                           architecture, API and operations guides
tests/                         Python test suites
tests/docker/                  Dockerfile, Compose file and backend fixtures
```

The build installs its own files at familiar runtime paths, including
`/etc/nginx/nginx.conf`, `/etc/nginx/lua/`, `/usr/share/nginx/html/`,
`/var/lib/nginx/`, `/run/nginx/admin.sock`, and the private
`/run/nginx/sni-bridge.sock`.

The control daemon listens on localhost port `9000` by default. Configure its
htpasswd file and lifecycle in [docs/operations.md](docs/operations.md).

## Traffic

Port 80 forwards normal traffic in plain HTTP. ACME requests for the wildcard
zone are served from `/var/lib/nginx/acme`; ACME requests for registered custom
domains are forwarded to their VM.

Port 443 is an SNI front door:

- wildcard names go to the local TLS terminator and then to the site VM on port 80;
- registered custom domains go through unchanged to the VM on port 443;
- unknown names receive the branded unconfigured-domain response;
- connections without SNI are dropped.

Site and custom-domain maps live in OpenResty shared dictionaries. Changes take
effect without an nginx reload and are persisted with a short debounce.

## Admin API

The controller connects through the Unix socket only:

```sh
curl --unix-socket /run/nginx/admin.sock http://localhost/v1/healthz
```

Full-map operations are useful for bootstrap and reconciliation:

```text
GET /v1/sites
PUT /v1/sites       JSON: {"site":"2001:db8::10"}
POST /v1/sites/sync  full site-map replacement
GET /v1/domains
PUT /v1/domains     JSON: {"example.com":"2001:db8::10"}
POST /v1/domains/sync full domain-map replacement
POST /v1/dump         write both current maps to disk
```

Normal changes can update one entry:

```text
GET    /v1/sites/<subdomain>
PATCH  /v1/sites/<subdomain>   JSON: {"address":"2001:db8::10"}
DELETE /v1/sites/<subdomain>

GET    /v1/domains/<domain>
PATCH  /v1/domains/<domain>    JSON: {"address":"2001:db8::10"}
DELETE /v1/domains/<domain>
```

`PATCH` creates or replaces one entry. `DELETE` returns `204`. A site address of
`-` creates a tombstone and returns `503` to traffic. Domain changes update both
the port-80 HTTP map and the stream-side SNI map through the private bridge.


## Build and start

Run the build inside a freshly provisioned Ubuntu guest as root:

```sh
cd /path/to/http-proxy
sudo ./nginx/setup.sh
sudo systemctl daemon-reload
sudo systemctl enable --now openresty.service atlas-proxy-control.service
```

Atlas then writes the full wildcard root domain to `/var/lib/nginx/region`, puts
the wildcard certificate in `/var/lib/nginx/certs/<region>/`, updates the flat
certificate symlinks, and snapshots the VM.

## Tests

The Docker test uses the same `nginx/setup.sh`:

```sh
cd tests
docker compose -f docker/docker-compose.yml up --build -d
python3 -m pytest test_proxy.py test_build.py test_custom_domain_proxy.py test_latency.py -v
docker compose -f docker/docker-compose.yml down -v
```
