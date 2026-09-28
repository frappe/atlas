# Metal Servers and provider integration

A Metal Server is the Atlas app's record for a physical host running Metal. Operators manage hosts through Desk. The tenant API does not expose host administration.

## Register and prepare a host

Atlas Settings selects one provider per region. Its adapter validates credentials, supplies the host catalog, and performs remote actions. Adapters can create a host or register one prepared manually.

| Record | Purpose |
| --- | --- |
| Metal Server | Provider identity, addresses, setup progress, and status. |
| Metal Server Size and Image | Provider catalog for new hosts. |
| Metal Server Usage | Capacity reports for placement. |
| SSH Task | Host command and result. |

Atlas commits a `Pending` record before contacting the provider. The stable record name lets retries find the same remote host.

Setup prepares provider resources, SSH, networking, WireGuard, and Metal. It saves completed phases in MariaDB and marks the host `Running` only after completion. See the [provisioning sequence](index.md).

### Retry setup

On failure, read the phase in the Error Log. Correct the cause, then use **Setup Metal Server**. Keep the provider identity so the retry can reuse the host.

Setup runs as Administrator. Setup and daemon upgrade share a per-server job lock, so they cannot run together.

## Desk actions

Each method checks permissions and local state before delegating long work.

| Area | Methods |
| --- | --- |
| Setup | `setup_server`, `configure_wireguard`, `install_metald`, `upgrade_metald` |
| Power | `reboot_server`, `poweroff_server`, `poweron_server` |
| Inventory | `ping_server`, `sync_disks`, `sync_state` |
| Removal | `archive_server` |

## Upgrade host binaries

Atlas publishes `metald` and Atlas WG Mesh as one compatible pair. A host upgrade installs both binaries before it starts the new Metal process.

| Action | Behavior |
| --- | --- |
| `install_metald` | Installs both binaries, writes configuration and units, and restarts `metal.service`. It restores the previous binaries and configuration when the service does not stay active. |
| `upgrade_metald` | Stages and verifies both binaries, checks console preservation, saves the previous pair, installs the new pair, and restarts `metal.service`. |

The normal upgrade does not clear WG Mesh state. It stops before mutation unless `metal.service` is active, `FileDescriptorStorePreserve=yes`, and systemd holds exactly one console descriptor for each active VM unit.

The script checks the new service five times. It also checks that the active VM unit set is unchanged and that the installed WG Mesh BPF hash matches the new binary. On failure, it restores both previous binaries, restarts the previous service, and reports the error.

An operator can explicitly request a legacy WG Mesh reset with the `reset_wg_mesh=1` method argument. Use this mode only when the installed BPF map layout is incompatible with the new binary.

Legacy reset mode is destructive. It removes private mesh state after both new binaries pass download and digest checks. Atlas host sync must then reconstruct VM, peer, privileged address, route, and transport state. Private VM traffic is interrupted until this convergence finishes.

Binary rollback after a legacy reset does not restore the removed maps. The recovery path restores the previous pair and rebuilds the previous host mesh, then requires Atlas sync and complete traffic checks. Upgrade one host at a time and do not continue when reconciliation is incomplete.

Use **Re-configure Metald** when an old host unit does not have descriptor preservation. The host systemd version must support `FileDescriptorStorePreserve=yes`.

## Capacity and state reports

Periodic [host sync](host-sync.md) sends policy and receives capacity and VM state. Atlas saves usage samples and a state cache.

A `Running` host still needs a recent sample for [placement](../compute/placement.md).

## Limits and recovery

Provider adapters support different power and address actions. Unsupported optional actions fail explicitly.

Setup needs valid provider credentials, root SSH, network access, and a suitable storage device. To add an adapter, follow the [provider guide](provider-guide.md). Provider methods return remote resource data. Atlas owns Frappe record changes.

::: details Source code and tests

- [Metal Server module specification](../../atlas/metal_server/SPEC.md) maps the host records and jobs.
- [Provider contract](../../atlas/atlas/core/server_providers/base.py) and [registry](../../atlas/atlas/core/server_providers/registry.py) define the integration boundary.
- [Host provisioner](../../atlas/metal_server/core/provisioning.py) owns the phase order and commits.
- [Host installation](../../atlas/metal_server/core/host_installation.py) installs Metal and host services.
- [Host upgrade transaction test](../../atlas/scripts/tests/test-upgrade-metald.sh) checks the descriptor gates and rollback paths.
- [Host sync](../../atlas/metal_server/usage.py) stores capacity and state reports.
- [Metal Server tests](../../atlas/metal_server/doctype/metal_server/test_metal_server.py) check record lifecycle behavior.

:::
