# Atlas Module Specification

[App specification](../SPEC.md)

## Purpose

The `atlas` Python module contains the Frappe application code for Atlas settings and provider catalogs.

## Layout

```text
core/server_providers/      Provider interfaces and implementations
core/dns_providers/         DNS provider interfaces and implementations
doctype/atlas_settings/     Atlas Settings DocType (module: Atlas)
../server/doctype/          Server catalog DocTypes (module: Server)
  server_size/               Server Size DocType
  server_image/               Server Image DocType
```

## Ownership

Keep provider behavior in the matching `core/` package. Keep document behavior in the matching DocType controller. Keep provider metadata in the related catalog record.

## Interfaces

Server providers expose settings validation, infrastructure setup, server sizes, and server images through `ServerProvider`. The Atlas Settings controller queues catalog synchronization jobs after setup.

Server size records store disk capacity in GiB and prices in integer USD cents. The provider controller preserves the provider price and fills a missing billing period from the available price.
