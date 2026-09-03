# Metal architecture

[metal SPEC](../SPEC.md) · packages: [internal SPEC](../internal/SPEC.md)

metald runs microVMs on one host. The premise is a stateless client: it turns HTTP
calls into per-VM systemd units and reaches each guest over its own socket. It keeps
no in-memory registry. All per-VM truth lives on disk.

## Components

```text
   HTTP client
       |
       v
   api  (Echo server over vm.VMDriver)
       |
       v
   firecracker.Driver ──> systemd (D-Bus)  ──> metal-vm@<id> ──> jailer ──> firecracker
       |                                                                       ^
       ├──> storage (ZFS)   kernel + rootfs + snapshots                        |
       ├──> network (netns) tap0 + veth + NAT                                  |
       └──> firecracker/api  REST over the VM socket  ─────────────────────────┘
```

metald is a client, not a parent. systemd owns each firecracker process, so a metald
restart or crash never kills a VM. Detail:
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

## Layering

`vm` defines the contract. `firecracker` implements it on the host packages. `api`
serves it. `cmd/metald` is the only place the concretes are built and injected. The
full dependency graph is in [internal/SPEC.md](../internal/SPEC.md).

## Stateless design

```text
per-VM truth on disk:  machines/<id>/config.json, snapshots/, the warmload mark
                       + the systemd unit state
on restart:            Load / List rebuild handles from disk, no registry to recover
```

Detail: [internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md) and
[docs/host-layout.md](host-layout.md).

## systemd unit model

Every VM is the template instance `metal-vm@<id>.service`. metald drives it over
D-Bus. Detail: [internal/systemd/SPEC.md](../internal/systemd/SPEC.md).

## Request flow

The API is synchronous. A handler calls the driver, blocks, and returns the settled
VM state. There is no polling. Detail: [internal/api/SPEC.md](../internal/api/SPEC.md).

## Design notes

Why the main choices were made across the daemon and driver.

**metald (composition root)**

- One base_dir. `machines`, `kernels`, and `images` are a convention under `base_dir`,
  so one value relocates the layout. The images dir must share a filesystem with the
  jails, because a warm start hard-links a memory file into a chroot.
- Interfaces below, concretes here. Each package takes an interface, so the whole
  dependency graph is one readable function and a test swaps a fake.
- Socket auth. A `unix:/path` listener removes a stale socket and sets mode 0660, so
  access control is file permissions. A TCP listener has no auth.

**firecracker driver**

- Client, not parent. systemd owns the firecracker process as `metal-vm@<id>`, so a
  metald restart or crash never kills a VM, and `Load`/`List` rebuild handles from
  disk.
- UUIDv7 ids. They sort by creation time. metald dials a short socket symlink, so the
  id length has no path limit.
- Warm load is deferred and paused. A warm create writes a marker and loads on the
  first `Start`. `LoadSnapshot` runs with `resume_vm=false` so an MMDS refresh (new
  ssh keys and a generation token) lands before the guest runs, letting a clone re-key
  and re-sync.
- Files move out of the chroot. firecracker writes snapshot files as the VM uid inside
  the chroot. metald moves them to `machines/<id>/snapshots/<name>`, which outlives the
  chroot that a relaunch wipes.
- Memory cap sized for snapshot. A full snapshot faults every page in and writes an
  equal-size memory file charged to the same cgroup, so the limit is about 2x guest RAM
  plus 128 MiB, or the cgroup OOM-kills the VM mid-snapshot.
- Idempotent teardown. `Destroy` and `cleanup` ignore an already-gone resource, so a
  partial create or a repeated destroy is safe.

**firecracker API client**

- No dependencies. `net/http` only, so the module carries no third-party API client and
  the surface stays small.
- One socket per client. Each `Client` binds to one VM's socket. The driver makes one
  client per `machine`.
- State strings are firecracker's. `Not started`, `Running`, `Paused`, and the
  `Paused`/`Resumed` transitions are firecracker's vocabulary. The driver maps them to
  `vm.State`.

Detail: [internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md),
[internal/firecracker/api/SPEC.md](../internal/firecracker/api/SPEC.md),
[cmd/metald/SPEC.md](../cmd/metald/SPEC.md).

## Read next

- [docs/vm.md](vm.md) the VM lifecycle and operations.
- [docs/storage.md](storage.md), [docs/networking.md](networking.md), [docs/snapshots.md](snapshots.md).
- [docs/host-layout.md](host-layout.md), [docs/api.md](api.md), [docs/testing.md](testing.md).
