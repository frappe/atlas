# VM state updates to Central

Atlas can send Central a message when a VM's reported state changes. The message comes from Atlas's `Virtual Machine State` record. That record contains the last host report, so a message can describe an older host state.

## Set the receiver

Central calls `PUT /api/atlas/webhooks` with a receiver URL, a shared secret, and an enabled flag. Only a Central token for all tenants can call this route. A repeated call updates the same Webhook records.

The default `central_id` is `1`. Other IDs need developer mode or the `allow_multiple_central_webhooks` site setting. The [Atlas API reference](/api/atlas/) gives the request and response fields.

## When Atlas sends a message

| Event | Trigger |
| --- | --- |
| `vm.state` | Atlas first saves a host report, the reported status changes, or the VM moves to another host. |
| `vm.state.deleted` | Atlas deletes the state record. |

A host report with the same status and host sends no new `vm.state` message. Atlas includes the VM ID, host server, status, and `observed_at` time in a JSON request. It sends `X-FC-Source: atlas` and includes `X-FC-Region` when the region has a name. Frappe signs the request with the shared secret.

The configured request timeout is 10 seconds, with at most 3 retries. For the current VM state, read Metal. The [host report guide](../region/host-sync.md) explains how the Atlas cache gets its data.

::: details Source code and tests

- [API route](../../atlas/api/routes/webhooks.py) accepts the configuration request.
- [Webhook setup](../../atlas/vm/core/state_webhook.py) creates or updates the two event deliveries.
- [Reported state cache](../../atlas/vm/core/vm_state.py) saves the host reports.
- [Webhook tests](../../atlas/vm/core/test_state_webhook.py) check event conditions and headers.

:::
