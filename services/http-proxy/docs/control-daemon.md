# Control daemon

The control daemon manages the site and custom-domain maps for one regional proxy cluster. Atlas writes node settings and peer addresses to `/etc/atlas/proxy-control.toml`.

The daemon listens only on `127.0.0.1:9000`. OpenResty exposes it at `https://<control.domain>/` with the regional wildcard certificate. `atlas-proxy-control.socket` keeps the port open while the daemon restarts. Without systemd socket activation, the daemon binds the port itself.

The `proxy` site name and every site name with the `proxy-` prefix are reserved. A site map cannot use these names.

## Authentication

Every public route except the health and documentation routes needs a Bearer credential. Use the regional proxy password or a valid JSON Web Token (JWT).

A token needs a trusted issuer, the Proxy audience, a `sub` claim, and a `scope` claim. The daemon reads the authority from the signed `scope` and `constraints` claims and not from the subject. A `tenant` claim makes the token an Atlas API credential, and the daemon refuses it.

The `scope` claim selects the permitted resources. Use `site:*` for the site map, `domain:*` for the custom-domain map, or `*` for all resources. The cluster status route needs `*`. Use a space between two scopes, such as `site:* domain:*`.

The optional `constraints` claim limits the names inside a resource. Use the `site` key, the `domain` key, or both. Each key holds 1 or more of these fields:

| Field | Effect |
|---|---|
| `prefix` | The name must start with this text. |
| `suffix` | The name must end with this text. |
| `names` | The name must be one of these exact names. |

A name is permitted when it is in `names`, or when it matches each `prefix` and `suffix` field in the claim. A resource without a constraint applies to all names for that resource. A read route returns only the permitted names. A constrained resource cannot replace its complete map. Name comparison is case-sensitive, and Atlas issues lowercase names.

```json
{
  "scope": "site:* domain:*",
  "constraints": {
    "site": { "prefix": "erp-", "suffix": "-svc" },
    "domain": { "names": ["www.customer.com"] }
  }
}
```

```sh
export ATLAS_PROXY_CONTROL_URL='https://proxy-001.iad.frappe.dev'
export ATLAS_PROXY_CONTROL_TOKEN='replace-with-the-proxy-password-or-a-jwt'
curl -H "Authorization: Bearer $ATLAS_PROXY_CONTROL_TOKEN" "$ATLAS_PROXY_CONTROL_URL/v1/sites"
```

Internal routes use the `X-Atlas-Cluster-Password` header and are not part of the public API. Atlas rotates the regional password every 6 hours. Public and internal routes accept the previous password for 10 minutes after rotation.

## High availability

The cluster elects one leader and keeps route maps on all available nodes. See the [high-availability design](high-availability.md) for elections, writes, recovery, and DNS behavior.

Each successful public mutation returns `X-Atlas-Proxy-Generation`. `GET /v1/cluster/status` returns the local node ID, role, leader, term, generation, member count, and readiness state.

## API reference

Use `GET /docs` for the Scalar API reference and `GET /docs/swagger.json` for its OpenAPI schema. Select `BearerAuth` to send the proxy password or a JWT. Scalar removes the credential when the page reloads.

The reference gives request fields, response fields, and examples. It selects cURL by default and lists all other client libraries in More.

## Health

| Route          | Auth | Use                                                    | Result |
| -------------- | ---- | ------------------------------------------------------ | ------ |
| `GET /healthz` | No   | Check that the node can serve traffic.                 | `204`  |
| `GET /readyz`  | No   | Check cluster synchronization and OpenResty readiness. | `204`  |

`/healthz` is the DNS health check. It ignores the leader, so a node that loses quorum keeps its traffic. `/readyz` is the provisioning gate.

## Sites

A site name is one label below the wildcard domain. For example, `erp` routes `erp.iad.frappe.dev` when the wildcard is `*.iad.frappe.dev`. Use `GET /v1/sites` to read the map, `PUT /v1/sites` to replace it, and `PATCH` or `DELETE` on `/v1/sites/<name>` to change one site.

```sh
curl -X PUT \
  -H "Authorization: Bearer $ATLAS_PROXY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"erp":"2001:db8::10","shop":"2001:db8::11"}' \
  "$ATLAS_PROXY_CONTROL_URL/v1/sites"

curl -X PATCH \
  -H "Authorization: Bearer $ATLAS_PROXY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"address":"2001:db8::10"}' \
  "$ATLAS_PROXY_CONTROL_URL/v1/sites/erp"
```

Use `"-"` as a site address to return `503` for that site. Do not use an empty address. The names `proxy`, `proxy-*`, and all configured auto-proxy prefixes return `409` for site mutations.

## Domains

A custom-domain key is a complete customer domain, such as `www.example.com`. The proxy sends its TLS traffic to the site VM without TLS termination. Use `GET /v1/domains` to read the map, `PUT /v1/domains` to replace it, and `PATCH` or `DELETE` on `/v1/domains/<domain>` to change one domain.

```sh
curl -X PUT \
  -H "Authorization: Bearer $ATLAS_PROXY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"www.example.com":"2001:db8::20"}' \
  "$ATLAS_PROXY_CONTROL_URL/v1/domains"
```

A custom-domain map accepts exact domain names only. A domain that equals the regional wildcard zone or sits below it returns `409`. Use the sites API for names in that zone.

## Errors

| Status | Meaning                                                                                     |
| ------ | ------------------------------------------------------------------------------------------- |
| `401`  | The Bearer credential is missing or invalid.                                                |
| `403`  | The credential does not grant access to the resource or name.                               |
| `409`  | A site name is reserved, a domain belongs to the wildcard zone, or cluster state conflicts. |
| `422`  | The request does not match the API schema, or a custom-domain key starts with `*`.           |
| `502`  | OpenResty cannot apply or return a map.                                                     |
| `503`  | The cluster or OpenResty is not ready, or the replication threshold was not met.            |

When a mutation returns `503`, send the desired mutation again. A replacement sends the complete desired map. An update sets one key to one value. A delete succeeds when the key is already absent.
