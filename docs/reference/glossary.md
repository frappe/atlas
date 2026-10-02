# Atlas glossary

Find a term by topic. **Atlas** names the whole project. **Atlas app** names its regional control plane.

## Project and components

| Term | Meaning |
| --- | --- |
| Atlas | The complete VM infrastructure project: Atlas app, Metal, HTTP proxy, WG Mesh, and IPv6 router. |
| Atlas app | The Frappe control plane within Atlas. It manages regional requests, hosts, placement, and service VMs. |
| Central | The external Frappe Cloud service that calls Atlas for every tenant. Its code is not in this repository. |
| Cargo | An external service that Atlas installs on a service VM. |
| Metal | The host runtime component. It owns desired and observed VM state on one host. |
| `metald` | The Metal daemon process on each host. |
| Metal Server | One provider host that runs Metal and VMs. |
| WG Mesh | The private IPv6 network between VMs and hosts in one region. Some code and logs call it Atlas WG Mesh. |
| Underlay | The provider's private network between hosts. WG Mesh uses it for host lookups and WireGuard tunnels. |
| HTTP proxy | The regional data plane for HTTP and TLS routes. |

## Virtual machines

| Term | Meaning |
| --- | --- |
| VM | One Firecracker guest. Atlas stores the request and host assignment. Metal runs it. |
| Service VM | A tenant-0 VM that Atlas creates for the HTTP proxy, Cargo, or the IPv6 router. |
| Privileged VM | A tenant-0 VM that WG Mesh lets reach every tenant. Atlas sends the set in host sync. |
| Sleepy VM | A VM that Metal saves and stops after an idle period, then restores on new traffic. See [sleepy VMs](../compute/sleepy-vms.md). |
| Draft | An Atlas Virtual Machine record that reserves identity and capacity before confirmation. |

## State and progress

| Term | Meaning |
| --- | --- |
| Desired state | The state and specification that Metal must apply. |
| Observed state | The host state that Metal last inspected or applied. |
| Desired generation | A number that increases with each real requested change. |
| Observed generation | The desired generation that Metal last applied. |
| Specification generation | A number that increases when the VM shape or network changes. A power change does not raise it. |
| Restart generation | A number that records a saved restart request. |
| Reconciliation | A safe operation that moves observed state toward desired state. |
| Host sync | The periodic Atlas call that sends policy sets to Metal and receives capacity and VM states. |
| Capacity sample | One host capacity result from host sync. Placement ignores a sample older than two minutes. |
| `Virtual Machine State` | The Atlas cache of the last VM state that host sync reported. Metal holds the real state. |

## Images

| Term | Meaning |
| --- | --- |
| System image | A base boot image that Atlas publishes. |
| Machine image | A boot image that Atlas creates from a VM disk. |
| Snapshot staging | Temporary Metal data for transfer to object storage. |
| Warm artifact | Host-local disk, memory, and Firecracker state for a compatible fast boot. |

## Networking

| Term | Meaning |
| --- | --- |
| Mesh address | The stable `fdaa::/16` IPv6 address that Atlas derives for a VM from region, tenant, and VM number. See [address formats](../networking/address-formats.md). |
| Direct public IP | A public address that reaches the VM through its host or provider. |
| Routed public IPv6 | A `/128` from a router block that reaches the VM through the IPv6 router VM. |
| Public address request | The current Atlas request to attach or detach a public address. Its version prevents an old job from completing a newer request. |
| Coordination API | The Metal-to-Metal mutual-TLS API that controls a migration. |
