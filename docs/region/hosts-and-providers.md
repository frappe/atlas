# Metal Servers and provider integration

A Metal Server is the Atlas app's record for a physical host running Metal. Operators manage hosts through Desk. The tenant API does not expose host administration.

## Register and prepare a host

Atlas Settings selects one provider per region. Its adapter validates credentials, supplies the host catalog, and performs remote actions. Adapters can create a host or register one prepared manually.

Generic selects an SSH or BMC driver. See the [Generic provider guide](provider-guide.md#generic-provider) for registration.

| Record | Purpose |
| --- | --- |
| Metal Server | Provider identity, addresses, setup progress, and status. |
| Metal Server Size and Image | Provider catalog for new hosts. |
| Metal Server Usage | Capacity reports for placement. |
| SSH Task | Host command and result. |

For host creation, Atlas commits a `Pending` record before contacting the provider. The stable record name lets retries find the same remote host.

Setup prepares provider resources, SSH, networking, WireGuard, and Metal. It saves completed phases in MariaDB and marks the host `Running` only after completion. See the [provisioning sequence](index.md).

### Import a provider server

Use **Import Server** on the Metal Server list for a Scaleway or AWS server that Atlas did not create. Enter its provider server ID. Atlas matches its size and image to the catalog, adds the Scaleway private network option when it is missing, and runs the normal setup.

The Atlas SSH key must allow login as a user that the provider supports. A non-root user needs passwordless `sudo`. Atlas copies that user's authorized keys to root and removes the user during promotion. Use root login if the existing user must remain.

Atlas uses the Scaleway `/dev/md2` array or the AWS storage volume mapped as `/dev/sdb`. If an imported AWS server has no volume mapped as `/dev/sdb`, enter a **Storage Pool Device** that exists on the host. You can also name a disk image file that you created. A new pool needs an empty device. Setup reuses an existing `metal` pool.

Importing the same server again continues setup if it is incomplete. The command line equivalent is `pilot --site SITE import-metal-server <id> [--storage-pool-device PATH]`. Generic SSH hosts use **Add Server**. Generic BMC hosts use **Register Server**.

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

## Upgrade `metald`

**Check console preservation before restarting Metal.** An empty systemd descriptor store means every running VM on the host stops.

| Action | Behavior |
| --- | --- |
| `install_metald` | Writes configuration and units. Installs missing binaries. Does not replace a running daemon. |
| `upgrade_metald` | Downloads the build from Atlas Settings, saves `/usr/bin/metald.previous`, installs the build, and restarts `metal.service`. |

The upgrade script reports the descriptor count before restart. It checks the new service **five times**. On failure, it restores the previous binary, restarts the service, and reports the error.

If it reports an old unit, run **Re-configure Metald**. This adds `FileDescriptorStorePreserve=yes`, which the host systemd version must support.

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
- [Host sync](../../atlas/metal_server/usage.py) stores capacity and state reports.
- [Metal Server tests](../../atlas/metal_server/doctype/metal_server/test_metal_server.py) check record lifecycle behavior.

:::
