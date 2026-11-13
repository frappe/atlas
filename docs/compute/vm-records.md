# Atlas VM records

An Atlas VM record identifies a tenant resource and reserves a host. Metal uses the same stable VM ID for host execution and retries.

## Create and confirm a record

Atlas commits the record as a draft before it calls Metal. The draft holds the host reservation while Atlas checks an uncertain response. [How a VM request works](../start/how-a-vm-request-works.md) explains when Atlas confirms or removes it.

## Change VM resources

Atlas sends complete desired compute, disk, network, metadata, and SSH key values.

[SSH key changes](ssh-keys.md) use Firecracker metadata and the guest's login command. The VM image does not need to be rebuilt for a key change.

CPU uses `cpu_millicores`: **1000 equals one core**. The range is `100` through `32000`, avoiding impractical quotas and respecting Firecracker's 32-vCPU maximum.

### Burstable CPU

A burstable VM can use all of its `cpu_millicores` for a short time, but its average must stay at or below its CPU baseline. `is_burstable` marks the VM. `cpu_baseline_millicores` sets the baseline, from `0` to `cpu_millicores`. Both are off and `0` by default. A baseline needs `is_burstable`. You set both when you create the VM.

A baseline of `0` means 20 percent of `cpu_millicores`. For example, a burstable VM with 2000 millicores and no baseline may average 400 millicores.

Every 30 seconds, metald measures the average CPU use of each running burstable VM over the last hour. It counts only the time that the VM was up. When the average is above the baseline, metald limits the VM to the baseline. When the average drops below 90 percent of the baseline, metald gives the VM its full `cpu_millicores` again. The gap stops a VM that is held at its baseline from switching on every check.

metald logs each change with `burstable VM CPU throttle changed`. The limit is a systemd CPU quota on the VM unit, so a throttled guest runs slower but does not stop. A restart of the VM or of metald does not clear a throttle, because metald applies the limit again on the next check from the stored samples.

To end a throttle, a System Manager uses **Reset CPU Throttle** in the VM form. Metal then counts the VM's CPU use only from the reset time, so the VM gets its full CPU at once. Metal stores the reset time with the VM, so a metald restart does not bring the old samples back. A VM that keeps a high load is throttled again when its new average goes above the baseline. Only a burstable VM can be reset.

## Read current VM state

| Data | Source |
| --- | --- |
| Request, host, image, architecture, tenant, resources | Atlas VM record. |
| Current guest state, routes, host error | Metal, read when document properties need them. |
| VM list state | `Virtual Machine State`, cached from host sync. |

The cache can be stale after failed sync. Compare Metal's desired and observed generations for operation progress.

## Terminate or move a VM

Termination retains the Atlas record while Metal cleans up. Atlas removes it only after Metal confirms absence.

Resize stays on the current host if capacity permits. Otherwise, Atlas reserves a destination and creates a [migration record](migration.md).

## Guest metadata

Atlas sends custom metadata as a map of string keys and values. Metal publishes it at `latest/meta-data/attributes` in Firecracker's microVM Metadata Service (MMDS). A change replaces the complete map.

| Value | Limit |
| --- | --- |
| Custom metadata | Atlas accepts at most 16 entries and 48 KiB of JSON. |
| Custom metadata key | Metal accepts at most 128 bytes. |
| SSH keys | Metal accepts at most 16 keys, each at most 2 KiB. |
| User data | Metal accepts at most 16 KiB. |
| Hostname | Metal accepts at most 253 bytes. |
| MMDS document | Metal sets the Firecracker MMDS limit to 256 KiB. Metal keeps the base document within 248 KiB and reserves 8 KiB for temporary console keys. |

Metal checks the complete MMDS document before it saves a create, network, SSH key, or metadata change. See [SSH key changes](ssh-keys.md) for the guest login path.

## Open the console

VM read access permits a console request. [Open a VM console](console.md) explains the token, realtime bridge, and two guest modes.

## Limits and recovery

Keep an uncertain draft if the host cannot be reached. Check current Metal state before retrying a change. The Atlas list alone is insufficient.

**Details:** [Atlas API](/api/atlas/) and [Metal reconciliation](reconciliation.md).

::: details Source code and tests

- [VM DocType](../../atlas/vm/doctype/virtual_machine/virtual_machine.py) owns record actions and virtual fields.
- [VM service](../../atlas/vm/core/vm_service.py) owns cross-service changes.
- [Atlas metadata validation](../../atlas/vm/core/models.py) limits custom entries and JSON size.
- [Metal metadata builder](../../metal/internal/vm/metadata_service.go) checks the complete MMDS document.
- [Draft and termination reconciliation](../../atlas/vm/core/reconciliation.py) settles uncertain outcomes.
- [Reported state cache](../../atlas/vm/core/vm_state.py) stores host sync results.
- [VM service tests](../../atlas/vm/core/test_vm_service.py) check the create and retry contract.

:::
