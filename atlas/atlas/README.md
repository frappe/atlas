# Atlas settings module

This Frappe module owns Atlas Settings, provider selection, S3 access, and host binary publication.

## Entry points

| Entry point | Purpose |
|---|---|
| `doctype/atlas_settings/atlas_settings.py` | Validate settings and queue provider catalog jobs. |
| `core/server_providers/` | Define and implement the server provider contract. |
| `core/dns_providers/` | Define and implement DNS provider operations. |
| `core/host_binaries.py` | Build and publish Metal and Atlas WG Mesh binaries. |
| `s3.py` | Provide S3 object and multipart operations. |

Read the [provider guide](../docs/providers.md) before you add a server provider.
Read the [app development guide](../docs/development.md) before you run migrations or tests.

Server providers, DNS providers, and S3 operations have separate contracts. Do not create one generic provider registry.
