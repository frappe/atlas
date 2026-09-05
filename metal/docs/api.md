# Metal HTTP API

metald serves JSON on its configured listener. It generates the OpenAPI document from handler annotations and serves the document at `/docs`.

All routes except `/docs` and `/docs/swagger.json` require a bearer token. `metald.auth_token_hash` must contain the lowercase SHA-256 digest of this token.

```http
Authorization: Bearer <token>
```

## Service routes

| Method | Path | Action |
|---|---|---|
| `GET` | `/health` | return `200` when the API is available |
| `POST` | `/sync` | apply controller state and return host capacity |
| `GET` | `/docs` | serve the API documentation |
| `GET` | `/docs/swagger.json` | serve the OpenAPI document |

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
    "private_network_throughput_mbps": 100,
    "public_network_throughput_mbps": 50,
    "egress": "uplink"
  }
}
```

The first request reserves the supplied ID and returns `202`. A repeat request for the same ID and specification also returns `202`. A request that changes reservation identity for the same ID returns `409`. The background reconciler starts the VM. The response can report `state: "unknown"` until the driver observes the VM.

Metal stores one immutable manifest for each `image.ref`. The manifest contains the root file system digest, kernel digest, and architecture. Metal verifies downloads before import. A request that reuses an image reference with different content returns `409`.

VM responses contain image identity and cache policy:

```json
{
  "image": {
    "ref": "ubuntu-24.04-1",
    "architecture": "amd64",
    "rootfs": {"sha256": "64 hexadecimal characters"},
    "kernel": {"sha256": "64 hexadecimal characters"},
    "cache_image": true,
    "memory_snapshot": true,
    "memory_snapshot_configuration": {
      "virtual_cpu_count": 2,
      "memory_mib": 512,
      "disk_mib": 2048
    }
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
| `PUT` | `/vms/{id}/network` | update mutable network settings |
| `PUT` | `/vms/{id}/ssh-keys` | replace all authorized SSH keys |
| `POST` | `/vms/{id}/actions/start` | request the running state |
| `POST` | `/vms/{id}/actions/stop` | request the stopped state |
| `POST` | `/vms/{id}/actions/pause` | request the paused state |
| `POST` | `/vms/{id}/actions/resume` | request the running state |
| `POST` | `/vms/{id}/actions/terminate` | request destruction |
| `POST` | `/vms/{id}/resize/compute` | change stopped VM compute size and request a boot |
| `POST` | `/vms/{id}/resize/disk` | grow the VM disk |
| `GET` | `/vms/{id}/console` | open the serial console websocket |

Lifecycle requests return `202`. Poll `GET /vms/{id}` until `state` reaches `desired_state`.

`PUT /vms/{id}/ssh-keys` accepts the complete desired `ssh_keys` list and returns the updated VM. An empty list removes all keys. The list can contain at most 100 unique OpenSSH keys. Each key must use one line and be at most 16 KiB. Running and paused VMs receive updated MMDS immediately. Stopped VMs use the keys at their next boot.

`PUT /vms/{id}/network` replaces mutable network settings without a VM restart and returns the updated VM.

```json
{
  "egress": "uplink",
  "public_ipv4": "203.0.113.10",
  "private_network_throughput_mbps": 100,
  "public_network_throughput_mbps": 50
}
```

`egress` controls internet reachability. It does not control mesh reachability.

| Value | VM can reach | Public IPv4 | Throughput limits |
|---|---|---|---|
| `uplink` | mesh peers and the internet | allowed | private and public |
| `mesh` | mesh peers only | rejected | private applied, public stored |
| `none` | nothing | rejected | none |

`egress` is required.

Use an empty `public_ipv4` to detach the address. Throughput values apply in both directions. `0` removes a limit. Metal keeps a limit that the mode does not permit and applies it when the mode permits it. Active connections can stop when the public IPv4 address or the egress mode changes.

`GET /vms/{id}/console` upgrades the request to a websocket for the serial console. The bearer token guards the handshake. Metal sends console output as binary frames. A viewer sends keystrokes as binary frames and a terminal resize as a text frame `{"resize":{"cols":80,"rows":24}}`. New viewers first receive the recent scrollback. Metal keeps the console open only while the VM runs. After a metald restart, the console is unavailable until the VM starts again, and the socket closes with a going-away status.

## Image staging

Metal uses a VM disk checkpoint to create a new immutable image. It does not expose image list, delete, or restore APIs.

| Method | Path | Action |
|---|---|---|
| `POST` | `/vms/{id}/snapshots` | create local rootfs and kernel staging with a new UUIDv7 |
| `POST` | `/snapshots/{snapshot_id}/upload` | upload staged artifacts with multipart HTTP URLs |
| `GET` | `/snapshots/{snapshot_id}` | get upload status and completed artifact details |
| `DELETE` | `/snapshots/{snapshot_id}` | remove local staging |

The create response contains the snapshot ID and rootfs and kernel sizes. Atlas uses the ID as its Machine image name. Parts are 2 GiB, except the final part. The status reports part ETags and artifact SHA-256 values.

The upload request returns `202`. Poll `GET /snapshots/{snapshot_id}` until `completed` or `failed`. The response reports progress during upload and artifact details when complete.

Metal updates snapshot activity during upload. The image reconciler deletes staging after 48 hours without activity. A staging snapshot cannot roll back its source VM.

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

Node names, node IDs, public keys, and image references must be unique. Metal applies WireGuard peers, then saves the image policy. It reconciles images outside the request. Cached images for the host architecture remain local. Other images are pruned after 24 hours without a successful VM start when no dependent disk exists. A cached memory-snapshot image uses local warm artifacts only when CPU, memory, and disk match exactly.

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
