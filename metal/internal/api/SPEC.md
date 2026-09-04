# api: Metal HTTP server

[internal SPEC](../SPEC.md) · endpoint guide: [docs/api.md](../../docs/api.md)

## Purpose

Package `api` validates HTTP requests and calls small service interfaces. Lifecycle handlers store desired state and wake the reconciler.

## Types

`New(Config, Dependencies)` validates authentication and required services. It returns the configured Echo router or an error.

`Dependencies` contains the VM driver, snapshot services, image policy store, reconciler wake function, WireGuard manager, and capacity provider.

Request and response types are split by resource. `Server` owns the handlers and injected services.

## Request flow

```text
HTTP -> validate -> call a service -> wake a reconciler when required -> JSON
```

Create and lifecycle handlers return before host reconciliation completes.

## Routes

```text
GET    /health
POST   /sync

PUT    /vms/:id
GET    /vms
GET    /vms/:id
PUT    /vms/:id/ssh-keys

POST   /vms/:id/actions/start
POST   /vms/:id/actions/stop
POST   /vms/:id/actions/pause
POST   /vms/:id/actions/resume
POST   /vms/:id/actions/terminate

POST   /vms/:id/resize/compute
POST   /vms/:id/resize/disk

POST   /vms/:id/snapshots
POST   /snapshots/:id/upload
DELETE /snapshots/:id

GET    /vms/:id/console
GET    /docs
GET    /docs/swagger.json
```

Create and lifecycle changes return `202`. Snapshot staging creation returns `201`. The console route returns `501`.

## Authentication

All routes except `/docs` and `/docs/swagger.json` require a bearer token. Metal compares its SHA-256 digest with the configured digest.

## Error mapping

Error responses contain a stable `code` and a safe `message`. Domain image conflicts and integrity failures have separate codes.

The server does not return host command output or signed URL query values.

## DTO layout

Request and response objects have separate files. The VM response groups image artifacts and network data into nested objects.

The VM response does not include transport URLs, the internal guest IPv4 address, or the Firecracker process ID.

## API specification

`GET /docs` serves the API page. `GET /docs/swagger.json` serves the generated OpenAPI document.

## Related

- [docs/api.md](../../docs/api.md) gives request and response details.
- [internal/vm/SPEC.md](../vm/SPEC.md) defines the VM contracts.
- [cmd/metald/SPEC.md](../../cmd/metald/SPEC.md) injects server dependencies.
