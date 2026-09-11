# Atlas app specification

[Root specification](../SPEC.md)

[Module specification](atlas/SPEC.md)

## Purpose

The Atlas app uses Frappe to manage provider hosts, virtual machines, images, and user actions.

## Layout

```text
api/                           Tenant API routes and models
  core/                        HTTP router, request decoding, errors, and API documentation
atlas/                         Site settings, provider behavior, TLS, and host binary builds
  core/                        Provider clients, TLS issuance, background jobs, and the host binary builder
  doctype/                     Atlas Settings and SSH Task
auth/                          Service tokens, regional keys, request identity, and permission overrides
metal_server/                   Provider hosts and Metal Server catalog records
  core/                        Provisioning, host installation, disk inventory, and catalog sync
  doctype/                     Metal Server records and catalog DocTypes
service/                       Atlas services that run on virtual machines
  core/                        HTTP proxy packaging, provisioning, and configuration
  doctype/                     Proxy Server
vm/                            Virtual machine records, images, and orchestration
  core/                        Placement, Metal transport, and image movement
  doctype/                     Virtual Machine and Virtual Machine Image
realtime/                      Browser console bridge
scripts/                       Host installation scripts
```

## Software

The app uses Python 3.14, Frappe, MariaDB, Redis, Node, and Yarn.

## Scope

Atlas stores settings, Metal Server catalogs, image metadata, virtual machine request metadata, and SSH task logs. Metal owns each virtual machine runtime and desired state. Atlas does not store a virtual machine lifecycle state machine.

The Virtual Machine name is the Metal VM ID. Creation uses idempotent `PUT /v1/vms/{name}` and accepts HTTP `202`. Atlas uses `GET /v1/vms/{name}` after a lost response. Atlas keeps the draft if the result is uncertain.

Atlas exchanges WireGuard peers, desired cached images, and host capacity with `POST /v1/sync`. Placement uses a fresh capacity sample and the image architecture. It locks the candidate Metal Server and subtracts requests that the sample does not include.

Virtual Machine Image is the durable boot artifact for System and Machine images. Each record owns rootfs and kernel objects, exact sizes, and SHA-256 values. Machine image transfer behavior is documented in [the VM module SPEC](vm/SPEC.md).

Atlas holds one wildcard TLS certificate for the region. A wildcard name can only be proved through DNS, so issuance uses the ACME dns-01 challenge and the configured DNS provider. See [the wildcard TLS guide](docs/wildcard-tls.md).

See [the security model](docs/security.md) for the trust boundaries and the accepted risks. See [the documentation index](docs/README.md) for every guide.

## Validation

See [docs/development.md](docs/development.md) for the commands to run.

## Module specifications

- [Atlas settings](atlas/SPEC.md)
- [Metal Servers](metal_server/SPEC.md)
- [Services](service/SPEC.md)
- [Virtual machines](vm/SPEC.md)
- [Realtime console bridge](realtime/SPEC.md)

## Ownership

Keep the HTTP framework in `api/core/`: routing in `base.py`, request decoding in `binding.py`, failures in `errors.py`, and documentation generation in `docs.py`. Keep the Atlas surface in `api/router.py`, `api/models.py`, and `api/routes/`. Keep authorization in `auth/`: request authentication in `auth/request.py`, token validation in `auth/token.py`, key distribution in `auth/jwks.py`, and token creation in `auth/issuer.py`. Keep the Atlas API users in `auth/user.py`, role checks in `auth/roles.py`, the request identity and its tenant in `auth/identity.py`, and permission overrides in `auth/overrides.py`. Keep each resource API compact and keep route functions thin. Register each tenant DocType with the shared permission overrides.

Keep the Administrator job decorator in `atlas/core/background_jobs.py`. Every queued job entry point uses it. Keep provider behavior in `atlas/core/server_providers/`. Keep settings behavior in `atlas/doctype/`.

Keep certificate issuance in `atlas/core/tls/`.

Keep Metal Server orchestration in `metal_server/core/`. Keep DocType controllers as lifecycle and API boundaries.

Keep service packaging and installation in `service/core/`.

Keep virtual machine orchestration and image transfers in `vm/core/`.
