# Atlas App Specification

[Root specification](../SPEC.md)

[Module specification](atlas/SPEC.md)

## Purpose

The Atlas app provides Frappe settings and provider catalog records for Atlas infrastructure.

## Layout

```text
atlas/                       Atlas module (settings)
  core/                      Provider controllers
  doctype/                   Settings records
vm/                          Virtual machine records, image records, and image builders
server/                      Server module (catalog)
  doctype/                   Server Size and Server Image records
```

## Software

The app uses Python 3.14, Frappe, MariaDB, Redis, Node, and Yarn.

## Scope

The app stores Atlas settings, server catalogs, image metadata, and VM request metadata. Metal owns each VM runtime state and desired state. Atlas does not store a VM lifecycle state machine.

The Virtual Machine name is the Metal VM ID. Creation uses idempotent `PUT /vms/{name}` and accepts HTTP `202`. Atlas uses `GET /vms/{name}` after a lost response. Atlas keeps the draft if the result is uncertain.

Atlas exchanges WireGuard peers, desired cached images, and host capacity with `POST /sync`. Placement uses the latest capacity sample and the image architecture.

Virtual Machine Image is the durable boot artifact for System and Machine images. Each record owns rootfs and kernel objects, exact sizes, and SHA-256 values. Machine image transfer behavior is documented in [VM operations](vm/README.md).

## Validation

Run the Frappe tests described in [the CI workflow](../.github/workflows/atlas-ci.yml).

## Ownership

Keep provider behavior in `atlas/core/server_providers/`. Keep settings behavior in `atlas/doctype/`. Keep catalog behavior in `server/doctype/`. Keep VM orchestration in `vm/virtual_machine_manager.py` and Machine image transfer in `vm/virtual_machine_image_manager.py`.
