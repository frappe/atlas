# Provision a Metal host

A Metal Server is Atlas's record of one physical host. When you create one, Atlas gets a machine from the provider and installs Metal on it. The record keeps the provider ID and setup progress, so a failed setup can be retried.

## Provisioning sequence

A new Metal Server queues a background job:

1. Create or reuse the provider host.
2. Ask the provider to prepare it and wait for root SSH access.
3. Configure the provider network and WireGuard, with the Atlas peer on `wg0`.
4. Wait for root SSH on the host `wg0` address.
5. Install storage, TLS credentials, Metal, WG Mesh, systemd units, and the host firewall.
6. Mark the host `Running` and queue disk inventory sync.

The provider adapter supplies host-specific operations. An adapter for a manually prepared host can expect some resources to exist already.

### What the status tells you

Setup moves through `Pending`, `Installing`, and `Running`. A failed step sets `Failed`. An operator retry returns it to `Pending`. `Stopped` and `Deleted` come from provider state.

Atlas logs each phase and commits important fields after successful steps. On failure, it keeps the provider ID and setup data. A retry returns to `Pending` and can reuse the same host.

### Installation inputs

The installation needs:

- A selected ZFS pool device.
- A private interface and address.
- Atlas-built binaries.
- A regional certificate.

Atlas writes the control and coordination listeners on the WireGuard address. [Atlas access to hosts](host-access.md) explains the path. The install script creates the ZFS pool. `metald` does not select its device.

## Failure and recovery

1. Read the Metal Server status and provider ID.
2. Find the failed phase in the Error Log and linked SSH Tasks.
3. Correct the provider, SSH, network, or installation fault.
4. Retry setup with the existing record.

**A failed Atlas record can still have a real provider host.** Keep its identity. Also check host sync: `Running` alone does not guarantee a fresh placement sample.

**Details:** [Host administration](hosts-and-providers.md) and [Metal startup](metald.md). The [Metal API](/api/metal/) starts after installation.

::: details Source code and tests

- [Metal Server controller](../../atlas/metal_server/doctype/metal_server/metal_server.py) queues setup and retry.
- [Provisioner](../../atlas/metal_server/core/provisioning.py) owns step order, commits, and failure status.
- [Host installation](../../atlas/metal_server/core/host_installation.py) sends WireGuard, TLS, and Metal setup work.
- [Host storage script](../../atlas/scripts/install-metal-storage.sh) creates the ZFS pool and mounts host state on it.
- [Host install script](../../atlas/scripts/install-metald.sh) creates host services.
- [Provisioning tests](../../atlas/metal_server/core/test_provisioning.py) check repeat and failure behavior.

:::
