# Security model

This guide records what protects an Atlas region today, and which decisions are accepted for now. Read it before you change authentication, permissions, or the tenant boundary.

## Trust boundaries

Atlas trusts the central control plane, the Metal hosts it provisions, and its own object storage. It does not trust an API caller, a guest virtual machine, or a proxy viewer.

The Atlas API at `/api/atlas` is the only tenant-facing surface. Metal Servers, provider credentials, and bare metal operations stay with a System Manager.

## Who can call the API

A service caller needs a valid Central or regional Atlas token. Send the token in the `Authorization: Bearer <token>` header. A System Manager can also use the Atlas API.

The authentication validator closes the standard Frappe guest surface. A guest can sign in and read the API reference or public key set. The realtime console stays open because its handshake and permission calls need the web process. A session without a verified token cannot use an Atlas API route.

Atlas requires the `iss`, `sub`, `aud`, `scope`, `tenant`, `iat`, and `exp` claims. It checks `nbf` when the claim is present. The audience is `atlas-admin:<region ID>`. The API surface is unrestricted, so the token needs `scope=*`, a subject, a tenant, and no resource constraint. The subject is an identity label and does not change the authority. The `tenant` claim carries the tenant boundary. Central uses `tenant=*`.

The authentication hook is the one access gate. It signs the caller in, stores the request identity, and refuses a path that the caller may not use. A route function performs no access check of its own.

Atlas binds each key namespace to its issuer. A `central:*` key can validate only `iss=central`. The regional key can validate only `iss=atlas:<region ID>`. A regional token cannot claim Central authority.

Atlas gets Central public keys every 5 minutes. It keeps the last valid key set after a fetch failure. Atlas publishes these keys and its regional public key at `/api/atlas/jwks.json`. An unknown key ID does not cause a fetch.

## Tenant isolation

Each tenant record carries a `tenant_id`. The `tenant` claim of the token decides the tenant of a request. Atlas signs a regional caller in as `tenant-<tenant ID>@atlas.local` and creates that user on first use, so the tenant belongs to the authenticated user and not to a request header. A Central caller (`tenant=*`) is not bound to one tenant and sends `X-Tenant-ID`.

The Frappe permission hooks are the enforcement point. `permission_query_conditions` filters each list query and `has_permission` checks each document, both for `Virtual Machine`, `Virtual Machine Image`, and `Metal Server IP Address`. A list route uses `frappe.get_list`, so the hook applies. `get_owned_document` applies the same rule to a single resource and returns `404` for a record of another tenant. Outside an Atlas API request there is no identity, so a caller without the System Manager role reads nothing.

A System image is the one shared record. Every tenant can read, boot, and download it. Only its owner can write or delete an image it owns.

A snapshot request carries `image_type`. Only tenant `0` may ask for `system`, and only tenant `0` may set the `cache_image` and `memory_snapshot` flags. Any other tenant that sends one of these values receives an error, and the default image type is `machine`. All 3 values are set at creation, and no route changes them later.

Tenant `0` is the system tenant. A privileged virtual machine reaches every tenant through the mesh, so it must use tenant `0`, and only a caller that acts for tenant `0` can create one or hold the privileged flag. An unallocated IP address carries tenant `-1`, so no tenant can read it.

## Accepted risks

**A service token is a bearer token.** Atlas does not track replay. A stolen token works until it expires, so each issuer must use a short lifetime.

**There are no quotas.** A caller can create virtual machines, reserve provider IP addresses, and request any vCPU, memory, and disk size without a limit. Cost and capacity control belong to the central control plane, not to Atlas.

**The API reference page loads a script from a CDN.** `/api/atlas/docs` is unauthenticated and serves Scalar from `cdn.jsdelivr.net` without a pinned version or an integrity hash. The page shares an origin with the site, so a compromised CDN would run in the browser of a signed-in viewer. Pin the version and add an integrity hash, or serve the bundle from the app.

**A signed image download URL lives for 24 hours.** The URL is a bearer capability for that artifact. Atlas sets `Cache-Control: no-store`, but a caller can pass the URL on.

**A console token is a bearer capability.** It is a 48-character value that expires after 60 seconds and is single use. The realtime handler accepts it from a guest, because the token is the only credential the console needs.

## Future work

**The Atlas API has no resource scopes.** A token carries `scope=*` and reaches every tenant API route. The HTTP proxy already binds a token to resource scopes and name constraints. Add the same model here when one caller must reach fewer routes than another.

## Rules for a change

A list route must use `frappe.get_list`. `frappe.get_all` ignores permissions and must not appear in a route.

Keep an API route thin. Put the permission check on the domain object, so a whitelisted method is safe on its own. A route is a second line of defence, not the only one.

Do not return a Frappe message to a caller unless the message is written for a caller. Raise `AtlasUserError` for a message that a caller may read. Every other failure returns a generic message for its status, and Atlas logs the traceback.

Run a queued job as Administrator with `run_as_admin`. A job must not depend on the permissions of the user that enqueued it, and it must not run inside a request.
