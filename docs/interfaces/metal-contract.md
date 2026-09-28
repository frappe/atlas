# Atlas to Metal API rules

This page lists the rules the Atlas app follows when it calls Metal. It does not repeat routes or fields. **Use the [Metal API reference ↗](/api/metal/) for every operation and schema.** For listeners and TLS, see [Metal daemon and API](../region/metald.md).

## Common rules

- All controller routes use `/v1`. Health and documentation routes are unversioned.
- Every request uses mutual TLS with the Atlas client certificate.
- Requests and responses are JSON. Metal rejects unknown fields, invalid values, and trailing data.
- Field names name their unit: sizes in `mib`, rates in `mibps`, IOPS in `iops`.
- Responses never contain signed URLs, user data, command output, host paths, process IDs, or host user IDs.

## Completion

Metal saves the requested change before it replies. Most changes return `202 Accepted`, and host work continues after the reply. **Compare the desired and observed generations to know when the host applied a change.** Read `observed.error` when the generations stop moving.

SSH key and metadata updates try to finish within 2 seconds. They return `200` on success, or `202` when reconciliation must continue.

## Create and retry

`PUT /v1/vms/{id}` reserves an ID that the Atlas app chooses. Metal stores a fingerprint of the first request.

| Retry | Result |
| --- | --- |
| Same ID and same request | Returns the current resource. Safe to repeat. |
| Same ID and a different request | `409 conflict`. |

Renewed signed image URLs do not change the fingerprint.

Set `disk_encryption` to `luks2` only when `image.initrd` supplies an initrd URL and SHA-256 digest. Metal rejects an encryption request without the initrd. The guest initrd derives the plaintext filesystem boundary, so the create request has no host-calculated boundary value.

See [guest disk encryption](../compute/disk-encryption.md) for the first-boot conversion, console, sleep, and recovery rules.

## Updates replace, not merge

Power, compute, disk, network, SSH keys, and metadata each replace their complete object. Send the full value, not a change.

The complete value is the state Metal saves and reapplies after a restart or failed host step. A retry does not depend on a chain of earlier updates.

| Rule | Detail |
| --- | --- |
| CPU | `cpu_millicores` from `100` to `32000`. `1000` is one core. The guest gets whole vCPUs, rounded up. |
| CPU or memory change | Needs a stopped VM. The idle timeout can change in any state. |
| Disk | Grows only. A shrink is rejected. |
| Guest metadata | See [guest metadata limits](../compute/vm-records.md#guest-metadata). |
| Resize | Changes CPU, memory, disk, and idle timeout as one shape, after a capacity check. |
| Capacity | An increase that the host cannot hold returns `409 insufficient_capacity`. |
| Restart | Each accepted restart increases `restart_generation`. |

Network rules (routes, public addresses, firewall) are in [Metal networking](../networking/host-networking.md).

## Snapshot upload

The Atlas app sends signed multipart URLs and an `upload_id` for each artifact. Metal never returns those URLs.

- Progress belongs to the upload ID. A resumed upload sends only the missing parts.
- Metal compresses each artifact with zstd, so it can use fewer parts than signed. Atlas signs one spare part.
- `size_bytes` is the uncompressed size. `stored_size_bytes` is the stored size. The SHA-256 digest covers the uncompressed data.
- A reader detects compression from the content: a zstd artifact starts with `28 B5 2F FD`.

## Host sync

`POST /v1/sync` replaces the complete WireGuard peer, cached image, and privileged VM sets. An empty array removes all managed values. Each peer needs a public IPv4 address and a private network MAC address. Invalid peer data returns `400`.

The response reports capacity and each VM's last observed status. CPU values rank hosts. They do not block the capacity check. The status is as fresh as the last reconcile pass.

## Errors

Every error is `{"error": {"code", "message", "retryable", "request_id"}}`. `retryable` says whether a retry can succeed, and `request_id` matches the Metal log entry.

| Status | Code | Meaning |
| --- | --- | --- |
| `400` | `invalid_request` | Invalid syntax or value. |
| `403` | `forbidden` | The request is not allowed in the current state. |
| `404` | `not_found` | The resource does not exist. |
| `409` | `conflict` | Current state or the first request's identity blocks the request. |
| `409` | `insufficient_capacity` | An increase does not fit on this host. |
| `409` | `image_content_conflict` | An image reference names different content. |
| `422` | `image_integrity_failed` | Downloaded image data failed verification. |
| `500` | `internal_error` | Metal failed and hides the host detail. |
| `501` | `not_implemented` | The runtime does not support the operation. |
| `503` | `unavailable` | Metal cannot serve the request now. Retry. |

A transport failure after a write leaves the result unknown. Read the resource before you retry with different values.
