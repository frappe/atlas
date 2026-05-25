# Tasks: the SSH execution model

A Task is one shell script invocation against one server, persisted as a row
in the database. The Task is the unit of audit, the unit of replay, and the
unit of failure.

## What a Task is

```
Task = (server, script, variables) executed over SSH, with captured output
```

Concretely, a Task is a row in `Task` with:

- `server`, `virtual_machine` (optional)
- `script`: the file name under `atlas/scripts/`, e.g. `provision-vm.sh`
- `variables`: a JSON object of env-var-to-value passed to the script
- `started`, `ended`, `duration_milliseconds`
- `exit_code`, `stdout`, `stderr`
- `status`: one of `Pending`, `Running`, `Success`, `Failure`
- `triggered_by`: the user

## How it runs

```
atlas/atlas/atlas/ssh.py:

    def run_task(connection, script, variables, ...) -> Task:
        # Low-level primitive: takes an already-built connection dict and
        # runs one script. Used directly during bootstrap (before a Server
        # row exists).
        ...

    def run_task_on_server(server, script, variables, ...) -> Task:
        # Convenience wrapper: builds the connection dict from a Server doc
        # and delegates to run_task. This is what every DocType button calls.
        ...
```

`scp` and `ssh` inside `run_task` are the system commands, invoked via
`subprocess.run()`. Not paramiko. Not fabric. Not anything else.

### Why the system `ssh`

- It is everywhere. Frappe servers already have it.
- `~/.ssh/config`, `known_hosts`, agent forwarding, `ControlMaster` — all
  just work.
- We avoid pinning a Python library to a Python version. SSH is stable.
- Debugging: an operator can copy-paste the same `ssh` invocation from a Task
  row and run it by hand.

### Connection details

- User: `root`.
- Auth: SSH private key from `Server Provider.ssh_private_key`, written to a
  short-lived tempfile (`mode 0600`) when the SSH command runs.
- Options we always pass:
  - `-o StrictHostKeyChecking=accept-new` — accept on first contact, fail on
    later changes. (Host-key pinning is on the [roadmap](./09-roadmap.md).)
  - `-o UserKnownHostsFile=~/.atlas/known_hosts` — keep host keys out of the
    user's normal `known_hosts`.
  - `-o BatchMode=yes` — never prompt.
  - `-o ConnectTimeout=30`.
- Variables: passed via `ssh ... env VAR=val VAR2=val2 bash -x /tmp/atlas/script.sh`.
  Quoted with `shlex.quote()`.

### Timeouts

- Connect: 30 seconds.
- Script execution: 30 minutes default, overridable per call. Most scripts
  finish in seconds; image syncs are the long pole.

## One Task = one script. Not one Task = one command.

The old design had one row per shell command. That was clean but it forced
network round-trips between every `mkdir` and `cp`, which made VM
provisioning take seconds longer than it had to and produced 8 rows per
provision.

The new design: a Task is whatever the script does. `provision-vm.sh` does
five things in one process. If step 3 fails, the script exits non-zero, the
Task is `Failure`, and the operator reads the Task to see which step.

The rule:

> A Task is one shell script. Compose at the script level, not at the SSH
> level. If you find yourself running two scripts back-to-back from Python,
> ask whether they should be one script.

### Trade-off

We lose fine-grained "which sub-step failed" visibility — the Task only knows
the script exited with code N. We gain:

- Provisioning is fast (one SSH connect, no per-step latency).
- The whole thing runs in one bash process so `set -e` propagates correctly.
- The script is the spec for what gets done; it has no Python coupling.
- The Task is replayable: same script, same variables → same result (modulo
  external state).

### Why not zx?

[zx](https://github.com/google/zx) is "write shell in JavaScript". The good
idea is *structured outputs and ergonomic shell composition*. Our equivalent
is *one self-contained shell script that takes env-var inputs and exits
non-zero on failure*. We get the ergonomics from Bash itself (`set -euo
pipefail`, heredocs, traps). When we eventually need typed orchestration —
fanout across servers, conditional branches, retries — we will reimplement
the small slice we need in Python, not adopt zx. See the
[roadmap](./09-roadmap.md).

## How Python triggers a Task

From any DocType method:

```python
from atlas.atlas.ssh import run_task

def provision(self):
    variables = {
        "VIRTUAL_MACHINE_NAME": self.name,
        "IMAGE_NAME": self.image,
        ...
    }
    run_task(
        server=self.server,
        script="provision-vm.sh",
        variables=variables,
        virtual_machine=self.name,
    )
```

The method is sync from the caller's perspective. For long tasks, callers
wrap it in `frappe.enqueue` (Frappe's background job queue) so the operator
isn't blocked in Desk.

## Idempotency

Every script in `atlas/scripts/` is idempotent. Re-running a script with the
same inputs is safe. We do not have automatic retry — the operator retries
by clicking the button again, which creates a new Task.

## Failure handling

If a script exits non-zero:

1. The Task row is marked `Failure` with the exit code and full stdout/stderr.
2. The Python caller's `run_task` raises `frappe.ValidationError`.
3. The calling DocType method catches it, sets its own `status` field
   appropriately (e.g. `Virtual Machine.status = Failed`), and re-raises so
   Desk shows the error.

The Task row is the authoritative record; the doc's status is a denormalized
view of the latest task.

## "Run Task" — the escape hatch

On `Server` there is a `Run Task` button. It opens a dialog with:

- A picker over the scripts directory (so an operator can run any known
  script ad-hoc).
- A JSON text field for variables.

This is the same code path Atlas itself uses, including being recorded in
the Task table. It's how we debug, and how we run one-off operations without
adding a new DocType method.
