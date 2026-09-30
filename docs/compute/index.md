# Create and manage a VM

Atlas chooses a host and stores the VM request. Metal applies that request on the host. Start with [how one VM request works](../start/how-a-vm-request-works.md) to learn which component owns each state.

## Create a VM

Atlas [chooses a host](placement.md), saves a draft reservation, and asks Metal to create the VM. Metal accepts the request before the guest is ready. Follow the linked request guide for the full create flow and lost-response behavior.

## Change a VM

Atlas sends power, restart, resource, network, key, and metadata changes to the assigned host. Metal reports the applied generation separately from request acceptance.

See [guest metadata](vm-records.md#guest-metadata) for the limits on custom metadata and other MMDS values.

A resize uses the current host if it has enough capacity. Otherwise, Atlas can reserve another host and use [migration](migration.md).

Read [Metal's requested and applied generations](reconciliation.md) for current progress. The Atlas VM list uses [cached host reports](../region/host-sync.md).

## Repair a VM

Use [rescue mode](rescue.md) to boot a repair system with the original disk attached and unmounted.

## Terminate a VM

1. Atlas requests destruction and marks its record as terminating.
2. Metal removes runtime, network, and disk resources through saved cleanup checkpoints.
3. Atlas removes its record after Metal confirms absence.

Public-address allocation has a separate retry path.

## Failure and recovery

Keep uncertain drafts and terminating records when Metal does not answer. [Find a problem](../operate/find-a-problem.md) starts with the assigned host, Metal progress, and Atlas jobs. For guest access, use the [console](console.md).

**Details:** [Atlas VM records](vm-records.md), [Metal reconciliation](reconciliation.md), [Atlas API](/api/atlas/), and [Metal API](/api/metal/).

::: details Source code and tests

- [Atlas VM service](../../atlas/vm/core/vm_service.py) owns draft creation and host calls.
- [Placement context](../../atlas/vm/core/placement/context.py) reserves host capacity.
- [Atlas reconciliation](../../atlas/vm/core/reconciliation.py) settles uncertain drafts and terminations.
- [Metal manager](../../metal/internal/vm/manager.go) stores the create request and checks retries.
- [Metal reconciliation](../../metal/internal/vm/reconcile.go) applies and removes host resources.
- [Atlas VM tests](../../atlas/vm/core/test_vm_service.py) and [Metal manager tests](../../metal/internal/vm/manager_test.go) check the two commit points.

:::
