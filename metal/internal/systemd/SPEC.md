# systemd: unit control over D-Bus

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `systemd` is metald's thin client over systemd through D-Bus. Each virtual
machine runs as the template instance `metal-vm@<id>.service`. metald talks to the
system systemd, not the `systemctl` binary.

## Types

| Type | Role |
|---|---|
| `Manager` | Interface: `Start`, `Stop`, `Kill`, `ResetFailed`, `Status`, `Wait`, `List`, `SetLimits`. |
| `DBus` | The implementation. Owns one system-bus connection (`conn`). |
| `Status` | `PID`, `ActiveState`, `SubState`. |
| `Result` | `Code` and `Signal` from `Wait`. |
| `Limits` | `MemoryMaxBytes`, `CPUQuotaPct`. |

## Unit model

One template unit backs every VM. `Connect` opens the system bus. `Close` shuts it.

```text
template:   metal-vm@.service
per VM:     metal-vm@<id>.service        ID comes from the controller

metald --D-Bus--> system systemd --> jailer --> firecracker
```

## Operations

`Start` and `Stop` submit a job with mode `replace`, then wait for the job result
on a channel. A result other than `done` is an error. The context cancels the wait.
Mode `replace` cancels any conflicting job already queued for the unit, so a
control command always wins.

```text
Start        StartUnit(replace) -> job "done"    => activating -> active
Stop         StopUnit(replace)  -> job "done"    => deactivating -> inactive
Kill(sig)    KillUnit(All, sig)                  signal to the unit's processes
ResetFailed  clears failed                       failed -> inactive
Status       -> {PID = MainPID, ActiveState, SubState}
List         ListUnitsByPatterns("metal-vm@*.service") -> ids
SetLimits    MemoryMax (bytes) and CPUQuotaPerSecUSec (pct x 10000 us), runtime

Wait: poll ActiveState every 500 ms
   active, activating -> keep waiting
   inactive, failed   -> result:
        ExecMainCode == 1 (CLD_EXITED) -> Result{Code = ExecMainStatus}
        else                           -> Result{Signal = <signal name>}
```

## State mapping

The driver derives the VM state from `ActiveState`. `failed` maps to VM `failed`,
`inactive` and `deactivating` map to VM `stopped`, and an active unit is queried
over the firecracker API for `created`, `paused`, or `running`. Full mapping:
[internal/firecracker/SPEC.md](../firecracker/SPEC.md).

## Related

- [docs/vm.md](../../docs/vm.md) the VM state machine that `ActiveState` feeds.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) owns the unit's `ExecStart` and maps state.
- [docs/host-layout.md](../../docs/host-layout.md) where the unit files live on disk.
