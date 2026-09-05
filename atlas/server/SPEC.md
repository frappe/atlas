# Server module specification

[App specification](../SPEC.md)

[Human entry point](README.md)

## Purpose

The Server module owns provider host records, setup, disk inventory, and capacity samples.

## Ownership

`doctype/server/server.py` owns lifecycle hooks, permissions, and whitelisted methods.

`core/provisioning.py` owns the setup order, progress saves, commits, and failure logs.

`core/host_installation.py`, `core/disk_inventory.py`, and `core/catalog_sync.py` own their named operations.

## Invariants

- `Server.before_validate` remains the remote creation entry.
- A creation retry uses one stable provider server identity.
- Compensation deletes only a server that the current request created.
- Each setup step is safe to repeat.
- The Server schema remains unchanged during this refactor.

## Tests

```sh
ruff check atlas
bench --site TEST_SITE run-tests --module atlas.server.doctype.server.test_server
bench --site TEST_SITE run-tests --module atlas.server.core.test_provisioning
```
