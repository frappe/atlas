# Tenant API

Atlas exposes a tenant control plane at `/api/atlas` for virtual machines, public IPv4 addresses, and virtual machine images. It does not expose Metal Servers or bare metal operations.

Use `GET /api/atlas/docs` to explore every route, request body, and response. The OpenAPI document is at `GET /api/atlas/docs/openapi.json`. This guide describes the rules that apply to every route, and how to add a route.

## Access

Each service request needs a valid JSON Web Token in the `Authorization: Bearer <token>` header. A System Manager can also use these routes.

Atlas accepts `iss=central` and `iss=atlas:<region ID>`. The token audience must be `atlas-admin:<region ID>`. Atlas requires the `iss`, `sub`, `aud`, `scope`, `tenant`, `iat`, and `exp` claims. Atlas also checks `nbf` when the claim is present.

A token needs `scope=*`, a subject, a tenant, and no resource constraint. The subject is an identity label and does not change the authority. Central uses `tenant=*`. The Atlas scope applies only to tenant API routes. It does not grant access to global administration routes.

A regional token names its tenant, and the header is unnecessary:

```json
{
  "iss": "atlas:1",
  "sub": "cargo",
  "aud": "atlas-admin:1",
  "scope": "*",
  "tenant": "7",
  "iat": 1789072323,
  "nbf": 1789072323,
  "exp": 1789072623
}
```

A Central token serves every tenant, so each request must also send `X-Tenant-ID`:

```json
{
  "iss": "central",
  "sub": "central",
  "aud": "atlas-admin:1",
  "scope": "*",
  "tenant": "*",
  "iat": 1789072323,
  "nbf": 1789072323,
  "exp": 1789072623
}
```

The header of both tokens is `{"alg": "EdDSA", "kid": "atlas:1:<key ID>"}`, and `kid` uses the `central:` namespace for a Central token.

Atlas binds each key namespace to its issuer. A `central:*` key can validate only `iss=central`. An `atlas:<region ID>:*` key can validate only the matching regional issuer. Atlas refuses a key from another region.

The public key set is at `GET /api/atlas/jwks.json`. Atlas gets Central keys every 5 minutes and keeps the last valid set after a fetch failure. The endpoint also contains the regional Atlas public key. The response does not contain a private key.

An `/api/atlas` route requires a verified token or a System Manager. The authentication hook is the only access gate, and it runs before Frappe matches the route. A session without a verified token cannot use the API. A guest can read the API reference and the public key set. The realtime console paths stay open to every user.

## Tenant

The `tenant` claim of the token decides the tenant boundary. Atlas signs each regional caller in as the Frappe user of its tenant, and Frappe permissions then filter every read and write.

Send `X-Tenant-ID` on every request with an unsigned 32-bit integer from 0 through 4294967295. A regional token must send the tenant of its own claim, and a header that names another tenant returns `400`. A Central token (`tenant=*`) is not bound to one tenant, so the header names the tenant it acts for. A System Manager uses the same header.

| Caller | `X-Tenant-ID` |
|---|---|
| Regional token, such as `tenant=7` | Required by this contract. It must match the token tenant, and a different value returns `400`. |
| Central token (`tenant=*`) | Required. It names the tenant of the request, and a missing or invalid value returns `400`. |
| System Manager session | Required, and the request is limited to that tenant. |

Tenant `0` is the system tenant. A token with `tenant=0`, or a Central token with `X-Tenant-ID: 0`, uses every route and can create a privileged virtual machine and a System image. An unallocated IP address carries tenant `-1` and stays outside every tenant. A request for another tenant's resource returns `404`. Each resource response carries `tenant_id`.

A snapshot request accepts `image_type=system`, `cache_image`, and `memory_snapshot` only from tenant `0`. Any other tenant that sends one receives `400`, and its snapshot becomes a `machine` image. These values cannot change after the image exists.

## Conventions

An error response has `error.code`, `error.message`, and `error.fields`. Validation errors use `400`; missing authentication uses `401`; denied access uses `403`; missing resources use `404`; and invalid resource state uses `409`.

A list response carries `items`, `offset`, `limit`, and `has_more`. The default limit is 20 and the maximum limit is 100.

A `PATCH` request needs at least one supported field. A field that is not in the request does not change. A `PUT` request replaces the complete stored value.

A route that asks the host for work returns `202`. Poll the resource route for completion.

All absolute time fields use Unix timestamps in seconds. Duration fields such as `expires_in` also use seconds.

## Behavior

Use [the virtual machine specification](../vm/SPEC.md) for the virtual machine and image rules. Use [the image guide](images.md) for the image lifecycle. Use [the Metal Server specification](../metal_server/SPEC.md) for IP address reservation and release.

## Layout

`atlas/api/core/` holds the typed HTTP framework. `atlas/api/router.py`, `atlas/api/models.py`, and `atlas/api/routes/` hold the Atlas API. `atlas/auth/request.py` owns request authentication. `atlas/auth/token.py` validates tokens. `atlas/auth/jwks.py` owns the merged key set. `atlas/auth/issuer.py` creates regional tokens. The other files in `atlas/auth/` own users, roles, tenants, and permission overrides.

`atlas.api.router.register_atlas_api` imports every route module. The `before_request` hook calls it, so the routes exist before Frappe matches an API request.

## Add a route

A router owns one path prefix below `/api`. Add a resource group in `atlas/api/router.py`, add its routes to a matching file in `atlas/api/routes/`, and import that file in `register_atlas_api`.

```python
from pydantic import BaseModel

from atlas.api.core.base import ApiResult, StrictModel
from atlas.api.core.docs import api_docs
from atlas.api.router import machines


class MachinePayload(StrictModel):
	name: str
	cores: int = 1


class MachineResponse(BaseModel):
	id: str
	name: str
	cores: int


@machines.post("")
@api_docs(
	request_example={"name": "vm-1", "cores": 4},
	responses={201: {"description": "The machine is created."}},
)
def create_machine(payload: MachinePayload) -> ApiResult[MachineResponse]:
	"""Create a machine."""
	machine = MachineResponse(id="machine-1", name=payload.name, cores=payload.cores)
	return ApiResult(machine, status=201)
```

A route function reads request data through two reserved parameter names. Each value must be a Pydantic model or a list of one Pydantic model. The router rejects raw dictionaries, raw lists, scalar values, unions, and missing annotations when it registers a route. Atlas documents the optional `X-Tenant-ID` header on each operation. Central tokens and System Manager sessions must send it.

- `payload` decodes the JSON request body. A route on `GET` or `HEAD` cannot declare it.
- `query` decodes the query string. Pydantic converts each string value to the annotated type.
- A path parameter uses the name in the route pattern, such as `virtual_machine_id`.

Return a Pydantic model for `200`. Return `ApiResult[Pydantic model]` when the route needs another status or response header. Return `None` for `204`.

Keep the route thin. Pass validated values to the domain service. Do not pass a request dictionary to the Frappe ORM. Use the first docstring line as the OpenAPI summary and the remaining text as the description. Use `api_docs` only for examples and route-specific response codes.
