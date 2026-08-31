# Control API

The control daemon listens on `127.0.0.1:9000` by default. Set the token in
`ATLAS_CONTROL_TOKEN` and send it as a bearer token:

```sh
curl -H "Authorization: Bearer $ATLAS_CONTROL_TOKEN" \
  http://127.0.0.1:9000/v1/state
```

Unauthenticated lifecycle endpoints are available for systemd and monitoring:

```text
GET /healthz  daemon is running
GET /readyz   daemon has successfully reconciled with OpenResty
```

Authenticated configuration endpoints:

```text
PUT    /v1/sites                 replace all site mappings
PATCH  /v1/sites/<name>          set one site mapping
DELETE /v1/sites/<name>          remove one site mapping

PUT    /v1/domains               replace all custom-domain mappings
PATCH  /v1/domains/<name>        set one custom-domain mapping
DELETE /v1/domains/<name>        remove one custom-domain mapping

GET    /v1/state                 read daemon desired state
```

Examples:

```sh
curl -X PATCH -H "Authorization: Bearer $ATLAS_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"address":"2001:db8::10"}' \
  http://127.0.0.1:9000/v1/sites/example

curl -X DELETE -H "Authorization: Bearer $ATLAS_CONTROL_TOKEN" \
  http://127.0.0.1:9000/v1/domains/example.com
```

The daemon accepts raw IPv6 addresses. It validates and forwards them to the
OpenResty API, which adds the correct port for each traffic path.
