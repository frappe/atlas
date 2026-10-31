# console: VM serial consoles

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · overview: [Metal daemon and API](../../../docs/region/metald.md)

## Purpose

The `console` package keeps recent VM serial output and replays it to new viewers. It also handles SSH session PTYs, whose IDs start with `ssh-`. `firecracker` owns their lifecycle.

## Types

| Type | Responsibility |
|---|---|
| `SerialBroker` | Maps VM and SSH session IDs to consoles. |
| `DescriptorStore` | Keeps PTY masters open across a metald restart. |
| `console` | Owns one PTY master, its scrollback, and its viewers. |
| `ringBuffer` | Holds recent output for replay. |
| `Winsize` | Carries a viewer terminal size. |

## PTY model

```mermaid
flowchart TB
    Firecracker -->|writes| Slave[PTY slave]
    Link[Console symlink] --> Slave
    Slave --> Master[PTY master]
    Master --> Drain[Drain goroutine]
    Master --> Store[systemd descriptor store]
    Drain --> Buffer[Ring buffer]
    Drain --> Viewers[Attached viewers]
```

| Method | Action |
|---|---|
| `Open` | Allocates the master and starts the drain. Keeps it out of the store. |
| `Persist` | Adds the master to the store after the unit holds the slave. |
| `KeepCarriageReturn` | Stops a VM console PTY from turning Enter into a newline. Do not use it for an SSH session, because `ssh` copies the setting to the guest terminal. |
| `Attach` | Adds a viewer. The viewer follows the VM to its next console after a restart. |
| `Close` | Releases the master. |
| `Shutdown` | Releases local masters. |
| `Adopt` | Restores stored masters, removes stale masters and links. |

Do not close a master while its VM runs. Linux then sends SIGHUP to Firecracker and stops the VM.

- The unit needs `NotifyAccess`, `FileDescriptorStoreMax`, and `FileDescriptorStorePreserve=yes`.
- systemd closes a stored master that reports `EPOLLHUP`, which happens while no slave is open.
- A master is non-blocking. One drain at a time reads a PTY.
- The drain treats EIO as idle until the console is closed.

## Backpressure

- Output must never block the guest.
- A viewer that would block is dropped and disconnected.
- A new viewer gets the bounded scrollback first, then live output.

## Lifecycle

A VM console lives from VM launch to VM stop. An SSH session console lives from session start to session end. The runtime calls `Open`, `Persist`, and `Close`, and `KeepCarriageReturn` for a VM console. metald calls `Adopt` and `Shutdown`.

A viewer lives until it disconnects or is dropped. `Attach` owns its input goroutine, so input goes to the current console and a restart does not leave a second reader on the viewer.

## Related

- [Metal daemon and API](../../../docs/region/metald.md) documents the console WebSocket endpoint.
- [Firecracker VM runtime](../../../docs/compute/runtime.md#limits-and-recovery) describes console survival across restarts.
- [internal/api/SPEC.md](../api/SPEC.md) owns the WebSocket framing and resize messages.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) opens and closes a console with the VM.
