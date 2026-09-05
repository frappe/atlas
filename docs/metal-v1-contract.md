# Metal `/v1` controller contract

This reference defines the target Atlas-to-Metal contract. Stage 4 will replace the current unversioned controller routes as one atomic change.

The health and documentation routes stay unversioned. All controller operations use `/v1`.

## Common rules

Metal accepts and returns JSON. Every route except the health and documentation routes needs a bearer token.

Requests use complete field names. Sizes use `mib`. Rates use `mibps`. IOPS values use `iops`.

Metal rejects unknown JSON fields, invalid values, and trailing JSON data. A mutation persists its intent before Metal sends a response.

Metal responses do not contain these values:

- Signed URLs.
- User data.
- Command output.
- Host paths.
- Process IDs.
- Host user IDs.

## Routes and status values

| Method | Path | Success status |
|---|---|---|
| `GET` | `/health` | `200` |
| `GET` | `/docs` | `200` |
| `GET` | `/docs/swagger.json` | `200` |
| `POST` | `/v1/sync` | `200` |
| `PUT` | `/v1/vms/{id}` | `202` |
| `GET` | `/v1/vms` | `200` |
| `GET` | `/v1/vms/{id}` | `200` |
| `PUT` | `/v1/vms/{id}/power` | `202` |
| `POST` | `/v1/vms/{id}/restarts` | `202` |
| `PUT` | `/v1/vms/{id}/compute` | `202` |
| `PUT` | `/v1/vms/{id}/disk` | `202` |
| `PUT` | `/v1/vms/{id}/network` | `202` |
| `PUT` | `/v1/vms/{id}/ssh-keys` | `200` or `202` |
| `PUT` | `/v1/vms/{id}/metadata` | `200` or `202` |
| `DELETE` | `/v1/vms/{id}` | `202` |
| `POST` | `/v1/vms/{id}/snapshots` | `201` |
| `POST` | `/v1/snapshots/{id}/upload` | `202` |
| `GET` | `/v1/snapshots/{id}` | `200` |
| `DELETE` | `/v1/snapshots/{id}` | `204` |
| `GET` | `/v1/vms/{id}/console` | WebSocket upgrade |

Secure Shell key and metadata updates use an immediate operation with a 2-second limit. Metal returns `200` after immediate success. Metal returns `202` when reconciliation must continue.

## Create request

`PUT /v1/vms/{id}` reserves a caller-supplied ID. The first request fingerprint makes retries safe.

```json
{
  "compute": {
    "virtual_cpu_count": 2,
    "memory_mib": 2048
  },
  "disk": {
    "size_mib": 20480,
    "throughput_mibps": 50,
    "iops": 2000
  },
  "image": {
    "ref": "sha256:immutable-reference",
    "architecture": "amd64",
    "rootfs": {
      "url": "signed transport URL",
      "sha256": "64 hexadecimal characters"
    },
    "kernel": {
      "url": "signed transport URL",
      "sha256": "64 hexadecimal characters"
    },
    "cache_image": true,
    "memory_snapshot": false
  },
  "network": {
    "egress": "uplink",
    "public_ipv4": "203.0.113.10",
    "wireguard_mesh_ipv6": "fdaa:1:1::1",
    "private_network_throughput_mibps": 100,
    "public_network_throughput_mibps": 50
  },
  "guest": {
    "hostname": "worker-1",
    "ssh_keys": ["ssh-ed25519 AAAA... user@example"],
    "metadata": {"role": "worker"},
    "user_data": "transport-only create data"
  }
}
```

A retry with the same fingerprint returns the current resource. A request with a different first-request identity returns `409`.

## Virtual machine response

Create, read, and mutation routes return the same nested resource shape.

```json
{
  "id": "vm-00001",
  "desired": {
    "generation": 4,
    "restart_generation": 1,
    "state": "running",
    "compute": {
      "virtual_cpu_count": 2,
      "memory_mib": 2048
    },
    "disk": {
      "size_mib": 20480,
      "throughput_mibps": 50,
      "iops": 2000
    },
    "image": {
      "ref": "sha256:immutable-reference",
      "architecture": "amd64",
      "rootfs": {"sha256": "64 hexadecimal characters"},
      "kernel": {"sha256": "64 hexadecimal characters"},
      "cache_image": true,
      "memory_snapshot": false
    },
    "network": {
      "egress": "uplink",
      "public_ipv4": "203.0.113.10",
      "wireguard_mesh_ipv6": "fdaa:1:1::1",
      "private_network_throughput_mibps": 100,
      "public_network_throughput_mibps": 50
    },
    "guest": {
      "hostname": "worker-1",
      "ssh_keys": ["ssh-ed25519 AAAA... user@example"],
      "metadata": {"role": "worker"}
    }
  },
  "observed": {
    "generation": 3,
    "restart_generation": 1,
    "state": "running",
    "phase": "network",
    "operation_id": "01900000-0000-7000-8000-000000000001",
    "operation_started_at": "2026-09-05T10:00:00Z",
    "updated_at": "2026-09-05T10:00:02Z",
    "disk": {"used_mib": 4096},
    "error": null
  }
}
```

The list route returns an array of these resources. Atlas compares the desired and observed generations to show progress.

## Mutation requests

Power replaces the desired lifecycle state:

```json
{"state": "stopped"}
```

Valid states are `running`, `stopped`, and `paused`. Delete records the desired destroyed state.

Restart has no request body. Each accepted request increases `restart_generation`.

Compute replaces the complete compute object:

```json
{"virtual_cpu_count": 4, "memory_mib": 4096}
```

Disk replaces the complete mutable disk object. Metal rejects disk shrink requests.

```json
{"size_mib": 40960, "throughput_mibps": 100, "iops": 4000}
```

Network replaces the complete network object:

```json
{
  "egress": "mesh",
  "public_ipv4": "",
  "wireguard_mesh_ipv6": "fdaa:1:1::1",
  "private_network_throughput_mibps": 100,
  "public_network_throughput_mibps": 0
}
```

Secure Shell keys and metadata also use complete replacement.

```json
{"ssh_keys": ["ssh-ed25519 AAAA... user@example"]}
```

```json
{"metadata": {"role": "worker"}}
```

## Snapshot requests

Snapshot creation has no request body. It returns the staging identity and exact artifact sizes.

```json
{
  "id": "01900000-0000-7000-8000-000000000001",
  "rootfs": {"size_bytes": 21474836480},
  "kernel": {"size_bytes": 33554432}
}
```

The upload request supplies consecutive multipart URLs. Metal does not return these URLs.

The snapshot status reports `pending`, `uploading`, `completing`, `completed`, or `failed`. A completed response contains part numbers, ETags, sizes, and SHA-256 values.

## Synchronization

`POST /v1/sync` replaces the complete WireGuard peer, cached image, and privileged address sets. Empty arrays remove all managed values.

The response contains current host capacity. CPU, memory, storage, and virtual machine counts use complete field names and binary units.

## Errors

Every HTTP error uses one safe object:

```json
{
  "error": {
    "code": "conflict",
    "message": "the virtual machine specification conflicts with the first request",
    "retryable": false
  }
}
```

| HTTP status | Code | Meaning |
|---|---|---|
| `400` | `invalid_request` | The request syntax or value is invalid. |
| `401` | `unauthorized` | Authentication failed. |
| `404` | `not_found` | The resource does not exist. |
| `409` | `conflict` | Current state or immutable identity blocks the request. |
| `409` | `image_content_conflict` | An image reference identifies different content. |
| `422` | `image_integrity_failed` | Downloaded image data failed verification. |
| `500` | `internal_error` | Metal failed and did not expose a host error. |
| `501` | `not_implemented` | The runtime does not implement the operation. |

Atlas uses the HTTP status, `code`, and `retryable` value at its Frappe boundary. A transport failure after a write can also mark an Atlas request as uncertain.
