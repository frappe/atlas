# Tenant API and identity

Tenants and Central use the tenant API to manage VMs, images, public IPs, and state webhooks in one region. Host and provider administration stay in Desk.

This page covers tenant scope and response conventions. [Signing keys and tokens](signing-keys.md) explains the credential format and regional key set. **For each operation, its fields, and its errors, use the [Atlas API reference ↗](/api/atlas/).** A running site also serves it at `/api/atlas/docs`.

## Request authentication

Send service credentials as `Authorization: Bearer <token>`. A System Manager session can also use the API.

The authentication hook validates identity and allowed paths before route execution. Frappe permissions restrict record access. Guests can read only the API docs and public key set. Realtime consoles use their own token.

## Tenant scope

The signed `tenant` claim sets the boundary. Regional callers run as their tenant's Frappe user. `X-Tenant-ID` is an unsigned 32-bit integer.

| Caller | `X-Tenant-ID` rule |
| --- | --- |
| Regional token, such as `tenant=7` | Must match the signed tenant. A different value returns `400`. |
| Central token with `tenant=*` | Required on tenant routes. Missing or invalid values return `400`. |
| System Manager session | Required on tenant routes. |

Central-wide routes, such as `PUT /api/atlas/webhooks`, need no tenant header. Regional tokens receive `403` there. Another tenant's resource returns `404`. Tenant responses include `tenant_id`.

### System tenant

Only callers acting for **tenant `0`** can:

- Create a privileged VM or System image.
- Set snapshot `image_type=system`.
- Set snapshot `cache_image` or `memory_snapshot`.

Other tenants receive `400`. These image values cannot change after creation.

## Conventions

| Area | Rule |
| --- | --- |
| Error body | `error.code`, `error.message`, `error.fields`. |
| Error status | `400` validation, `401` unauthenticated, `403` denied, `404` missing, `409` invalid state. |
| Host work | `202`. Poll the resource for completion. |
| Lists | `items`, `offset`, `limit`, `has_more`. Default limit 20. Maximum 100. |
| Tags | `tag=key:value,key:value`. Every pair must match. |
| Updates | `PATCH` changes sent fields and needs at least one. `PUT` replaces the value. |
| Time | Unix seconds for timestamps. Seconds for durations. |

A create or resize that cannot place the VM returns `503`:

| Code | Action |
| --- | --- |
| `out_of_capacity` | Retry later when capacity is available. |
| `placement_busy` | Retry after the response's `Retry-After` interval. |
| `affinity_unsatisfied` | No host with room meets the affinity rules of the VM. Change the rules, or add a host that meets them. |

See [host selection](../compute/placement.md) for capacity rules.

## Limits and recovery

Host work returns `202` before it finishes. A timeout can leave accepted work in progress, so read the resource again before you retry with different values. [VM state updates](vm-state-updates.md) carry the latest host report, which can be older than the delivery time.

Service tokens have no route scopes. Read the [security model](security.md) before issuing one. To add an endpoint, see [Add a route](../develop/atlas-app.md#add-a-route).

::: details Source code and tests

- [API router](../../atlas/api/router.py) owns route registration and resource groups.
- [API framework](../../atlas/api/core/base.py) owns route handling, with request decoding in `binding.py`.
- [Request authentication](../../atlas/auth/request.py) and [identity handling](../../atlas/auth/identity.py) own caller and tenant rules.
- [Token validation](../../atlas/auth/token.py), [key-set handling](../../atlas/auth/jwks.py), and [regional issuer](../../atlas/auth/issuer.py) own service credentials.
- [Token tests](../../atlas/auth/test_token.py) and [identity tests](../../atlas/auth/test_identity.py) show access rules.

:::
