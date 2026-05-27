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

The public SSH surface lives in [`atlas/atlas/ssh.py`](../atlas/atlas/ssh.py)
(a re-export shim over `atlas/atlas/_ssh/{runner,transport}.py`). Five
symbols, used by every controller and test:

```python
def run_task(*, script, variables, server=None, connection=None,
             virtual_machine=None, timeout_seconds=1800) -> Task:
    """Insert a Task row, run the script over SSH, update the row.

    Exactly one of `server` or `connection` is required:
      - server=<name>  — production path. Loads the Server doc and builds
                         the Connection from it. Every DocType button calls
                         this form.
      - connection=<Connection> — bootstrap path. Used before the Server
                         row has a usable provider linkage (`finish_provisioning`
                         uses it indirectly through `wait_for_ssh`).
    """

def execute_task(task_name: str) -> None:
    """Background-job entrypoint. Reads an already-inserted Pending Task,
    runs it via the same code path, updates the row. Called via
    `frappe.enqueue` for long Tasks (image sync)."""

def connection_for_server(server) -> Connection:
    """Build the SSH Connection from a Server doc (reads the provider's
    private key via `get_secret`)."""

def upload_files(connection, files: list[tuple[str, str]]) -> None:
    """scp a list of (local, remote) pairs. Not a Task. Used by
    `Server.bootstrap()` to lay down helpers + the systemd unit before
    the bootstrap script runs."""

def wait_for_ssh(connection, timeout_seconds: int = 300) -> None:
    """Poll the host until `ssh ... true` returns 0, or raise. Used after
    droplet create, before bootstrap."""
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

### Sync vs queued, by script

| Script                | Path                     | Why                                                                 |
| --------------------- | ------------------------ | ------------------------------------------------------------------- |
| `bootstrap-server.sh` | Queued (`finish_provisioning`) | 30–60s; chained after `wait_for_active` + `wait_for_ssh`. |
| `sync-image.sh`       | Queued (`execute_task`)  | Minutes; downloads ~600MB.                                          |
| `provision-vm.sh`     | Sync                     | ~3s; operator waits.                                                |
| `start-vm.sh` / `stop-vm.sh` / `terminate-vm.sh` | Sync | <1s.                                                  |
| `reboot-server.sh`    | Sync (via `run_task_dialog`) | The SSH drops mid-Task; the operator confirms by reconnecting. |
| Ad-hoc via Run Task   | Sync                     | The dialog is the operator's "I want to see this finish" path.      |

The "queue or not" decision lives in the calling DocType method, not in
`run_task`. Both paths funnel through the same `_execute_into` core.

### Queued-task ownership

For queued Tasks, the button handler runs in the request and the script
runs in the worker. The two-step pattern is:

1. **In the request**: the handler inserts a Task row with
   `status = "Pending"` and the full variables block, commits, then calls
   `frappe.enqueue("atlas.atlas.ssh.execute_task", task_name=task.name,
   queue="long", timeout=...)`. Returns the task name.
2. **In the worker**: `execute_task(task_name)` loads the row, builds the
   Connection from `task.server`, runs the script, and updates the row.

The Pending row is the operator's receipt: it shows up in the Task list
immediately, even before the worker has picked it up. If the worker never
runs (queue down), the row sits in `Pending` forever — visible enough that
the operator notices.

For sync Tasks (Provision/Start/Stop/Terminate, Run Task dialog) the
button handler calls `run_task` directly; row insert and run happen back
to back in one process.

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

## Sidecar uploads (`SCRIPT_UPLOADS`)

Some scripts need a supporting file on the server before they run. The
canonical example is `sync-image.sh`, which needs the guest
`atlas-network.service` unit file staged so it can be embedded into the
ext4 it builds.

Rather than grow a Python "do this first" hook per Task type, we keep a
single map in
[`atlas/atlas/script_uploads.py`](../atlas/atlas/script_uploads.py):

```python
SCRIPT_UPLOADS: dict[str, list[tuple[str, str]]] = {
    "sync-image.sh": [
        ("scripts/guest/atlas-network.service",
         "/tmp/atlas/atlas-network.service"),
    ],
}
```

`run_task` consults this map before each invocation and `scp`s the listed
files alongside the script itself. The script reads them via env vars
(e.g. `GUEST_NETWORK_UNIT=/tmp/atlas/atlas-network.service`).

Bootstrap's helper scripts (`vm-network-up.sh`, `vm-network-down.sh`,
`firecracker-vm@.service`) are **not** in this map — they're durable
state, placed by `Server.bootstrap()` calling `upload_files` directly, not
re-uploaded on every Task. See [03-bootstrapping.md](./03-bootstrapping.md).

## Scripts catalog

The list of scripts an operator can run lives in
[`atlas/atlas/scripts_catalog.py`](../atlas/atlas/scripts_catalog.py):

- `allowed_scripts()` returns the sorted `.sh` filenames directly under
  [`scripts/`](../scripts/). This is the whitelist used by the SSH
  runner and the `Server.run_task_dialog` controller method.
  `scripts/guest/` and `scripts/systemd/` are excluded — they aren't
  host-runnable shell scripts.
- `operator_visible_scripts()` is the strict subset the desk's `Run Task`
  picker is allowed to expose: `bootstrap-server.sh`,
  `reboot-server.sh`, `sync-image.sh`. Everything else
  (`provision-vm.sh`, `start-vm.sh`, `terminate-vm.sh`, …) is a
  state-machine move that must originate from a VM or Image controller
  method — the operator drives it via the VM form's lifecycle buttons,
  not by hand-firing the script with empty variables.
- `resolve(script)` locates a script file in either `scripts/` or the
  e2e-only `atlas/tests/e2e/scripts/` directory (used by tests).

The split is enforced at the boundary, not deep in: `Server.get_scripts()`
returns `operator_visible_scripts()` for the desk picker, while
`Server.run_task_dialog` continues to validate against
`allowed_scripts()`. Internal callers (`Server.bootstrap`, `Server.reboot`,
VM lifecycle methods) keep working unchanged.

## "Run Task" — the escape hatch

On `Server` there is a `Run Task` button. It opens a dialog with:

- A Select populated from `Server.get_scripts()` (the operator-visible
  three).
- A per-script form: bootstrap-server.sh asks for
  `FIRECRACKER_VERSION` and `ARCHITECTURE`; sync-image.sh asks for
  `IMAGE_NAME` (Link → Virtual Machine Image, filtered to
  `is_active = 1`); reboot-server.sh asks for nothing. Per-script
  fields are gated by `depends_on: doc.script === ...` and toggled on
  change. The raw `Variables (JSON)` Code field is no longer the
  default surface.
- A `Show advanced (System Manager)` toggle that brings the raw-JSON
  field back. This preserves the "hand-fire a script with custom
  vars" capability we use for debugging while keeping the default
  safe for everyone else.

The whitelisted method `Server.run_task_dialog(script, variables)` rejects
any script not in `allowed_scripts()`, parses `variables` (string or dict),
and runs the same `run_task` code path. Reboot is implemented as
`run_task_dialog(script="reboot-server.sh", variables={})` — uniform path,
recorded as a Task, same audit story.

This is the same code path Atlas itself uses, including being recorded in
the Task table. It's how we debug, and how we run one-off operations without
adding a new DocType method.

## Retrying a failed Task

Failed Tasks expose a **Retry** button on the form. `Task.retry()` is a
whitelisted method that:

- For VM lifecycle scripts (`provision-vm.sh`, `start-vm.sh`,
  `stop-vm.sh`, `restart-vm.sh`, `terminate-vm.sh`): loads the linked
  Virtual Machine and calls the matching controller method
  (`vm.provision()`, `vm.start()`, …). The state-machine guards on the
  VM live there; Retry does not duplicate them. If the VM is in a
  state that disallows the action, the controller's existing
  `frappe.throw` surfaces to the operator.
- For operator-visible server scripts (`bootstrap-server.sh`,
  `reboot-server.sh`, `sync-image.sh`): re-invokes
  `Server.run_task_dialog(self.script, self.variables_dict)` so the
  retry is recorded as a fresh Task row with the original variables.
- For anything else (e.g. an ad-hoc `noop.sh`): throws "not retriable
  from the Task form."

A retry is a new Task row, not a mutation of the failed one. The audit
trail keeps both.
