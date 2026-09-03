# Metal HTTP API

metald serves JSON on its configured listener. It generates the OpenAPI document from handler annotations and serves the document at `/docs`.

All routes except `/docs` and `/docs/swagger.json` require a bearer token. `metald.auth_token_hash` must contain the lowercase SHA-256 digest of this token.

```http
Authorization: Bearer <token>
```

## Virtual machines

VM create is an idempotent resource operation.

```http
PUT /vms/{id}
Content-Type: application/json
```

```json
{
  "vcpus": 2,
  "memory_mib": 512,
  "disk_mib": 2048,
  "image": {
    "ref": "ubuntu-24.04-1",
    "architecture": "amd64",
    "rootfs": {
      "url": "https://images.example/rootfs?signature=...",
      "sha256": "64 hexadecimal characters"
    },
    "kernel": {
      "url": "https://images.example/vmlinux?signature=...",
      "sha256": "64 hexadecimal characters"
    },
    "cache_image": true,
    "memory_snapshot": true,
    "memory_snapshot_configuration": {
      "virtual_cpu_count": 2,
      "memory_mib": 512,
      "disk_mib": 2048
    }
  },
  "hostname": "worker-1",
  "ssh_keys": ["ssh-ed25519 ..."],
  "user_data": "...",
  "network": {
    "public_ipv4": "203.0.113.10",
    "wireguard_mesh_ipv6": "fdaa:1:0:7::1",
    "egress": "host"
  }
}
```

The first request reserves the supplied ID and returns `202`. A repeat request for the same ID and specification also returns `202`. A request that reuses the ID with a different specification returns `409`. The background reconciler starts the VM. The response can report `state: "unknown"` until the driver observes the VM.

Metal stores one immutable manifest for each `image.ref`. The manifest contains the root file system digest, kernel digest, and architecture. Metal verifies downloads before import. A request that reuses an image reference with different content returns `409`.

VM responses contain immutable image data:

```json
{
  "image": {
    "ref": "ubuntu-24.04-1",
    "architecture": "amd64",
    "rootfs": {"sha256": "64 hexadecimal characters"},
    "kernel": {"sha256": "64 hexadecimal characters"}
  },
  "ssh_keys": ["ssh-ed25519 AAAA... user@example"]
}
```

They do not contain image transport URLs, the internal guest IP, or the Firecracker PID. The MAC address is in the `network` object.

| Method | Path | Action |
|---|---|---|
| `PUT` | `/vms/{id}` | create or confirm a VM reservation |
| `GET` | `/vms` | list VMs |
| `GET` | `/vms/{id}` | get one VM |
| `PUT` | `/vms/{id}/ssh-keys` | replace all authorized SSH keys |
| `POST` | `/vms/{id}/actions/start` | request the running state |
| `POST` | `/vms/{id}/actions/stop` | request the stopped state |
| `POST` | `/vms/{id}/actions/pause` | request the paused state |
| `POST` | `/vms/{id}/actions/resume` | request the running state |
| `POST` | `/vms/{id}/actions/terminate` | request destruction |
| `POST` | `/vms/{id}/resize/compute` | change stopped VM compute size and request a boot |
| `POST` | `/vms/{id}/resize/disk` | grow the VM disk |
| `GET` | `/vms/{id}/console` | return `501` |

Lifecycle requests return `202`. Poll `GET /vms/{id}` until `state` reaches `desired_state`.

`PUT /vms/{id}/ssh-keys` accepts the complete desired `ssh_keys` list and returns the updated VM. An empty list removes all keys. Running and paused VMs receive updated MMDS immediately. Stopped VMs use the keys at their next boot. VM list and info responses include the stored keys.

## Image staging

Metal does not expose image list, image delete, or in-place snapshot restore APIs. Atlas uses a VM disk checkpoint to create a new immutable image.

| Method | Path | Action |
|---|---|---|
| `POST` | `/vms/{id}/snapshots` | create local rootfs and kernel staging with a new UUIDv7 |
| `POST` | `/snapshots/{snapshot_id}/upload` | upload staged artifacts with signed multipart URLs |
| `DELETE` | `/snapshots/{snapshot_id}` | remove local staging |

The create response contains the Metal-generated snapshot ID and the exact rootfs and kernel sizes. Atlas uses the ID as its Machine image document name. Upload parts are 2 GiB, except for the final part. Metal streams each part and returns its S3 ETag plus the SHA-256 value of each complete artifact. Atlas completes the multipart uploads, then deletes local staging.

Metal updates snapshot activity when an upload starts or finishes. The image reconciler deletes local staging after 48 hours without activity.

A staging snapshot is only an image-transfer resource. It cannot roll back its source VM.

## Controller exchange

`POST /sync` applies controller state and returns host state. `wireguard_peers` and `images` are required. Send an empty list to remove all managed remote peers or cached-image policies.

```json
{
  "wireguard_peers": [
    {
      "node": "node-2",
      "node_id": 2,
      "public_key": "base64 WireGuard public key",
      "address": "192.0.2.2:51820"
    }
  ],
  "images": [
    {
      "ref": "sha256:immutable-reference",
      "architecture": "amd64",
      "rootfs": {"url": "https://images.example/rootfs?...", "sha256": "..."},
      "kernel": {"url": "https://images.example/kernel?...", "sha256": "..."},
      "cache_image": true,
      "memory_snapshot": false
    }
  ]
}
```

```json
{
  "capacity": {
    "total_cpu_count": 32,
    "available_cpu_count": 24,
    "virtual_machine_count": 4,
    "total_memory_mib": 131072,
    "available_memory_mib": 81920,
    "total_storage_mib": 3662109,
    "available_storage_mib": 2288818
  }
}
```

Node names, node IDs, public keys, and image references must be unique. Metal saves both desired sets atomically and reconciles images outside the request. Cached images remain local. Other images are pruned after 24 hours without a successful VM start when no dependent disk exists. A cached memory-snapshot image uses local warm artifacts only when CPU, memory, and disk match exactly.

## Errors

Errors have a stable code and a safe public message.

```json
{
  "error": {
    "code": "not_found",
    "message": "resource not found"
  }
}
```

Common codes are `invalid_request`, `unauthorized`, `not_found`, `conflict`, `image_content_conflict`, `image_integrity_failed`, `internal_error`, and `not_implemented`. Internal command errors and signed URL query values are not included in responses.
