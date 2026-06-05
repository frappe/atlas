# Virtual machine lifecycle

The core lifecycle is **provision, start, stop, terminate**. On top of it sit
the disk- and state-management operations: **snapshot, restore/rebuild, clone,
resize, pause/resume**. Each operation is exactly one Task running one
idempotent shell script.

Two design rules keep this set small and safe:

- **Snapshots are disk-only.** A snapshot is an LVM thin CoW snapshot of the
  VM's disk LV — not a Firecracker memory-state snapshot. We never call
  Firecracker's `/snapshot/create` or `/snapshot/load`. This dodges the
  pre-boot-only load path (which can't coexist with our `--config-file`
  boot), the RAM-sized memory file, and the duplicate-identity hazard the
  [Firecracker docs](../../references/firecracker/docs/snapshotting/snapshot-support.md)
  call insecure. The boot path and the systemd unit are unchanged.
- **Disk operations require the VM to be Stopped.** A thin snapshot of (or a
  replacement under) an ext4 the guest still has mounted captures a torn,
  mid-write filesystem; a cleanly unmounted disk LV is consistent. Firecracker also can't change vCPU/RAM on a running VM
  (`/machine-config` is pre-boot only), so resize is stop-required too. The
  desk surfaces these actions only while Stopped; the controllers enforce
  it.

The only operation that touches Firecracker's API socket is **pause/resume**
(`PATCH /vm {Paused|Resumed}`) — a runtime vCPU freeze that keeps RAM
resident, distinct from Stop.

## Identity

A `Virtual Machine.name` is a **UUID** assigned at insert. It never changes —
including on terminate. This means:

- The on-host directory path
  (`/var/lib/atlas/virtual-machines/<uuid>/`) is stable forever.
- The systemd unit instance name (`firecracker-vm@<uuid>.service`) is stable.
- Tasks referencing the VM stay valid after terminate.
- The operator does not have to invent a name; they use `title` for a
  human-readable label (the framework's `title_field`).

The MAC and TAP device are derived from the UUID so they are also stable.

## States

```
                  (insert via Create form — Save)
                              |
                              v
                          Pending ----(provision fails)----> Failed
                              |                                 |
                  (auto_provision worker)            (Provision retry)
                              |                                 |
                              v                                 |
                          Running <----------------------------+
                          ^   |  ^
                  (Resume)|   |  |(Start)
                          |   |  |
                       Paused |  Stopped
                          ^   |   ^  |
                   (Pause)|   +---+  |  (Snapshot / Rebuild / Restore / Resize
                          |  (Stop)  |   all stay Stopped)
                          +----------+

       (Terminate from any non-Terminated state) ---> Terminated
```

Statuses: `Pending`, `Running`, `Paused`, `Stopped`, `Failed`, `Terminated`.

Status checks treat this as an **open set** — controllers guard on the
specific states a transition allows, never "anything but X". `stop()` accepts
`Running` *or* `Paused`; `pause()` only `Running`; `resume()` only `Paused`;
`start()` only `Stopped`; `restart()` only `Running`/`Stopped` (a Paused VM
resumes or stops first). The disk operations (snapshot, rebuild, restore,
resize) require `Stopped`.

Two transitions carry an additional, operator-set **protection** gate
orthogonal to status (see [Stop / Terminate protection](#stop--terminate-protection)):
`stop()` is refused while `stop_protection` is set, and `terminate()` while
`termination_protection` is set. Both default off; both are hard throws, not
confirmations.

There is no transient `Provisioning` status — the Task row is the "in-flight"
record; the VM row only moves to `Running` after a successful Provision Task,
and stays at `Pending` if it fails (re-clickable because the script is
idempotent).

`Paused` keeps the microVM's RAM resident with vCPUs frozen; the systemd unit
is still active. It is reached only from `Running` and leaves to `Running`
(resume) or `Stopped` (stop = full shutdown).

`Terminated` is terminal. The doc stays in the table forever for history;
terminating a VM also deletes its snapshot rows. Each snapshot row's `on_trash`
lvremoves its snapshot LV — snapshot LVs live in the thin pool, outside the VM
directory, so they survive `terminate-vm.py`'s `rm -rf` and must be removed
explicitly (one Task each).

## Provision

Trigger: operator fills the Create form (server, image, vCPUs, RAM,
disk, SSH key, title) and clicks `Save`. `Virtual Machine.after_insert`
enqueues `auto_provision` on the `long` queue; the worker calls
`Virtual Machine.provision()` on the freshly inserted row. There is no
operator-facing `Provision` primary on a `Pending` form — saving *is*
the provision trigger. The `Provision` primary returns on `Failed` as
a manual retry path.

Steps in Python (one DocType method, `Virtual Machine.provision`):

1. **Allocate networking values** in the Frappe DB:
   - `ipv6_address`: next free address in `Server.ipv6_virtual_machine_range`.
     The allocator selects `Server` for update, scans existing
     `Virtual Machine.ipv6_address` for that server, picks the next, commits.
   - `mac_address`: `06:00:` + first 4 bytes of the UUID, hex-formatted.
   - `tap_device`: `atlas-` + first 9 chars of the UUID with `-` removed.
     Linux `IFNAMSIZ` is 16 *bytes* including the null terminator, so the
     usable interface-name length is 15: `atlas-` (6) + 9 = 15 exactly.

2. **Run the provisioning task**:
   `run_task(server=name, script="provision-vm.py", variables=…,
   virtual_machine=name)`. The script's step 0 verifies the image is on the
   server; if not, it exits non-zero with a clear error pointing the operator
   at the **Sync to Server** action. Provision does not auto-sync — image
   sync is a multi-minute operation and we want it deliberate, predictable,
   and visible as its own Task. The remaining steps (thin-snapshot the base
   image LV into the VM's disk LV, resize,
   SSH key injection, per-VM hostname `atlas-<first-8-of-uuid>` written to
   `/etc/hostname` and `/etc/hosts`, 512 MiB `/swapfile`, fresh per-VM
   `/etc/ssh/ssh_host_*` keypairs, per-VM `/etc/machine-id`, config
   write, systemd enable+start) happen inside the same SSH session.
   The per-VM identity writes share the rootfs mount with the SSH-key
   injection — no per-VM systemd unit needed. See
   [`atlas/scripts/provision-vm.py`](../scripts/provision-vm.py).

3. **Update status**: on Task success, `status = Running`,
   `last_started = now()`.

One Task per VM creation. (The image sync, if needed, is a separate Task
triggered explicitly by the operator before provisioning.)

### Host-side precondition

Before the guest-side probe runs, the e2e suite asserts the Atlas
host carries the SSH key on disk as
[07-filesystem-layout.md § SSH keys](./07-filesystem-layout.md)
describes: `Atlas Settings.ssh_private_key_path` resolves to a regular
file with mode `0600` (or `0400`, equally safe). This is a Python-side
check in
[`use_cases/virtual_machine_provisioning.py::_assert_provider_ssh_key_path`](../atlas/tests/e2e/use_cases/virtual_machine_provisioning.py),
not a bash probe — the file lives on the Atlas host, not in the guest.
A missing or wrong-mode key surfaces here as a clean AssertionError
rather than as a noisy SSH timeout in the guest probe.

### Guest-side identity contract

A freshly provisioned VM presents the following to an operator who SSHes
in. These are the contract `provision-vm.py` writes and the e2e suite
([`phase5-guest-identity.sh`](../atlas/tests/e2e/scripts/phase5-guest-identity.sh))
asserts on every run:

- `hostname` is `atlas-<first-8-of-uuid>`. Same string in `/etc/hostname`
  and as a `127.0.1.1` entry in `/etc/hosts`.
- `/etc/machine-id` is unique per VM (derived from the UUID; the leaked
  CI value `4833ad8775a24dcc9d4b159af4e84d08` is gone).
- `/etc/ssh/ssh_host_*` keypairs are unique per VM — generated on the
  host at provision time with `ssh-keygen` and written into the mounted
  rootfs. The CI build-container comment `root@bf0feaa40806` does not
  appear.
- The only global IPv4 on `eth0` is the Atlas NAT44 egress address
  (`100.64.x.x/30`, see [06-networking.md](./06-networking.md)). The
  `fcnet.service` that derived a phantom `91.83.x.x/30` from the MAC is
  removed at image-sync time, so any *non-`100.64`* global v4 is a
  regression. (The egress address and its reachability are asserted
  separately by the `phase5-ipv4-egress.sh` probe.)
- `/etc/hosts` has no Docker bridge leftover; just localhost, the
  per-VM 127.0.1.1 line, and the ip6-* aliases.
- Root password locked (`root:!:` in `/etc/shadow`). `sshd -T` reports
  `passwordauthentication no` — key-only by contract.
- `/swapfile` is active swap (512 MiB by default), referenced by the
  `/etc/fstab` installed at image-sync time.

This list is short for a reason: it is the operator-visible delta
between a stock Ubuntu cloud image and a VM that looks like the
operator's own. When the upstream image changes, every bullet either
stays a no-op (good) or needs a new strip (a regression to fix in
`sync-image.py`).

## Start / Stop / Restart

Each is a single Task running a one-line script:

- `start-vm.py`: `systemctl start firecracker-vm@<name>.service`
- `stop-vm.py`: `systemctl stop firecracker-vm@<name>.service`
- `terminate-vm.py`: see below

Restart is `stop-vm.py` then `start-vm.py`, but as the Python method's
choice — we do not add a `restart-vm.py`, because the only thing `systemctl
restart` adds is one fewer network round-trip and we already paid for both.

Status updates happen after the Task succeeds. We do not poll the server
to verify; the source of truth is the Task. If the operator wants ground
truth, they click `Run Task` with `script=systemctl status ...`.

## Stop / Terminate protection

Two optional, operator-set flags on `Virtual Machine` guard the destructive
transitions, independent of status:

- `stop_protection` gates `stop()` — and therefore `restart()`, which stops
  first.
- `termination_protection` gates `terminate()`.

Both **default off** (a new VM is freely stoppable and terminable, as before)
and both are **hard throws**, not confirmations: a protected `stop()`/
`terminate()` raises ("Disable stop/termination protection before …") and runs
no Task. To proceed, the operator unchecks the flag, **saves** the VM, then
clicks the action — the same deliberate two-step shape as the immutability
throws. The check is in the controller (`stop()` / `terminate()`), so it holds
on every path (desk button, SPA, direct API), not just the desk.

The two flags are independent. `terminate()` does not route through `stop()`
(it `systemctl disable --now`s the unit directly via `terminate-vm.py`), so a
VM can be termination-protected but freely stoppable, or stop-protected but
terminable — whichever the operator chose. Protection is purely a Frappe-side
guard on *initiating* the operation; it changes no on-host state and is not
consulted by any script.

## Pause / Resume

The only operations that talk to Firecracker's API socket. Each is one Task
running a one-line `curl`:

- `pause-vm.py`: `PATCH /vm {"state":"Paused"}` over the in-jail socket
  `…/<uuid>/jail/firecracker/<uuid>/root/run/firecracker.socket`.
  `Running` → `Paused`.
- `resume-vm.py`: `PATCH /vm {"state":"Resumed"}`. `Paused` → `Running`.

`curl --fail` so a refused state change surfaces as a failed Task rather than
a silent success. Idempotent: Firecracker accepts a redundant Pause/Resume.
RAM stays resident across a pause — this is *not* a shutdown. The boot path is
still `--config-file` (forwarded through the jailer); the socket is created by
Firecracker inside its jail and used only for these post-boot operations. It is
a host-filesystem unix socket, so the VM's network namespace does not affect
reaching it — `curl --unix-socket` talks to it from the host as before.

## Snapshot

`Virtual Machine.snapshot(title)` on a **Stopped** VM. Runs
[`snapshot-vm.py`](../scripts/snapshot-vm.py):

1. Pre-flight thin-pool check — refuse if the pool's `data_percent` or
   `metadata_percent` is ≥ 90%. A thin snapshot consumes no space up front, but
   every subsequent CoW write allocates from the pool; taking snapshots against
   an almost-full pool courts a pool-exhaustion stall. The
   [Firecracker docs](../../references/firecracker/docs/snapshotting/snapshot-support.md)
   warn unbounded snapshots are a DoS vector; pool-space accounting is the
   guard (no quota system this iteration).
2. `lvcreate -s atlas-vm-<uuid> -n atlas-snap-<snapshot-uuid>` — an instant CoW
   thin snapshot of the VM's disk LV. Pure host op, no jail interaction; the
   snapshot shares the disk's blocks until one side is written.
3. Emit the typed result `ATLAS_RESULT={"size_bytes": <n>}` (from `blockdev
   --getsize64` on the snapshot LV), which the controller parses back with
   `task_results.parse_result()` — the typed successor to the old `SIZE_BYTES=`
   stdout scrape.

The controller inserts a `Virtual Machine Snapshot` row (`Pending`), runs the
Task, then records `rootfs_path` (the snapshot's `/dev/atlas/atlas-snap-<uuid>`
device path), `size_bytes`, and flips it to `Available`. One snapshot = one row
= one thin LV. Deleting the row runs
[`delete-snapshot-vm.py`](../scripts/delete-snapshot-vm.py) via `on_trash`,
which `lvremove`s the snapshot LV — always, even for a Terminated VM, because
the snapshot LV lives in the pool (outside the VM directory) and is not swept by
terminate's `rm -rf`. See
[02-doctypes.md § Virtual Machine Snapshot](./02-doctypes.md#virtual-machine-snapshot).

## Restore / Rebuild

One controller method, `Virtual Machine.rebuild(source_type, source)`, on a
**Stopped** VM. It replaces the VM's disk LV while keeping its identity
(name/UUID, IPv6, MAC, tap, SSH key). Two sources:

- `source_type="snapshot"` — **Restore**: roll the disk back to one of this
  VM's own snapshots. `source` is the snapshot row name; it must belong to
  this VM and be `Available`. (The Snapshot form's **Restore to VM** button
  calls the thin wrapper `Snapshot.restore_to_vm()`.)
- `source_type="image"` — **Rebuild**: lay down a fresh disk from a base image
  (wipes stored data). `source` defaults to the VM's current image.

Both run [`rebuild-vm.py`](../scripts/rebuild-vm.py): `lvremove` the old disk
LV, recreate it as a fresh CoW snapshot of the source LV (a snapshot LV for
Restore, the base image LV for Rebuild), grow it to the VM's disk size, then
re-inject this VM's identity (SSH key, network env, hostname, swap, fresh host
keys, machine-id) via the shared `atlas.rootfs` module (the Python successor to
the `prepare-rootfs.sh` library), and re-`mknod` the
jail's `rootfs.ext4` block node (the new LV's dev_t can differ). Because identity
is re-derived from the VM's own UUID, a restored/rebuilt VM is indistinguishable
from a freshly provisioned one of the same name. The VM stays `Stopped`; the
operator starts it when ready.

## Clone (create from snapshot)

`Virtual Machine Snapshot.clone_to_new_vm(title, ssh_public_key, …)` creates a
**new** VM whose initial disk is seeded from the snapshot's rootfs. The clone
is a fresh VM — new UUID, IPv6, MAC, SSH host keys and machine-id (all
re-derived at provision). It is a *disk template*, not a memory-state resume:
the safe path that avoids the duplicate-identity hazard of resuming the same
running state twice.

Mechanically the clone reuses the normal provision flow: the new VM row
carries an internal `clone_source_rootfs` field (the snapshot's LV device
path), and `provision-vm.py` snapshots the clone's disk LV from that snapshot
LV instead of the base image LV (the kernel still comes from the image, so the
image must be synced). A snapshot-of-a-snapshot is an independent thin LV — the
clone never shares writable blocks with its source. Disk defaults to the
snapshot's size and can only grow.

## Resize

`Virtual Machine.resize(vcpus, memory_megabytes, disk_gigabytes)` on a
**Stopped** VM. Firecracker reads `/machine-config` only at boot, so resize is
stop-required; the next Start picks up the new config. Runs
[`resize-vm.py`](../scripts/resize-vm.py): `jq`-edit `vcpu_count` /
`mem_size_mib` in `firecracker.json`, then `lvextend -r` the disk LV to the new
size (grows the LV and the ext4 on it in one shot). Disk may only **grow** —
`lvextend` refuses to shrink and is a clean no-op when the size is already met.
Unspecified fields keep their current value. The new
values are persisted on the row through a guarded path (see
[Why resource fields are frozen outside resize](#why-resource-fields-are-frozen-outside-resize)).

## Terminate

Runs [`terminate-vm.py`](../scripts/terminate-vm.py), which:

1. `systemctl disable --now firecracker-vm@<uuid>.service` (no-op if already
   stopped).
2. Calls `vm-network-down.py` defensively in case the unit's `ExecStopPost`
   didn't fire.
3. `rm -rf /var/lib/atlas/virtual-machines/<uuid>` (takes the jail tree,
   including the `rootfs.ext4` block node, with it) and removes the API socket.
4. `lvremove atlas-vm-<uuid>` — the VM's disk LV. Guarded: the helper refuses
   to remove the thin pool or any `atlas-image-*` base LV, so a teardown bug
   can never destroy shared state. The VM's snapshot LVs are **not** removed
   here (their names aren't derivable from the VM UUID) — they go via the
   per-snapshot delete path below.

Then Python sets `status = Terminated` and deletes the VM's
`Virtual Machine Snapshot` rows; each row's `on_trash` `lvremove`s its snapshot
LV (those live in the pool, outside the VM directory, so step 3's `rm -rf` did
not touch them). **The UUID does not change.** The Task row that did the
terminate remains attached to the terminated VM.

If the Terminate Task fails (SSH dropped, script error, etc.), the row stays
in its prior status. The operator clicks Terminate again — the script is
idempotent (each step is a no-op if its target is already gone), so a
second invocation is the correct retry.

## The systemd unit

[`scripts/systemd/firecracker-vm@.service`](../scripts/systemd/firecracker-vm@.service) is the
canonical artifact. Highlights:

- `Restart=always` with `RestartSec=5s` — if Firecracker dies, systemd
  brings it back. "Keep them running."
- **`ExecStart` runs a per-VM launcher that execs the `jailer`, not
  `firecracker` directly.** The launcher (`…/%i/jailer-launch.sh`, generated by
  `provision-vm.py`) builds the jailer command line and `exec`s it; the jailer
  drops the Firecracker process to the VM's per-VM uid/gid, chroots it into
  `…/<uuid>/jail/firecracker/<uuid>/root`, applies cgroup-v2 memory/CPU caps
  and fd/file rlimits, and joins the VM's network namespace (`--netns`).
  Everything after `--` is forwarded to Firecracker, with paths relative to the
  jail root (`--config-file firecracker.json`, `--api-sock run/firecracker.socket`).
  The launcher exists — rather than putting the jailer line straight in
  `ExecStart` — because `--cgroup cpu.max=<quota> <period>` carries a value with
  an internal space, and systemd word-splits an unquoted `$VAR` in `ExecStart`
  on *every* space, which would shatter that value into a stray positional the
  jailer rejects. The per-VM uid, netns name and cgroup/rlimit flags are baked
  into the launcher at provision time: `provision-vm.py` receives the cgroup and
  resource limits as repeatable `--cgroup-arg` / `--resource-arg` flags (one argv
  token per value, `shlex.quote`'d, so `cpu.max`'s internal space survives) and
  writes each as its own continued line in the launcher's `exec`. The real argv
  vector means the shell's `mapfile` dance is gone entirely. The unit template
  stays static and the launcher is regenerated on every (re)provision.
- `ExecStartPre=/usr/bin/python3 /var/lib/atlas/bin/vm-network-up.py %i`
  (creates the netns + veth + in-namespace tap, so they exist when the jailer
  joins the namespace) and the matching `ExecStopPost` for `vm-network-down.py`.
  A third `ExecStartPre` runs `vm-disk-up.py %i` to re-activate the VM's disk LV
  and refresh its in-jail block node (needed after a host reboot, when
  activation-skip snapshots don't auto-activate). `ExecStartPre` runs
  to completion before `ExecStart`, so the namespace is ready at jailer exec.
  Networking is part of the unit's lifecycle, so a host reboot brings VMs back
  with networking intact.
- Two earlier `ExecStartPre` lines clean the jail for a fresh launch: the jailer
  `mknod()`s its device nodes (`/dev/net/tun`, `/dev/kvm`, …) inside the jail on
  *every* start and aborts with `EEXIST` if they already exist, but the jail root
  persists on disk across stop/start — so we `rm -rf` the jailer-owned `dev/`
  (and the stale API socket) first. Without this, the first Stop→Start cycle
  fails ("Failed to create /dev/net/tun via mknod: File exists"). The rootfs,
  kernel and config alongside `dev/` are left untouched.
- `KillMode=mixed` — the jailer is the unit's main process and Firecracker is
  its child; mixed sends SIGTERM to the jailer and SIGKILL to the whole cgroup,
  so the jailed Firecracker dies with the unit rather than being orphaned.
- `--config-file` is used, not the API socket, during boot. Fewer moving
  parts. The API socket is still created (`--api-sock`) inside the jail and used
  after boot by `pause-vm.py` / `resume-vm.py`. Snapshot/restore/rebuild/resize
  do **not** touch the socket — they are disk and config operations on a
  Stopped VM.

## Host reboot recovery

Because every `firecracker-vm@<uuid>.service` is `WantedBy=multi-user.target`,
a host reboot brings them all back. `vm-network-up.py` re-creates the network
namespace, veth pair, in-namespace tap and nft rules from
`/var/lib/atlas/virtual-machines/<uuid>/network.env`; the unit re-execs the
per-VM `jailer-launch.sh`, which has the per-VM uid/caps/netns baked in. Both
artifacts were written at provision time and survive the reboot on disk. No
Atlas-side intervention needed; the Frappe DB does not have to be consulted on
host reboot.

## Why resource fields are frozen outside resize

`server`, `image`, and `ssh_public_key` are immutable for the VM's lifetime —
they pin identity and what the rootfs was built against. To change them, the
operator terminates and provisions anew.

`vcpus`, `memory_megabytes`, and `disk_gigabytes` are *frozen on ordinary
saves* but mutable through `resize()` on a Stopped VM. The freeze is the
drift guard: the on-host VM must match the doc, so we never let an idle form
save silently desync the config from reality. `resize()` is the one path that
changes both together — it sets the new values **and** rewrites the on-host
config/disk in the same gesture, so they can't drift. The controller's
`validate()` enforces this: it adds the resource fields to the immutable set
unless `flags.resizing` is set (only `resize()` sets it). The framework
`set_only_once` flag was removed from these three fields so the controller is
the single gate.

This is the deliberate reversal of the original building-block stance ("change
CPU/RAM by terminating and reprovisioning"). Snapshots, restore/rebuild,
clone, resize and pause are now first-class — but each is constrained (disk
operations require Stopped, snapshots are disk-only, disk only grows) so the
on-host state stays derivable from the doc.
