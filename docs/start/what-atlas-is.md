# What Atlas is

Atlas is the VM infrastructure project for Frappe Cloud. It brings host management, VM execution, storage, and networking into one system.

Here, **Atlas means the whole stack**: the Atlas app, Metal, HTTP proxy, WG Mesh, and IPv6 router.

## The problem it solves

Frappe Cloud offers two things: sites, which run inside VMs, and plain VMs with a raw OS. Both need VMs, and running VMs well takes more than a way to start one. A region needs to:

- Get bare-metal servers from a provider and prepare them as hosts.
- Pick a host with enough room for each new VM.
- Give each VM a disk, a boot image, and its own private network.
- Send public and private traffic to the right VM through the proxy and the mesh.
- Take snapshots, and move a VM to another host when needed.

Each step talks to a different system: the provider, the host, or the network. Any step can fail halfway.

**Atlas is the VM management plane for a region.** It records requested work and coordinates the components that complete it. Saved state lets each owner inspect progress and retry after a failure.

## From a request to a usable VM

A request expresses what should exist. Atlas selects a host, records the request, prepares the guest, and connects its network.

The system keeps both the request and its progress. After a timeout or restart, it can check what happened and continue from saved state.

This is the central idea: **save the request, make the host match it, and check the result**. [How a VM request works](how-a-vm-request-works.md) shows each step.

## Design goals

**Reliability comes first.** Every design choice aims to keep VMs running and to recover cleanly from a crash, a lost response, or a restart. Follow these rules when you add a feature:

| Goal | What it means |
| --- | --- |
| Keep host state local | Metal stores each VM's desired and observed record. It derives names and addresses from IDs or reads host resources such as systemd and ZFS. |
| Keep requests separate from reports | Atlas stores requests and host assignments. Its `Virtual Machine State` record caches the last host report, which can be old. |
| Save the requested state | Callers send the full VM state they want. Metal stores it before it changes the host. |
| Make retries safe | Use stable IDs and check saved state before repeating a step after a timeout. |
| Fail loudly | A corrupt record stops management of its VM and is reported, instead of being ignored. An uncertain result stays visible until its owner confirms it. |

## Where Atlas fits in Frappe Cloud

| System | Responsibility |
| --- | --- |
| Atlas | Regional VM infrastructure: hosts, guests, disks, images, addresses, and traffic. |
| Central | An external Frappe Cloud service that issues tokens used by Atlas. Its full implementation is outside this repository. |
| Cargo | An external service that Atlas installs on a service VM for usage and object storage work. |

The Atlas app makes regional decisions and stores requests. Metal runs VMs on hosts, and network services carry their traffic. See [architecture](architecture.md) for those boundaries.

## Scope and status

One Atlas instance manages **one region with one provider**. It does not connect regions or span providers.

Atlas is in pre-production. The handbook describes the code in this repository. Read [status and limitations](../reference/status.md) before you plan a feature or an operation.

::: details Source code and tests

- [Root specification](../../SPEC.md) maps the components and their ownership.
- [Atlas app specification](../../atlas/SPEC.md) maps the control-plane modules.
- [Metal specification](../../metal/SPEC.md) maps the host runtime.
- [Atlas API router](../../atlas/api/router.py) is the tenant API entry point.
- [Service module specification](../../atlas/service/SPEC.md) identifies the Atlas services that run on VMs.
- [VM creation tests](../../atlas/vm/core/test_vm_service.py) show the draft and host-request contract.

:::
