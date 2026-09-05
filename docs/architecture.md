# Atlas system architecture

Atlas and Metal form one virtual machine control system. Atlas owns controller intent and manages Metal. Metal owns host intent and host state.

## Ownership

```text
API and Desk
      |
      v
Atlas app
  provider resources, Server records, placement, images, user actions
      |
      | Metal HTTP API
      v
Metal on one host
  desired VM state, observed VM state, reconciliation, cleanup
      |
      v
Firecracker, systemd, Linux network, ZFS
```

| Owner | Mutable state |
|---|---|
| Atlas | Provider resources, Server records, placement requests, image records, public IPv4 intent |
| Metal | Virtual machine desired state, observed state, host resources, cleanup progress |
| systemd | Each Firecracker process |
| Firecracker runtime | Runtime files, sockets, machine configuration, metadata, and console connections |
| Atlas WG Mesh | Private network forwarding between hosts |
| HTTP proxy | Regional HTTP and TLS routes |

Atlas does not copy the Metal lifecycle state into durable DocType fields. Atlas can expose current Metal values as virtual fields.

## Virtual machine creation

```text
Atlas selects and locks a Server
  -> Atlas commits a draft with a stable VM ID
  -> Atlas sends one idempotent create request
  -> Metal stores desired state
  -> Metal returns 202
  -> Metal reconciles runtime, network, and storage state
  -> Atlas reads desired and observed state
```

Atlas keeps a draft when a create result is uncertain. Atlas removes the draft only after Metal confirms that the VM is absent.

## Server creation and provisioning

```text
Server.before_validate
  -> validate Atlas Settings and provider catalog records
  -> ensure the named provider server
  -> apply provider values to the Server document
  -> insert the Server document
  -> queue ServerProvisioner
```

`ServerProvisioner` prepares provider resources, waits for root Secure Shell access, and configures the provider network. It then configures WireGuard and installs Metal. Each step stores useful progress.

The insert path deletes a remote server only when the current request created it. A retry reuses the provider server by its stable Atlas identity.

## Capacity synchronization

Atlas sends the complete peer, image, and privileged address sets to each Metal host. Metal returns a current capacity sample.

Placement matches the image architecture. It also subtracts Atlas reservations that are newer than the selected capacity sample.

## Images and snapshots

Atlas owns public image records and object storage transfers. Metal owns host image data, guest disks, and local snapshot staging.

```text
Object storage -> Metal image cache -> VM disk
VM disk -> Metal snapshot staging -> object storage -> Atlas Machine image
```

Warm Firecracker state stays on one host. Atlas never uploads guest memory or Firecracker state.

## Failure boundaries

- A provider failure affects Server creation or provider resources. It does not change Metal state.
- An Atlas-to-Metal failure can make a request uncertain. Atlas keeps its draft or intent until a read resolves it.
- A Metal runtime failure keeps desired state and error data for reconciliation.
- A host resource cleanup failure keeps independent cleanup progress until all owners report success.
- A proxy failure affects traffic routing. It does not own virtual machine lifecycle state.
- An Atlas WG Mesh failure affects private connectivity. It does not own Metal records.

## Contracts

- [Metal target API contract](metal-v1-contract.md)
- [Atlas provider contract](../atlas/docs/providers.md)
- [Server lifecycle](../atlas/docs/server-lifecycle.md)
