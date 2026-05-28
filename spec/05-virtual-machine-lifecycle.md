# Virtual machine lifecycle

The lifecycle is intentionally narrow: **provision, start, stop, terminate**.
No resize, no migrate, no snapshot, no clone. Changing CPU/RAM means
terminating and provisioning a new VM. Each operation is exactly one Task.

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
                          Pending
                              |
                  (after_insert → auto_provision worker)
                              |
                  +-----------+-----------+
                  v                       v
              Running                 Failed
                  |                       |
       (Stop)     |     (Provision retry) |
                  v                       v
              Stopped                 Running / Failed
                  |
       (Start)    |
                  +---> Running
                          |
       (Terminate from any non-Terminated state)
                          v
                       Terminated
```

There is no transient `Provisioning` status — the Task row is the "in-flight"
record; the VM row only moves to `Running` after a successful Provision Task,
and stays at `Pending` if it fails (re-clickable because the script is
idempotent).

`Terminated` is terminal. The doc stays in the table forever for history.

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
   `run_task(server=name, script="provision-vm.sh", variables=…,
   virtual_machine=name)`. The script's step 0 verifies the image is on the
   server; if not, it exits non-zero with a clear error pointing the operator
   at the **Sync to Server** action. Provision does not auto-sync — image
   sync is a multi-minute operation and we want it deliberate, predictable,
   and visible as its own Task. The remaining steps (rootfs copy, resize,
   SSH key injection, per-VM hostname `atlas-<first-8-of-uuid>` written to
   `/etc/hostname` and `/etc/hosts`, 512 MiB `/swapfile`, fresh per-VM
   `/etc/ssh/ssh_host_*` keypairs, per-VM `/etc/machine-id`, config
   write, systemd enable+start) happen inside the same SSH session.
   The per-VM identity writes share the rootfs mount with the SSH-key
   injection — no per-VM systemd unit needed. See
   [`atlas/scripts/provision-vm.sh`](../scripts/provision-vm.sh).

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
in. These are the contract `provision-vm.sh` writes and the e2e suite
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
- No global IPv4 on `eth0` — the `fcnet.service` that derived a phantom
  `91.83.x.x/30` from the MAC is removed at image-sync time.
- `/etc/hosts` has no Docker bridge leftover; just localhost, the
  per-VM 127.0.1.1 line, and the ip6-* aliases.
- Root password locked (`root:!:` in `/etc/shadow`). `sshd -T` reports
  `passwordauthentication no` — key-only by contract.
- `/swapfile` is active swap (512 MiB by default), referenced by the
  `/etc/fstab` installed at image-sync time.

This list is short for a reason: it is the operator-visible delta
between a Firecracker CI test artifact and a VM that looks like the
operator's own. When the upstream image changes, every bullet either
stays a no-op (good) or needs a new strip (a regression to fix in
`sync-image.sh`).

## Start / Stop / Restart

Each is a single Task running a one-line script:

- `start-vm.sh`: `systemctl start firecracker-vm@<name>.service`
- `stop-vm.sh`: `systemctl stop firecracker-vm@<name>.service`
- `terminate-vm.sh`: see below

Restart is `stop-vm.sh` then `start-vm.sh`, but as the Python method's
choice — we do not add a `restart-vm.sh`, because the only thing `systemctl
restart` adds is one fewer network round-trip and we already paid for both.

Status updates happen after the Task succeeds. We do not poll the server
to verify; the source of truth is the Task. If the operator wants ground
truth, they click `Run Task` with `script=systemctl status ...`.

## Terminate

Runs [`terminate-vm.sh`](../scripts/terminate-vm.sh), which:

1. `systemctl disable --now firecracker-vm@<uuid>.service` (no-op if already
   stopped).
2. Calls `vm-network-down.sh` defensively in case the unit's `ExecStopPost`
   didn't fire.
3. `rm -rf /var/lib/atlas/virtual-machines/<uuid>` and removes the API
   socket.

Then Python sets `status = Terminated`. **The UUID does not change.** The
Task row that did the terminate remains attached to the terminated VM.

If the Terminate Task fails (SSH dropped, script error, etc.), the row stays
in its prior status. The operator clicks Terminate again — the script is
idempotent (each step is a no-op if its target is already gone), so a
second invocation is the correct retry.

## The systemd unit

[`scripts/systemd/firecracker-vm@.service`](../scripts/systemd/firecracker-vm@.service) is the
canonical artifact. Highlights:

- `Restart=always` with `RestartSec=5s` — if Firecracker dies, systemd
  brings it back. "Keep them running."
- `ExecStartPost=/var/lib/atlas/bin/vm-network-up.sh %i` and the matching
  `ExecStopPost` for `vm-network-down.sh`. Networking is part of the unit's
  lifecycle, so a host reboot brings VMs back with networking intact.
- `--config-file` is used, not the API socket, during boot. Fewer moving
  parts. The API socket is still created for future post-boot operations.

## Host reboot recovery

Because every `firecracker-vm@<uuid>.service` is `WantedBy=multi-user.target`,
a host reboot brings them all back. `vm-network-up.sh` re-creates the tap
and nft rules from `/var/lib/atlas/virtual-machines/<uuid>/network.env`,
which was written at provision time. No Atlas-side intervention needed; the
Frappe DB does not have to be consulted on host reboot.

## Why immutable resource fields

`server`, `image`, `vcpus`, `memory_megabytes`, `disk_gigabytes` are not
editable after first provision. To change them, the operator terminates the
VM and provisions a new one. This keeps the on-host state derivable from the
doc — no migration logic, no resize commands, no out-of-sync moments. The
moment we let those fields change, we add code paths that have to handle
"the on-host VM was provisioned with the old values, now the doc says
something else". Not worth it for the building block.
