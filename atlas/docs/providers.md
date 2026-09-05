# Server providers

Atlas uses `ServerProvider` as the server provider extension point. The registry maps one stable provider type to one provider class.

## Ownership

Atlas owns provider selection, credentials, catalog records, and Server documents. A provider owns remote resource operations.

Low-level provider components return values. They do not save Frappe documents or commit database transactions.

## Contract

The provider contract includes these operations:

- Validate settings and credentials.
- Set up named provider infrastructure.
- Return server sizes and images.
- Ensure one named provider server.
- Prepare provider resources before Secure Shell access.
- Configure the provider network after Secure Shell access.
- Apply one explicit power action.
- Delete one provider server safely.
- Return the storage pool device.
- Optionally reserve, attach, detach, and delete public IPv4 addresses.

Optional address operations raise `UnsupportedProviderOperation` when the provider does not support them.

Creation uses `ServerCreateRequest` and returns `ProviderServer`. Catalog operations return `ServerSizeData` and `ServerImageData`.

## Add a provider

1. Add one package under `atlas/core/server_providers/`.
2. Implement `ServerProvider` with absolute imports.
3. Register the class with `register`.
4. Split remote operations by owned provider resource.
5. Keep Frappe document writes in Atlas owners.
6. Add tests for registration, retries, power failures, deletion, and optional operations.
7. Add the provider option and fields to Atlas Settings.
8. Update this guide and the related specification.

Use one stable remote identity for `ensure_server`. A retry must return the same provider server.

Do not delete a reused provider server during local compensation. Delete only a provider server that the current request created.

## Scaleway structure

| Module | Owner |
|---|---|
| `configuration.py` | Immutable settings for low-level operations. |
| `client.py` | HTTP transport and Scaleway errors. |
| `infrastructure.py` | VPC, private network, and Secure Shell key operations. |
| `catalog.py` | Offer and operating system translation. |
| `servers.py` | Elastic Metal server operations. |
| `ip_addresses.py` | Flexible IP address operations. |
| `partitioning.py` | Provider disk layout. |
| `provider.py` | Contract composition and host network setup. |
