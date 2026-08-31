# Control API

The control daemon listens on `127.0.0.1:9000` by default. Store a bcrypt htpasswd entry in `/etc/atlas/proxy-control.htpasswd` and send the raw password as a bearer token:

```sh
curl -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" \
  http://127.0.0.1:9000/v1/state
```

Unauthenticated lifecycle endpoints are available for systemd and monitoring:

```text
GET /healthz  daemon is running
GET /readyz   OpenResty admin API is available
```

Authenticated configuration endpoints:

```text
PUT    /v1/sites                 replace all site mappings
PATCH  /v1/sites/<name>          set one site mapping
DELETE /v1/sites/<name>          remove one site mapping

PUT    /v1/domains               replace all custom-domain mappings
PATCH  /v1/domains/<name>        set one custom-domain mapping
DELETE /v1/domains/<name>        remove one custom-domain mapping

GET    /v1/state                 read current OpenResty state
PUT    /v1/certificate           replace the active regional wildcard certificate
```

Examples:

```sh
curl -X PATCH -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"address":"2001:db8::10"}' \
  http://127.0.0.1:9000/v1/sites/example

curl -X DELETE -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" \
  http://127.0.0.1:9000/v1/domains/example.com

curl -X PUT -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" \
  -H 'Content-Type: application/json' \
  --data-binary @certificate.json \
  http://127.0.0.1:9000/v1/certificate
```

The certificate request body must contain `wildcard_domain`, `fullchain_pem`, and `private_key_pem`. For example, `wildcard_domain` can be `*.iad.frappe.dev`. The daemon verifies that the certificate covers the wildcard and that the key matches, writes the region and certificate atomically, validates OpenResty, and performs a graceful reload.

The daemon accepts raw IPv6 addresses. It validates and forwards them to the OpenResty API, which adds the correct port for each traffic path.

Domain keys can be exact names or wildcard suffix patterns. For example, `*-something.example.com` matches `one-something.example.com` and `two-something.example.com`. Exact names take priority, followed by the most specific wildcard.
