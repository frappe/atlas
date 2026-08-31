# Control daemon

The control daemon is the main API for the controller. It listens on `0.0.0.0:9000` and `[::]:9000` by default.

The daemon reads the OpenResty maps through a Unix socket. The daemon does not store a second copy of the maps.

CAUTION: Allow port `9000` only from the controller network. The daemon can change proxy routes and certificates.

## Authentication

Send the raw password in a bearer token. The daemon checks the password against the bcrypt hash in `/etc/atlas/proxy-control.htpasswd`.

```sh
export ATLAS_CONTROL_PASSWORD='replace-with-the-raw-password'
export ATLAS_CONTROL_URL='http://[2001:db8::1]:9000'
curl -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" "$ATLAS_CONTROL_URL/v1/state"
```

The daemon returns `401` if the file is missing, empty, not valid, or does not match the password.

## Health endpoints

Use these endpoints with authentication.

| Method and path | Use | Success result |
| --- | --- | --- |
| `GET /healthz` | Check that the daemon runs. | `200` |
| `GET /readyz` | Check that the OpenResty admin API runs. | `204` |

```sh
curl -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" "$ATLAS_CONTROL_URL/healthz"
curl -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -o /dev/null "$ATLAS_CONTROL_URL/readyz"
```

## State endpoint

Use `GET /v1/state` to get the current maps from OpenResty.

```sh
curl -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" "$ATLAS_CONTROL_URL/v1/state"
```

The response has a `sites` object and a `domains` object.

```json
{
  "sites": {"erp": "2001:db8::10"},
  "domains": {"www.example.com": "2001:db8::20"}
}
```

## Site map

A site is a name below the proxy wildcard domain. If the wildcard domain is `*.iad.frappe.dev`, the `erp` site key routes `erp.iad.frappe.dev`.

Use `PUT /v1/sites` to replace the full map. Send the full map when the controller starts or when it restores state.

```sh
curl -X PUT -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -H 'Content-Type: application/json' -d '{"erp":"2001:db8::10","shop":"2001:db8::11"}' "$ATLAS_CONTROL_URL/v1/sites"
```

Use `PATCH /v1/sites/<name>` to add or change one site.

```sh
curl -X PATCH -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -H 'Content-Type: application/json' -d '{"address":"2001:db8::10"}' "$ATLAS_CONTROL_URL/v1/sites/erp"
```

Use `DELETE /v1/sites/<name>` to remove one site.

```sh
curl -X DELETE -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" "$ATLAS_CONTROL_URL/v1/sites/erp"
```

Use `"-"` as a site address to stop traffic with a `503` response. Do not use an empty address.

## Custom-domain map

A custom domain is a customer domain such as `www.example.com`. The proxy sends TLS traffic for a custom domain to the site VM without TLS termination.

Use `PUT /v1/domains` to replace the full map.

```sh
curl -X PUT -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -H 'Content-Type: application/json' -d '{"www.example.com":"2001:db8::20"}' "$ATLAS_CONTROL_URL/v1/domains"
```

Use `PATCH /v1/domains/<name>` to add or change one custom domain.

```sh
curl -X PATCH -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -H 'Content-Type: application/json' -d '{"address":"2001:db8::20"}' "$ATLAS_CONTROL_URL/v1/domains/www.example.com"
```

Use `DELETE /v1/domains/<name>` to remove one custom domain.

```sh
curl -X DELETE -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" "$ATLAS_CONTROL_URL/v1/domains/www.example.com"
```

The map can use a wildcard suffix key. The key `*-shop.example.com` matches `a-shop.example.com` and `a-b-shop.example.com`.

An exact domain key has priority. The most specific wildcard suffix has the next priority.

## Wildcard certificate

Use `PUT /v1/certificate` to set the proxy wildcard domain and its certificate. This endpoint applies only to the regional wildcard certificate.

```json
{
  "wildcard_domain": "*.iad.frappe.dev",
  "fullchain_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
  "private_key_pem": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
}
```

Save the JSON in `certificate.json`. Send the request.

```sh
curl -X PUT -H "Authorization: Bearer $ATLAS_CONTROL_PASSWORD" -H 'Content-Type: application/json' --data-binary @certificate.json "$ATLAS_CONTROL_URL/v1/certificate"
```

The daemon checks the certificate date, the certificate name, and the private key. The daemon writes the files, does an OpenResty check, and reloads OpenResty.

The daemon writes `iad.frappe.dev` to `/var/lib/nginx/region`. It writes the certificate files below `/var/lib/nginx/certs/iad.frappe.dev`.

Do not use this endpoint for a custom-domain certificate. Install a custom-domain certificate on its site VM.

## Error responses

| Status | Meaning |
| --- | --- |
| `400` | The request data is not valid. |
| `401` | The password is not valid. |
| `502` | The daemon cannot use the OpenResty admin API. |
| `503` | OpenResty is not ready or cannot reload. |
