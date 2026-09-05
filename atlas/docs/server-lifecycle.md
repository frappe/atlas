# Server lifecycle

Atlas creates one provider server before it inserts the matching Server document. It then runs setup in a background job.

## Creation

`Server.before_validate` uses this sequence:

1. Validate Atlas Settings and both provider catalog records.
2. Stop if the document already has `provider_server_id`.
3. Call `ensure_server` with the stable Server name.
4. Apply the returned provider values to the document.
5. Record whether this request created the provider server.

If document insertion fails, Atlas deletes the provider server only when this request created it. A reused server remains available for retry.

## Provisioning

`ServerProvisioner` uses this sequence:

1. Prepare the provider infrastructure and network attachment.
2. Wait for root Secure Shell access.
3. Configure the provider host network.
4. Configure the existing WireGuard interface.
5. Install Metal.
6. Set the Server status to `Running`.

Each successful step stores the current Server setup fields. The provisioner owns each commit during this long operation.

A retry runs the sequence again. Each external operation must be safe to repeat.

## Failure behavior

The provisioner keeps useful fields and sets the Server status to `Failed`. It writes the Server name, operation, and failed phase to logs.

The Error Log gives the failed phase. It does not contain credentials, signed URLs, or command output.

Use the existing Setup Server action after you correct the cause. Atlas does not add an operation DocType for setup progress.

## Desk methods

The existing whitelisted methods remain the public boundary:

- `ping_server`
- `setup_server`
- `configure_wireguard`
- `reboot_server`
- `poweroff_server`
- `poweron_server`
- `archive_server`
- `sync_disks`
- `install_metald`

These methods check permissions and local state. Dedicated server objects perform the long operations.
