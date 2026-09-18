# Metal Server module specification

[Atlas app specification](../SPEC.md)

For the lifecycle overview, see [docs/metal-server-lifecycle.md](../docs/metal-server-lifecycle.md).

## Purpose

A Metal Server is a provider host where Metal runs virtual machines. This module creates it through a provider, installs Metal, and keeps its capacity current.

Provisioning is a sequence of phases, not one transaction. Each phase records progress, so a failed run resumes at the phase that failed instead of repeating work.

## Types

| Type | Owns |
|---|---|
| `MetalServer` (DocType) | Lifecycle hooks, permissions, and the whitelisted API. |
| `provisioning` | The phase order, progress saves, and failure logs. |
| `host_installation` | Installing and configuring Metal on the host. |
| `disk_inventory` | Reading block devices into Metal Server Disk rows. |
| `mesh_peers` | The region unicast peer set and its peer file contents. |
| `catalog_sync` | Refreshing Metal Server Size and Metal Server Image from the provider. |
| `MetalServerIPAddress` (DocType) | One public IPv4 address and its provider intent. |
| `IPAddressService` | Tenant reservation, shared-pool claims, and release. |
| `MetalServerUsage` (DocType) | One capacity sample reported by Metal. |

The `Metal Server` module uses [SSH Task](../atlas/doctype/ssh_task/README.md) for host commands. The DocType belongs to the Atlas module.

## Provisioning

```text
create provider host     one stable provider identity across retries
  -> wait for ready
  -> attach addresses
  -> install Metal
  -> configure WireGuard
  -> mark provisioning complete
```

Every phase is safe to repeat. A creation retry reuses the provider identity from the first attempt, so a lost response cannot create a second host. Compensation deletes only a host that the current request created.

## Capacity

`POST /v1/sync` sends the desired host state and returns capacity in the same exchange. Atlas records the result as a Metal Server Usage row, which is what placement later reads.

When Atlas Settings enables unicast networking, the same exchange carries the complete unicast peer set. metald writes the set into the peer file and enables the unicast daemon unit, so the unicast hooks replace the multicast NDP filters. The peer set comes from `mesh_peers`, which also builds the peer file that `get_mesh_peer_file` returns. The unit arrives with the host installation, so a host installed before the unit existed must run the installation again before the switch.

A scheduled job queues one exchange for each ready server. The `sync_state` action queues one exchange for a single server, so an operator does not wait for the next scheduled run.

A transport fault and a response fault are logged separately, because they need different responses: one is a network problem, the other is a host problem.

The same exchange carries the Atlas public keys that the host must trust. Each host receives its own name as the receiver. A host that is inside the key overlap window receives the current key and the key it replaced. Atlas logs a missing signing key and syncs the host state without keys, so a capacity report never stops for it.

## Public IPv4

An address carries a desired intent and an intent version. Reconciliation applies the intent and preserves a pending one for a retry, so a failed apply is never mistaken for a completed one.

An address has a tenant and a `reserved` flag. An address without a tenant is in the shared pool. `IPAddressService` owns both fields.

A tenant reservation claims an address from the shared pool. An operator fills the pool from the provider. An empty pool is an error. A claim locks the row before it writes the tenant and flag. Release clears both fields, keeps the provider reservation, and refuses attached or detaching addresses.

A virtual machine can attach an address that its own tenant holds, or an unowned address from the shared pool. Attaching an unowned address claims it for the tenant of the virtual machine but does not reserve it. An address that another tenant holds is refused. The attach route accepts `auto` and borrows the oldest free pool address.

An unreserved address returns to the shared pool after detach, including VM deletion. The guarded update clears the tenant only after provider detach succeeds. A reserved address keeps its tenant until release.

A tenant can reserve an address it already holds to stop that return, even while it is attached. A `Detaching` address is refused because reconciliation has already decided its pool return.

Reset Tenant returns one unattached address to the shared pool. It needs the System Manager role, refuses unowned addresses, and records the previous tenant in a comment.

Remove deletes one unattached address and releases its provider reservation. It uses the standard delete permission and confirmation. Both actions appear only for an unused Allocated address.

## Related

- [docs/metal-server-lifecycle.md](../docs/metal-server-lifecycle.md) describes the lifecycle and its reasons.
- [docs/providers.md](../docs/providers.md) describes the provider contract.
- [atlas/atlas/SPEC.md](../atlas/SPEC.md) describes provider implementations and settings.
