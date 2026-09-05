# Atlas Module Specification

[App specification](../SPEC.md)

## Purpose

The `atlas` Python module contains the Frappe application code for Atlas settings and provider catalogs.

Read [the module README](README.md) for entry points.

## Layout

```text
core/server_providers/      Provider interfaces and implementations
core/dns_providers/         DNS provider interfaces and implementations
doctype/atlas_settings/     Atlas Settings DocType (module: Atlas)
../vm/doctype/              Virtual machine and image DocTypes (module: VM)
../server/doctype/          Server catalog DocTypes (module: Server)
  server_size/               Server Size DocType
  server_image/               Server Image DocType
```

## Ownership

Keep provider behavior in the matching `core/` package. Keep document behavior in the matching DocType controller. Keep provider metadata in the related catalog record.
Keep VM records, image records, image transfer, and image builders in `../vm/`. Keep S3 operations in `s3.py`.

## Interfaces

Server providers implement `ServerProvider`. The registry maps one stable provider type to one implementation.

Providers use typed creation, result, catalog, power, address, and error values. Low-level provider components do not save Frappe documents.

The Atlas Settings controller queues catalog synchronization jobs after setup. `server/core/catalog_sync.py` owns catalog persistence.

Server size records store disk capacity in GiB and prices in integer USD cents. The provider controller preserves the provider price and fills a missing billing period from the available price.

Read [the provider guide](../docs/providers.md) for the complete contract.
