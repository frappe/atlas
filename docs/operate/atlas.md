# Operate the Atlas app

Start with the failed resource record and its operation ID. If the owner is unclear, use [Find a problem](find-a-problem.md).

## Before you retry

- Read the **Error Log** for job failures and **SSH Tasks** for host commands.
- Check Metal for current VM state. The Atlas list is a cache.
- Correct the underlying fault before repeating an operation.
- Keep uncertain records until the external owner confirms presence or absence.

The scheduler retries drafts, termination, sync, public IPs, images, and pending service setup. A `Failed` record can still need operator action. Metal continues local reconciliation during an Atlas outage.

## Proxy cluster cannot accept writes

The proxy API returns `503`, or `/readyz` returns `503` on several nodes. The nodes still answer `/healthz` and route traffic.

**Check**

1. Call `GET /v1/cluster/status` on node addresses.
2. Compare `leader_id`, `term`, `generation`, `members`, and `ready`.
3. Check `atlas-proxy-control.service` logs.

**Expected:** A ready majority has one leader and matching generations. A five-node cluster needs three write acknowledgements.

**Recover:** Restore enough configured peers for the required acknowledgement count. Retry the same route change after connectivity returns.

Do not edit `cluster-state.json` or reduce a generation by hand.

## Proxy node stays outside regional DNS

A Proxy Server is Failed, or its address is absent from `proxy.<wildcard-domain>`.

**Check**

1. Read the failed provisioning phase.
2. Check the node A record, `/readyz`, its `/healthz` health check, and configuration push tasks on active peers.

**Expected:** Atlas publishes regional DNS only after the node is ready. Route 53 normally omits an unhealthy node and returns all records if every health check fails.

**Recover:** Correct the failed phase and select Re-provision. Atlas repeats safe steps and reconciles active node configurations every minute.

Do not add an unready node to regional DNS by hand.

## Atlas cannot reach Metal

A VM read fails, or Metal Server synchronization writes a Metal connection Error Log.

**Check**

1. Open the Metal Server document.
2. Run `wg show atlas0` on the Atlas machine and check the handshake with the host. See [Atlas access to hosts](../region/host-access.md#recovery).
3. Check recent Error Logs.
4. On the host, run `systemctl status metal.service` and `journalctl -u metal.service --since "15 minutes ago"`.

**Expected:** The log names the Metal Server and keeps the connection error. Metal logs show the same request time or no received request.

**Recover:** Restore network access or restart `metal.service`. Wait for the next synchronization. Repeat a user action only after you read current state.

Do not delete the Atlas VM record. Do not copy certificates or keys into logs or chat.

## Desired and observed generations do not match

`desired.generation` stays greater than `observed.generation`.

**Check**

1. Call `GET /v1/vms/{id}` with the Atlas client certificate.
2. Check `observed.error` and the Metal operation logs for the VM ID.

**Expected:** The observed state shows the last applied generation and a safe error when reconciliation stopped.

**Recover:** Correct the named host fault. Let reconciliation retry. Send the same desired request only when you must wake reconciliation.

Do not edit Metal record files. Do not increase a generation by hand.

## VM stays unknown or failed

The Atlas form shows `unknown` or `failed` after the normal reconcile interval. A draft shows `pending`.

**Check**

1. Check whether the Atlas record is a draft.
2. Read `GET /v1/vms/{id}`.
3. Check the Metal log and `systemctl status metal-vm@<id>.service`.

**Expected:** HTTP `404` confirms an absent draft. A present record reports desired state, observed state, and an error.

**Recover:** Let draft reconciliation finalize or remove a confirmed absent draft. Correct a reported host error and let Metal retry.

Do not delete an uncertain draft before Metal returns HTTP `404`. Do not start Firecracker outside systemd.

## Metal Server stays Pending, Installing, or Failed

Metal Server setup does not reach Running.

**Check**

1. Open the Metal Server and its setup task.
2. Read the latest Error Log.
3. Check the recorded failed phase.
4. Test provider state and root Secure Shell access.

**Expected:** The Metal Server keeps completed fields. The log names the Metal Server and failed phase.

**Recover:** Correct the phase fault. Use Setup Metal Server again. Each completed operation is safe to repeat.

Do not clear `provider_server_id`. Do not delete a reused provider host.

## Capacity samples become old

The newest Metal Server Usage record is more than 2 minutes old.

**Check**

1. Check recent Metal Server synchronization Error Logs.
2. Check that the Metal Server is Running and provisioning is complete.
3. Check `metal.service` logs.

**Expected:** The Error Log distinguishes a connection failure from an invalid capacity response.

**Recover:** Restore Metal access or correct the host inspection fault. Wait for a new Metal Server Usage record.

Do not change the creation time of an old sample. Do not estimate free capacity by hand.

## Placement reports no capacity

Create Virtual Machine reports no current sample or no Metal Server with enough capacity.

**Check**

1. Compare the image architecture, newest Metal Server Usage values, and existing Atlas reservations.
2. Check uncertain drafts.

**Expected:** Atlas identifies a missing sample separately. A current sample shows the limiting CPU, memory, or storage value.

**Recover:** Restore synchronization, remove only confirmed stale drafts, or add real host capacity.

Do not remove a draft that Metal might have created. Do not edit capacity samples.

## Public IP request stays pending

A Public IP Allocation stays Attaching or Detaching, or its pool has pending provider state.

**Check**

1. Read the allocation and pool `intent_version` values, server, provider resource ID, and `failure_message`.
2. Find the Error Log for the same resource and version.

**Expected:** A failed job keeps the current request. An old job cannot complete a newer version.

**Recover:** Correct provider or Metal access and wait for the scheduled retry. If you queue a retry, use only the latest request version.

Do not reduce `intent_version`. This code field identifies the latest attach or detach request. Do not change the provider attachment manually while that request is pending.

## Machine image transfer does not finish

A Machine image stays Pending, Uploading, Completing, Cleaning, or Failed.

**Check**

1. Open the image and record its status, error, source Metal Server, snapshot ID, object keys, and upload IDs.
2. Check Atlas and Metal logs without printing signed URLs.

**Expected:** Failed transfers keep all retry identifiers. Completed uploads have both SHA-256 values before cleanup.

**Recover:** Correct Metal or object storage access. Use Retry Transfer for a Failed image. Let periodic work advance an active image.

Do not clear upload IDs before completion. Do not delete Metal staging while Atlas still needs it.

::: details Source code and tests

- [Scheduler hooks](../../atlas/hooks.py) list periodic reconciliation jobs.
- [VM reconciliation](../../atlas/vm/core/reconciliation.py) settles uncertain create and delete results.
- [Host sync](../../atlas/metal_server/usage.py) records capacity and reported VM state.
- [Host provisioner](../../atlas/metal_server/core/provisioning.py) records failed setup phases.
- [Public IP allocation](../../atlas/metal_server/doctype/public_ip_allocation/public_ip_allocation.py) keeps the request version.
- [Image transfer](../../atlas/vm/core/vm_image_transfer.py) keeps retry data.

:::
