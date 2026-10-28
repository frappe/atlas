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

Select **Set hostname to server name** before setup to set the host's operating system hostname to its Metal Server title, such as `metal-bom-1`. The option is off by default and is available on the server form, the **Add Server** review step, and **Import Server**.

The field is read-only after provisioning completes. Setup retries apply the saved option. Cloud-init preserves the hostname across restarts.

### Import a provider server

Use **Import Server** on the Metal Server list for a Scaleway or AWS server that Atlas did not create. Enter its provider server ID. Atlas matches its size and image to the catalog, adds the Scaleway private network option when it is missing, and runs the normal setup.

The Atlas SSH key must allow login as a user that the provider supports. A non-root user needs passwordless `sudo`. Atlas copies that user's authorized keys to root and removes the user during promotion. Use root login if the existing user must remain.

Atlas uses the Scaleway `/dev/md2` array or the AWS storage volume mapped as `/dev/sdb`. If an imported AWS server has no volume mapped as `/dev/sdb`, enter a **Storage Pool Device** that exists on the host. You can also name a disk image file that you created. A new pool needs an empty device. Setup reuses an existing `metal` pool.

Importing the same server again continues setup if it is incomplete. The command line equivalent is `pilot --site SITE import-metal-server <id> [--storage-pool-device PATH] [--set-hostname]`. Generic hosts use **Add Server** instead.

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
| `upgrade_metald` | Upgrades one server to the latest build in Atlas Settings. |
| Metald Update | Upgrades many servers to one pinned build in batches. Use it for every fleet rollout. |

### Roll out a build

A Metald Update moves Metal Servers to one pinned metald and Atlas WG Mesh build. [Roll out metald and WG Mesh](../operate/roll-out-metald.md) gives the steps with screenshots. This section explains how the update works.

A new update pins the versions, binaries, and hashes from Atlas Settings, so later builds do not change it.

| Status | What happens |
| --- | --- |
| Draft | You change the servers and the rollout fields. **Plan Rollout** starts the planning. |
| Planning | Reads host versions through `GET /v1/versions`. A server that already runs the build or is not Running becomes Skipped. A failed read becomes Failed. If all servers are Skipped, the update becomes Completed. |
| Planned | You review the versions. **Start Rollout** assigns batches in row order. **Edit Plan** returns to Draft. |
| Running | Upgrades and checks one batch at a time. |
| Paused | Waits for **Resume Rollout**, which retries the Failed servers, or **Cancel Rollout**. |
| Completed, Cancelled | The rollout has ended. |

Only one update can be Running or Paused. A step runs every 5 seconds and continues from the stored row statuses after a restart. During Running, it:

1. Starts one upgrade SSH Task per server in the current batch.
2. Waits **Wait Minutes** after the last upgrade.
3. Checks current and earlier batches. Each server must report the pinned `metald` and Atlas WG Mesh versions and must have synchronized within 2 minutes.
4. Starts the next batch or completes the update.

A failed upgrade or check pauses the update. [When a server fails](../operate/roll-out-metald.md#when-a-server-fails) explains how to continue or go back to an earlier build.

### Run a script on many hosts

**Metal Server Console** runs a script as `root` on many Metal Servers at once, over each host SSH address (wg0). Enter titles, names, or IP addresses, or use **Pick Servers**. Each entry must match one server. The Metal Server Console Log shows the live output of each server.

**Scrub Output** is on by default. It masks credentials and high-entropy words, such as keys and tokens, before Atlas stores the output. Turn it off only when you must see a value.

### Upgrade on one host

`upgrade-metald.sh` replaces `metald` and `atlas-wg-mesh` together. It checks the hashes, keeps the running binaries as `.previous`, restarts `metal.service`, and waits up to 120 seconds for the status `ready <version>`. If the service does not become ready or a VM unit stops, it restores the previous binaries. It keeps the new binaries when the new build has a [metald patch](../develop/metal.md#change-stored-state-with-a-patch) that the host has not applied, because the previous build cannot read the patched state. A repeated upgrade to the running build changes nothing.

If the script reports an old unit, run **Re-configure Metald**. This adds `FileDescriptorStorePreserve=yes`, which the host systemd version must support.

Run **Re-install Firewall** to apply the current host firewall without a metald restart. Use it when a host was upgraded before a firewall rule existed, for example the migration rule for the private network.

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
