# Atlas app specification

[Root specification](../SPEC.md)

Behavior and validation commands: [Atlas app handbook](../docs/develop/atlas-app.md). The app uses Python 3.14, Frappe, MariaDB, Redis, Node, and Yarn.

## Layout

```text
api/            Tenant API routes and models
  core/         HTTP router, request decoding, errors, API docs
atlas/          Settings, providers, TLS, host binaries
auth/           Tokens, regional keys, identity, permissions
metal_server/   Provider hosts and catalog records
service/        Atlas services on VMs: Cargo, proxy, IPv6 router
vm/             VM records, images, and orchestration
realtime/       Browser console bridge
simulator/      Placement trials, not run in the app
scripts/        Host installation scripts
```

In each module, `core/` holds orchestration and `doctype/` holds DocType controllers. Controllers are lifecycle and API boundaries only.

## Module specifications

- [Atlas settings](atlas/SPEC.md)
- [Metal Servers](metal_server/SPEC.md)
- [Services](service/SPEC.md)
- [Virtual machines](vm/SPEC.md)

## Ownership

| Concern | Path |
|---|---|
| HTTP framework | `api/core/`: `base.py`, `binding.py`, `errors.py`, `docs.py` |
| Atlas API | `api/router.py`, `api/models.py`, `api/routes/` |
| Tokens and keys | `auth/request.py`, `token.py`, `jwks.py`, `issuer.py` |
| Users and identity | `auth/user.py`, `roles.py`, `identity.py` |
| Permission overrides | `auth/overrides.py` |
| Job decorator | `atlas/core/background_jobs.py` |
| Providers and TLS | `atlas/core/server_providers/`, `atlas/core/tls/` |

- Keep route functions thin.
- Register each tenant DocType with the shared permission overrides.
- Every queued job entry point uses the Administrator job decorator.
