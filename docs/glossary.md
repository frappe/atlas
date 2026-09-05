# Atlas glossary

| Term               | Meaning                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------- |
| Atlas              | The Frappe controller that owns provider integration and user operations.                |
| Metal / Metald     | The host daemon that owns virtual machine desired state and observed state.              |
| Server             | One provider host that runs Metal and virtual machines.                                  |
| Virtual machine    | One guest resource managed by Metal.                                                     |
| Desired state      | The state and specification that Metal must apply.                                       |
| Observed state     | The host state that Metal last inspected or applied.                                     |
| Generation         | A number that increases when desired specification data changes.                         |
| Restart generation | A number that records durable restart intent.                                            |
| Reconciliation     | A safe operation that moves observed state toward desired state.                         |
| Draft              | An Atlas Virtual Machine record that reserves identity and capacity before confirmation. |
| Capacity sample    | One host capacity result from a Metal synchronization request.                           |
| Provider server    | The remote bare-metal resource that backs an Atlas Server.                               |
| System image       | A base boot image that Atlas publishes.                                                  |
| Machine image      | A boot image that Atlas creates from a virtual machine disk.                             |
| Snapshot staging   | Temporary Metal data for transfer to object storage.                                     |
| Warm artifact      | Host-local disk, memory, and Firecracker state for a compatible fast boot.               |
| Public IPv4 intent | The current Atlas request to attach or detach a provider address.                        |
| Atlas WG Mesh      | The private IPv6 network between virtual machines and hosts.                             |
| HTTP proxy         | The regional data plane for HTTP and TLS routes.                                         |
