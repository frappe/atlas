# api: Metal HTTP server

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · Behavior: [controller contract](../../../docs/interfaces/metal-contract.md) and [metald](../../../docs/region/metald.md)

## Purpose

This package owns request validation, the public response shape, and the error-to-status mapping. It owns no VM, image, or host state. A mutation stores desired state, wakes the reconciler, and returns without waiting for the host.

## Types

- `New(Config, Dependencies)` returns the Atlas API router.
- `NewCoordination` returns the node coordination router.
- `Server` holds the injected services and handlers.
- `Dependencies` holds the injected services. Each is an interface declared in this package.

## Request flow

Each request goes through these steps in order:

1. Add the correlation IDs.
2. Write the request log.
3. Authenticate the route group.
4. Decode strict JSON in the handler.
5. Validate the request.
6. Call the domain service.
7. Wake the reconciler when needed.
8. Write the response.

- Each request gets an `X-Request-ID` and an `X-Operation-ID`. A caller value is kept only when it is safe to echo.
- JSON decoding rejects unknown fields and trailing values.
- Request bodies are size capped.

## Routes

The generated [Metal API reference](../../../docs/region/metald.md#request-rules) lists each route. Rules:

- Use PUT when a request replaces desired state, so a repeat is safe.
- Use POST only for an action that runs on each call, such as a restart, or to create a resource.
- The rescue PUT requires an explicit `enabled` boolean and a validated cold image when enabling. Desired responses include the pinned image identity without signed URLs. Desired and observed rescue generations expose transition progress. See [rescue mode](../../../docs/compute/rescue.md).
- The network PUT requires the complete network object, including `firewall`.
- Every coordination route carries the VM ID as the `virtual_machine_id` query value.
- The coordination stop route is idempotent. It returns the final snapshot.

| Listener | Caller | Routes |
|---|---|---|
| Control | Atlas client certificate | `/v1/sync`, `/v1/vms`, `/v1/snapshots`, destination `/v1/migrations/:id` |
| Coordination | Regional node certificate | Source routes under `/v1/migrations/:id` |
| Snapshot stream | One-shot mutual TLS | Owned by [vm/migration](../vm/migration/SPEC.md) |

## Capacity

Compute and disk handlers check only the increase against host memory and storage. CPU is never checked. See the [controller contract](../../../docs/interfaces/metal-contract.md#updates-replace-not-merge).

## Authentication

- Each route group names its middleware, so a route cannot inherit the wrong rule.
- The TLS listeners verify the client certificate. This package holds no credential.
- An unknown path or a wrong method under `/v1` returns `404`.

See [security](../../../docs/interfaces/security.md) for the certificate that each listener accepts.

## Errors

A domain error maps to one status and one safe message. An unknown error becomes `500` with no detail, and the log keeps its cause under the request ID.

| Domain error | Status | Code |
|---|---|---|
| `vm.ErrNotFound`, `storage.ErrNotFound` | `404` | `not_found` |
| `vm.ErrConflict`, `storage.ErrInUse` | `409` | `conflict` |
| Compute or disk increase without host capacity | `409` | `insufficient_capacity` |
| `storage.ErrImageConflict` | `409` | `image_content_conflict` |
| `storage.ErrImageIntegrity` | `422` | `image_integrity_failed` |
| `storage.ErrShuttingDown` | `503` | `unavailable` |
| `network.ErrInvalidPeers`, `storage.ErrInvalidUpload`, `vm.ErrMetadataServiceTooLarge` | `400` | `invalid_request` |

- Every error body carries a `retryable` flag. `501` is never retryable.
- `POST /v1/sync` maps a host-state not-found to `503`, not `404`. A `404` would stop the controller retry.
- Responses never carry command output, signed URL values, or local error detail.

## Related

- [internal/vm/SPEC.md](../vm/SPEC.md) defines the VM contracts and the generation model.
- [internal/console/SPEC.md](../console/SPEC.md) owns the serial console.
- [cmd/metald/SPEC.md](../../cmd/metald/SPEC.md) injects server dependencies.
