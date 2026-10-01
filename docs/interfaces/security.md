# Trust and security model

Each control interface checks caller identity. Guests are outside the control boundary. Network access alone grants no authority.

## Who can call each interface

| Caller | Interface | Credential |
| --- | --- | --- |
| Central or regional service | Atlas tenant API | EdDSA bearer token with the Atlas audience, `scope=*`, and signed tenant claim. |
| System Manager | Atlas tenant API and Desk | Frappe session with the System Manager role. |
| Atlas | Metal control API | Regional certificate and pinned Atlas client common name, checked through mutual TLS. |
| Metal node | Coordination API and migration stream | Any certificate signed by the regional authority. |
| Central, Atlas, or Cargo | Proxy control API | Bearer password or issuer-bound token with route scopes and optional name constraints. |
| Atlas | Cargo API | Token for the regional Cargo audience. Cargo's validator is outside this repository. |
| Proxy peer | Internal cluster API | Regional cluster password. |
| Browser | Atlas realtime bridge | Short-lived, single-use console token. |

### Atlas and proxy tokens

[Signing keys and tokens](signing-keys.md) explains the combined public key set, token structure, and separate Atlas, proxy, and Cargo audiences. [Tenant identity](tenant-api.md) defines record access, and [proxy authentication](../networking/http-proxy/control-daemon.md#authentication) defines route access. Proxy control requests fail authentication when valid credentials are not configured.

### Metal listeners

Metal uses TLS 1.3 with no shared secret.

| Installed listener | Port | Authority |
| --- | --- | --- |
| Control | `9000` | Regional CA certificate plus the Atlas client common name. |
| Coordination | `9001` | Any regional node certificate. Source-side migration routes only. |
| One-shot snapshot stream | `9002` | Any regional node certificate. |

All three listeners bind only to the WireGuard address. The host firewall admits `9000` only from the Atlas WireGuard address and `9001` and `9002` only from host addresses. See [Atlas access to hosts](../region/host-access.md#host-firewall). The destination also checks the source server certificate against the source address.

### Certificate lifetime

Atlas owns one private certificate authority per region. Node certificates support client and server authentication. They identify the node and its WireGuard, private, and public addresses.

| Certificate | Lifetime | Renewal |
| --- | --- | --- |
| Node | 825 days | Atlas renews within 30 days of expiry and restarts Metal. |
| Regional authority | 10 years | No rotation. Atlas reports an error within 180 days of expiry. |

## Tenant and guest boundaries

### Atlas records

Frappe permission hooks enforce tenant access:

- `permission_query_conditions` filters lists.
- `has_permission` checks VM, image, and public IP allocation documents.
- A request for another tenant's record returns `404`.
- Outside an Atlas API identity, callers without System Manager access read nothing.

System images are shared reads. An Available Public IP Allocation has no tenant, so tenants cannot read it.

### Guests and packets

Metal gives each guest its own user ID, Firecracker jail, and Linux network namespace. An enabled guest firewall filters forwarded traffic there.

WG Mesh checks source ownership and tenant IDs in eBPF. A privileged tenant-0 VM can cross tenant boundaries only when Atlas includes its address in the privileged set.

Treat guest user data, metadata, and packets as untrusted input.

## Secret and console handling

Atlas stores regional keys and provider credentials in Frappe Password fields. It writes Metal client credentials to private site files for TLS connections. Node and proxy credentials go to their host configurations.

A console connection uses a one-use Redis token. [Console access](../compute/console.md) explains its lifetime, permission check, and bridge.

## Rules for a change

1. Use `frappe.get_list` in list routes. `frappe.get_all` bypasses permissions.
2. Check permissions on the domain object so each whitelisted method is safe on its own.
3. Use `AtlasUserError` only for caller-safe messages. Other failures return a generic message and log the traceback.
4. Run queued work as Administrator with `run_as_admin`. Do not depend on the initiating user's permissions.

## Limitations

| Accepted limit | Effect |
| --- | --- |
| Bearer service tokens | No replay tracking. A stolen token works until expiry. Issuers must use short lifetimes. |
| Atlas `scope=*` | Service tokens can reach every tenant API route within their tenant authority. |
| No Atlas tenant quotas | VM validation limits one VM. Central owns aggregate capacity and cost control. |
| Signed image URLs last 24 hours | Anyone with the URL can access that artifact. |
| Console token is a bearer credential | Anyone who holds a valid token can use it once before it expires. See [console access](../compute/console.md). |
| Regional node authority | A node in the mesh can call another node's migration source routes. |
| Live Atlas API docs load Scalar from a CDN | `/api/atlas/docs` loads an unpinned script without an integrity hash on the site origin. |

::: details Source code and tests

- [Atlas request authentication](../../atlas/auth/request.py), [token validator](../../atlas/auth/token.py), and [tenant permission hooks](../../atlas/auth/overrides.py) own Atlas API identity.
- [Metal TLS setup](../../metal/cmd/metald/tls.go) owns listener certificate checks.
- [Atlas Metal certificates](../../atlas/atlas/core/tls/metal.py) owns regional issuance and private client files.
- [Proxy authentication](../../services/http-proxy/control/proxy_control/auth.py) owns route API credentials and scopes.
- [Mesh tenant checks](../../services/wg-mesh/bpf/maps.h) own private packet isolation.
- [Console token](../../atlas/vm/core/console_token.py) and [realtime bridge](../../atlas/realtime/handlers.py) own browser console access.
- [Atlas identity tests](../../atlas/auth/test_identity.py), [Metal TLS tests](../../metal/cmd/metald/tls_test.go), and [console tests](../../atlas/realtime/test_handlers.py) show the intended access boundaries.

:::
