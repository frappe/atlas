# Virtual machine lifecycle

The lifecycle is intentionally narrow: **provision, start, stop, delete**. No
resize, no migrate, no snapshot, no clone. Changing CPU/RAM means archiving
and provisioning a new VM. Each operation is exactly one Task.

## Identity

A `Virtual Machine.name` is a **UUID** assigned at insert. It never changes —
including on archive. This means:

- The on-host directory path
  (`/var/lib/atlas/virtual-machines/<uuid>/`) is stable forever.
- The systemd unit instance name (`firecracker-vm@<uuid>.service`) is stable.
- Tasks referencing the VM stay valid after archive.
- The operator does not have to invent a name; they use `description` for a
  human-readable label.

The MAC and TAP device are derived from the UUID so they are also stable.

## States

```
                  (insert via Create dialog)
                              |
                              v
                          Pending
                              |
                  (Provision button)
                              |
                              v
                       Provisioning
                              |
                  +-----------+-----------+
                  v                       v
              Running                 Failed
                  |                       |
       (Stop)     |                       | (Delete cleans up)
                  v                       v
              Stopped                 Archived
                  |
       (Start)    |
                  +---> Running
                          |
       (Delete from any non-Archived state)
                          v
                       Archived
```

`Archived` is terminal. The doc stays in the table forever for history.

## Provision

Trigger: operator fills the Create dialog (server, image, vCPUs, RAM, disk,
SSH key, description) and clicks `Provision`.

Steps in Python (one DocType method, `Virtual Machine.provision`):

1. **Allocate networking values** in the Frappe DB:
   - `ipv6_address`: next free address in `Server.ipv6_virtual_machine_range`.
     The allocator selects `Server` for update, scans existing
     `Virtual Machine.ipv6_address` for that server, picks the next, commits.
   - `mac_address`: `06:00:` + first 4 bytes of the UUID, hex-formatted.
   - `tap_device`: `atlas-` + first 9 chars of the UUID with `-` removed.
     Linux `IFNAMSIZ` is 16 *bytes* including the null terminator, so the
     usable interface-name length is 15: `atlas-` (6) + 9 = 15 exactly.

2. **Verify the image is on the server**. If not, **fail fast** with a clear
   error that points the operator at the **Sync to Server** action on the
   Image record. Provision does not auto-sync — image sync is a
   multi-minute operation and we want it deliberate, predictable, and
   visible as its own Task. This is also why `provision-vm.sh` is fast and
   synchronous.

3. **Run the provisioning task**:
   `run_task(server, "provision-vm.sh", variables, virtual_machine=name)`.
   The script does everything else inside one SSH session (rootfs copy,
   resize, SSH key injection, config write, systemd enable+start). See
   [`atlas/scripts/provision-vm.sh`](../scripts/provision-vm.sh).

4. **Update status**: on Task success, `status = Running`,
   `last_started = now()`.

This is one Task per VM creation. (The image sync, if needed, is a
separate Task triggered explicitly by the operator before provisioning.)

## Start / Stop / Restart

Each is a single Task running a one-line script:

- `start-vm.sh`: `systemctl start firecracker-vm@<name>.service`
- `stop-vm.sh`: `systemctl stop firecracker-vm@<name>.service`
- `delete-vm.sh`: see below

Restart is `stop-vm.sh` then `start-vm.sh`, but as the Python method's
choice — we do not add a `restart-vm.sh`, because the only thing `systemctl
restart` adds is one fewer network round-trip and we already paid for both.

Status updates happen after the Task succeeds. We do not poll the server
to verify; the source of truth is the Task. If the operator wants ground
truth, they click `Run Task` with `script=systemctl status ...`.

## Delete

Runs [`delete-vm.sh`](../scripts/delete-vm.sh), which:

1. `systemctl disable --now firecracker-vm@<uuid>.service` (no-op if already
   stopped).
2. Calls `vm-network-down.sh` defensively in case the unit's `ExecStopPost`
   didn't fire.
3. `rm -rf /var/lib/atlas/virtual-machines/<uuid>` and removes the API
   socket.

Then Python sets `status = Archived`. **The UUID does not change.** The Task
row that did the delete remains attached to the archived VM.

If the Delete Task fails (SSH dropped, script error, etc.), the row stays
in its prior status. The operator clicks Delete again — the script is
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
editable after first provision. To change them, the operator archives the VM
and provisions a new one. This keeps the on-host state derivable from the
doc — no migration logic, no resize commands, no out-of-sync moments. The
moment we let those fields change, we add code paths that have to handle
"the on-host VM was provisioned with the old values, now the doc says
something else". Not worth it for the building block.
