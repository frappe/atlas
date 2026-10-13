# Operate a Metal host

Use the **VM ID and operation ID** to connect API responses, records, logs, units, namespaces, and datasets.

## Before you retry

Read `GET /v1/vms/{id}`. Compare desired and observed generations, then inspect the phase and error. A timeout or stale Atlas view does not prove rejection.

Correct the host fault and let reconciliation retry. Keep record files, saved state, and migration data intact. Do not create duplicate units, namespaces, or datasets, or print private keys and signed URLs.

## Choose a check

| Symptom | Start with |
| --- | --- |
| Guest state or console | `metal-vm@<id>.service`, its journal, Firecracker socket, console adoption. |
| Network | Namespace links, routes, firewall, and mesh health. |
| Storage or sync | `zpool status`, `zfs list`, configured pool, Metal journal. |
| Snapshot or migration | Status, checkpoint, free storage, and both sides' logs. |

## Network setup fails

A VM stays failed or has no expected connectivity.

**Check**

1. Read `GET /v1/vms/{id}` and `observed.error`.
2. Run `ip netns list`, `ip -n metal-<id> link`, and `journalctl -u metal.service --since "15 minutes ago"`.

**Expected:** Logs name the VM, operation, and failed network step. The namespace and links show the partial state.

**Recover:** Correct the host interface, route, or firewall fault. Let reconciliation call `Network.Ensure` again.

Do not create an alternate namespace for the same VM. Do not change stored desired network JSON by hand.

## Firecracker and systemd report different states

Metal reports a state that does not match `metal-vm@<id>.service`.

**Check**

1. Run `systemctl status metal-vm@<id>.service`, `systemctl show metal-vm@<id>.service`, and `journalctl -u metal-vm@<id>.service`.
2. Check the Firecracker socket link under `/run/metal`.

**Expected:** Metal inspection records each partial fault. Systemd remains the process owner.

**Recover:** Correct the unit or runtime-file fault. Wake reconciliation with the same desired request when required.

Do not start Firecracker directly. Do not remove a running unit's files.

## Serial console is blank or unavailable

The console closes, or it has no output.

**Check**

1. Confirm `/run/metal/consoles/<id>` and the `TTYPath`, `StandardInput`, and `StandardOutput` settings.
2. Read `NotifyAccess`, `FileDescriptorStoreMax`, and `FileDescriptorStorePreserve` for `metal.service`.
3. Run `metald version`. Confirm that the build supports the descriptor store.

**Recover:** Correct the unit settings, run `systemctl daemon-reload`, and restart the VM through the API.

Do not restart `metal-vm@<id>.service` or remove a running guest's console link.

## VMs stop when metald stops

Every VM on the host fails with exit code 156 when `metal.service` stops or restarts.

Firecracker holds the PTY slave as its controlling terminal. A closed PTY master sends SIGHUP to Firecracker. systemd keeps the master in the descriptor store, so the PTY survives a metald exit.

**Check**

1. Run `systemctl show metal.service -p NFileDescriptorStore`. Expect one descriptor per running VM, plus two listeners. `0` means no protected consoles.
2. Read the startup log. Expect `descriptor_store_available=true` and `adopted_console_count` equal to `running_unit_count`.
3. Confirm `NotifyAccess=main`, `FileDescriptorStoreMax`, and `FileDescriptorStorePreserve=yes` in the unit.

If descriptors disappear, use this diagnostic sequence:

1. Run `systemd-analyze log-level debug`.
2. Start the VM through the API.
3. Read the journal for `Added fd` and `Received EPOLLHUP on stored fd`.
4. Restore the log level with `systemd-analyze log-level info`.

**Recover:** Correct the unit settings and run `systemctl daemon-reload`. Start each VM again through the API. A running VM keeps its unprotected console until it starts again.

Do not start or restart `metal-vm@<id>.service` by hand.

## Disk usage or ZFS inspection fails

`/v1/sync` fails or a VM operation reports storage inspection failure.

**Check**

1. Run `zpool status`, `zfs list`, and `journalctl -u metal.service --since "15 minutes ago"`.
2. Check the configured pool name.

**Expected:** Metal returns no invented capacity value. Logs identify the failed inspection.

**Recover:** Restore the pool and retry synchronization or reconciliation.

Do not publish a guessed capacity. Do not destroy a dataset until ownership is confirmed.

## Snapshot upload stays pending or completing

Atlas does not receive completed artifact hashes, or multipart completion does not finish.

**Check**

1. Read `GET /v1/snapshots/{id}`.
2. Check the snapshot activity time and Metal logs.
3. In Atlas, check image status and upload IDs.

**Expected:** Metal reports pending, uploading, completed, or failed with progress. Atlas keeps retry identifiers.

**Recover:** Correct network or object storage access. Retry from Atlas. The Metal start request and the multipart completion are safe to repeat.

Do not log signed URLs. Do not remove staging during an active retry.

## Machine image cleanup fails

An image is Available or Cleaning, but Metal staging remains.

**Check**

1. Read the Atlas image status.
2. Call `GET /v1/snapshots/{id}`.
3. Check Metal cleanup logs and ZFS state.

**Expected:** The public image objects are complete before Atlas requests staging deletion.

**Recover:** Send the same `DELETE /v1/snapshots/{id}` request or wait for stale-staging cleanup.

Do not delete public image objects. Do not remove a ZFS snapshot that another clone uses.

## Metal cannot complete graceful shutdown

`metald` does not stop within the configured shutdown bound.

**Check**

1. Read `journalctl -u metal.service`.
2. Check shutdown, upload cancellation, and wait-group log entries.
3. Check whether the process still accepts requests.

**Expected:** Shutdown cancels upload roots, stops background owners, and waits for their bounded completion. Guest systemd units remain active.

**Recover:** Allow the configured bound. Stop the daemon service again after the worker exits. Use process termination only after you preserve logs.

Do not stop `metal-vm@*.service` units as part of daemon shutdown. Do not remove upload state while a worker is active.

## Automatic idle shutdown does not stop or restore a VM

An idle VM does not reach `stopped`, or new IP traffic does not restore it.

**Check**

1. Read `GET /v1/vms/{id}`.
2. Check `desired.compute.sleep_after_idle_seconds`, `observed.state`, and `observed.error`.
3. Run `systemctl show -p MainPID --value metal-vm@<id>.service`. An automatically stopped VM reports `0`.
4. Check `<base_dir>/machines/<id>/saved-state/metadata.json`, `bpftool map show`, `ip netns exec metal-<id> bpftool link show`, and the metald journal.

**Expected:** Host-to-guest IPv4 or IPv6 traffic updates `activity_by_user_id`. When observation is active, it also writes the VM user ID to `traffic_events`.

**Recover:** Retry the connection because the first packet can be lost during restoration. Correct the timeout or the host eBPF fault and let reconciliation retry.

Do not delete a saved state that Metal must restore. Do not start Firecracker directly.

## Limits and recovery

Metal logs are local to the host, and the observed VM record is only a host-side report. A clean desired/observed generation match does not prove guest application health. A daemon exit without a working systemd descriptor store can stop running guests even though the VM records remain intact.

::: details Source code and tests

- [VM reconciliation](../../metal/internal/vm/reconcile.go) records phases, errors, and cleanup progress.
- [Daemon startup](../../metal/cmd/metald/main.go) reports console adoption.
- [Daemon shutdown](../../metal/cmd/metald/daemon.go) defines worker and listener cleanup.
- [Network allocator](../../metal/internal/network/linux_allocator.go) repairs VM host networking.
- [Migration engine](../../metal/internal/vm/migration/manager.go) owns destination and source records.

:::
