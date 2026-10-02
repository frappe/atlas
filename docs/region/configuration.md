# Atlas configuration

Use **Atlas Settings** for regional configuration. Set the site value `atlas_internal_url` to the Atlas listener on its mesh address. Hosts and tenant-0 VMs reach Atlas through it.

Set `atlas_base_url` to the public Atlas address when the site URL is not public.

## Settings by purpose

| Group | What it controls | Important effect |
| --- | --- | --- |
| Region and provider | Region identity, provider adapter, host catalog defaults, and credentials. | New hosts and signed regional identities use these values. |
| Host and network | Private network range, MTU, unicast mesh mode, and Atlas WireGuard identity. | Host setup and sync use these values. |
| Placement | Strategy, sleepy VM pool, overcommit factor, and host auto-spawn. | New VM capacity checks and host expansion use these values. |
| Trust | Metal certificate authority, Atlas client certificate, regional signing key, and Central public key source. | Atlas, Metal, and service clients use these credentials. |
| Proxy and DNS | Wildcard domain, certificate, proxy password, and DNS access. | Proxy nodes receive updated configuration. |
| Images and storage | Object storage endpoint, bucket, credentials, and signed URL life. | Atlas issues artifact URLs and migrates bootstrap images. |

## Apply settings changes

Save validates the provider and placement settings.

| Change | Effect |
| --- | --- |
| Region ID | Refused while VMs or non-archived proxy records exist. |
| Proxy credentials or certificate | Queues configuration updates for active proxies. |
| Object-storage fields | Queues migration of eligible Site File images. |

## Credentials and host access

Atlas creates regional signing and Metal trust material as part of settings setup. Secret keys and passwords use Frappe Password fields. Do not copy them into logs or documentation. A host receives its own certificate during installation.

[Signing keys and tokens](../interfaces/signing-keys.md) explains the Central JWKS URL, the combined regional key set, and the separate service audiences.

| Network setting | Purpose |
| --- | --- |
| `atlas_internal_url` | Site URL that hosts and tenant-0 VMs use for Atlas files and APIs. |
| `atlas_base_url` | Public site URL when the normal site URL is not public. It is the fallback for internal clients. |
| `atlas_wireguard_mtu` | MTU of `atlas0`. Restart the interface after a change. |
| `warpgate_grant_max_hours` | Longest Warpgate host access grant, 24 hours by default. |
| `is_unicast_network_enabled` | Uses unicast discovery when the host network cannot carry multicast. |

The first four values are site configuration keys. See [Atlas access to hosts](host-access.md) for setup and recovery.

## Limits and recovery

**Saving a field does not prove that every host applied it.** Setup, sync, or service configuration may still need to run.

Verify hosts and services after changing region identity, trust material, storage credentials, or the internal URL.

::: details Source code and tests

- [Atlas Settings schema](../../atlas/atlas/doctype/atlas_settings/atlas_settings.json) lists fields and defaults.
- [Atlas Settings controller](../../atlas/atlas/doctype/atlas_settings/atlas_settings.py) validates values and queues dependent work.
- [Provider registry](../../atlas/atlas/core/server_providers/registry.py) selects the host adapter.
- [Host installation](../../atlas/metal_server/core/host_installation.py) writes Metal host configuration.
- [Host sync](../../atlas/metal_server/usage.py) applies regional host policy.

:::
