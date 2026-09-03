# firecracker/api: REST client over the VM socket

[firecracker SPEC](../SPEC.md) · overview: [docs/architecture.md](../../../docs/architecture.md)

## Purpose

Package `api` speaks Firecracker's REST API over a virtual machine's Unix socket.
The premise is a tiny, dependency-free client: `net/http` with a custom dialer that
always connects to one socket. The URL host is ignored. Nothing outside the standard
library is used.

## Types

| Type | Role |
|---|---|
| `Client` | An `http.Client` whose dialer binds to one VM's socket. `New(sockPath)`. |
| models | Request and response structs: `MachineConfig`, `BootSource`, `Drive`, `PartialDrive`, `NetworkInterface`, `MmdsConfig`, `InstanceInfo`, `CreateSnapshotReq`, `MemBackend`, `LoadSnapshotReq`. |
| `Error` | `{Status, Message}`. Reads firecracker's `fault_message`. |

## Transport

```text
Client.do(method, path, body, out):
   request URL = http://localhost<path>          host ignored
   DialContext always dials  unix:<sockPath>
   marshal body (JSON) ; on 2xx decode out (JSON)
   status >= 300 -> decodeFault -> *Error   "firecracker api: <code>: <fault_message>"
```

## Methods

| Go method | HTTP | Route | Purpose |
|---|---|---|---|
| `PutMachineConfig` | PUT | `/machine-config` | vCPU count and memory size. |
| `PutBootSource` | PUT | `/boot-source` | Kernel path and boot args. |
| `PutDrive` | PUT | `/drives/{id}` | Attach a drive. |
| `PatchDrive` | PATCH | `/drives/{id}` | Rescan the drive after a host-side resize. |
| `PutNetworkInterface` | PUT | `/network-interfaces/{id}` | Attach the TAP with a MAC. |
| `InstanceStart` | PUT | `/actions` | Boot the guest. |
| `InstanceInfo` | GET | `/` | Read the runtime state. |
| `SendCtrlAltDel` | PUT | `/actions` | Ask the guest to shut down. |
| `Pause` | PATCH | `/vm` | Halt the vCPUs. |
| `Resume` | PATCH | `/vm` | Run the guest again. |
| `CreateSnapshot` | PUT | `/snapshot/create` | Write state and memory. The VM must be paused. |
| `LoadSnapshot` | PUT | `/snapshot/load` | Restore into a fresh firecracker process. |
| `PutMmdsConfig` | PUT | `/mmds/config` | Enable the metadata service on an interface. |
| `PutMmds` | PUT | `/mmds` | Set the metadata payload. |

## Related

- [firecracker SPEC](../SPEC.md) the caller and the boot, snapshot, and stop flows.
- [docs/networking.md](../../../docs/networking.md) the metadata service (MMDS) the guest reads.
