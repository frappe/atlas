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
server/                      Server module (catalog)
  doctype/                   Server Size and Server Image records
```

## Software

The app uses Python 3.14, Frappe, MariaDB, Redis, Node, and Yarn.

## Scope

The app stores Atlas settings, server sizes, and server images. Provider controllers fetch and sync catalog data. Host VM management belongs to Metal. Regional traffic belongs to the HTTP proxy. Private VM networking belongs to WG Mesh.

## Validation

Run the Frappe tests described in [the CI workflow](../.github/workflows/atlas-ci.yml).

## Ownership

Keep provider behavior in `atlas/core/server_providers/`. Keep settings behavior in `atlas/doctype/`. Keep catalog behavior in `server/doctype/`.
