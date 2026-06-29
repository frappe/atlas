# Tasks: the SSH execution model

A Task is one script invocation against one server, persisted as a row in the
database. The Task is the unit of audit, the unit of replay, and the unit of
failure. The script is a typed Python program (see
[§ Tasks are Python](#tasks-are-python-the-zx-slice-we-built)); a couple of
trivial shell scripts remain.

## What a Task is

```
Task = (server, script, variables) executed over SSH, with captured output
```

Concretely, a Task is a row in `Task` with:

- `server`, `virtual_machine` (optional)
- `script`: the **verb** — the name of a script under `scripts/` with its
  extension dropped, e.g. `provision-vm` (the file on disk is `provision-vm.py`).
  Executed on the host as `atlas provision-vm …`. The on-disk file keeps its
  `.py`/`.sh` suffix; only the Task identifier drops it. `scripts_catalog` is the
  authority that maps a verb to its file (`file_for`) and its nature (`kind`).
- `variables`: a JSON object passed to the script — as `--kebab-case` CLI
  flags for a Python verb, or env vars for a shell verb
- `started`, `ended`, `duration_milliseconds`
- `exit_code`, `stdout`, `stderr`
- `status`: one of `Pending`, `Running`, `Success`, `Failure`
- `triggered_by`: the user

## How it runs

The public SSH surface lives in [`atlas/atlas/ssh.py`](../atlas/atlas/ssh.py)
(a re-export shim over `atlas/atlas/_ssh/{runner,transport}.py`). The symbols
used by every controller and test:

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
    """Build the SSH Connection from a Server doc. Reads the private-key
    path off `Atlas Settings.ssh_private_key_path` via
    `atlas.get_ssh_private_key_path()` and loads the PEM at SSH-connect
    time. Only guard is `Server.ipv4_address` — `Server.provider_type` is not
    read by this function (the SSH key is vendor-agnostic)."""

def connection_for_guest(virtual_machine) -> Connection:
    """The SECOND SSH target type: a guest, not a host. Builds a Connection
    to a VM's public IPv6 `/128`, as `root`, with the SAME Atlas key — its
    public half is already in the guest's `root/.ssh/authorized_keys`
    (injected by the rootfs at provision), so no new image plumbing. Used by
    the proxy control plane (`atlas/atlas/proxy.py`) to reach a proxy guest's
    unix-socket admin API over SSH (map sync, cert push). The admin socket's
    file permissions remain the gate inside the guest. See [12-proxy.md]."""

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

- User: `root` — for both target types. The host path SSHes a Server by its
  public v4; the guest path (`connection_for_guest`) SSHes a VM by its public
  v6 `/128`, also as `root`, with the same key (the guest's
  `root/.ssh/authorized_keys` already carries it). `Connection.user` is the
  field that carries this, defaulting to `root`.
- Auth: SSH private key read from the path on
  `Atlas Settings.ssh_private_key_path` (a `0600` PEM on the Atlas host,
  see [07-filesystem-layout.md § SSH keys](./07-filesystem-layout.md)).
  The key is loaded via `secrets.get_ssh_key_from_disk(path)` at
  SSH-connect time and written to a short-lived tempfile (`mode 0600`)
  for the `ssh`/`scp` invocation.
- Options we always pass:
  - `-o StrictHostKeyChecking=accept-new` — accept on first contact, fail on
    later changes. (Host-key pinning is on the [roadmap](./09-roadmap.md).)
  - `-o UserKnownHostsFile=~/.atlas/known_hosts` — keep host keys out of the
    user's normal `known_hosts`.
  - `-o BatchMode=yes` — never prompt.
  - `-o ConnectTimeout=30`.
  - `-o ControlMaster=auto -o ControlPath=~/.atlas/cm/%C -o ControlPersist=60s`
    — connection multiplexing. A Task that stages a sidecar opens 2+ connections
    to the same host back-to-back (stage, then run the script); the first does
    the TCP+SSH handshake and the rest ride the shared master socket. A durable,
    sidecar-free Task is a single run, and back-to-back Tasks reuse the master
    too (`ControlPersist`). `%C` (a hash of user/host/port) keeps concurrent
    Tasks to different servers on distinct sockets. This is a dominant latency
    win for a remote provision — each avoided handshake is ~1.5s+ over a real
    droplet.
- Variables: how they reach the script depends on the verb's nature
  (`scripts_catalog.kind(verb)` — never a suffix-sniff; see
  [§ Tasks are Python](#tasks-are-python-the-zx-slice-we-built)):
  - **Python verb** — `ssh ... atlas <verb> --kebab-flag val …`. The
    pip-installed `atlas` console script on `PATH` (`/usr/local/bin/atlas`)
    dispatches to the typed entry point; no interpreter path, no `PYTHONPATH` —
    the durable `uv pip install` put both the package and the console entry into
    the Atlas venv at bootstrap (see
    [03-bootstrapping.md § The Atlas interpreter and CLI](./03-bootstrapping.md)).
    The `variables` dict keys (`UPPER_SNAKE`) become `--kebab-case` CLI flags; a
    list value becomes a repeated flag. Quoted with `shlex.quote()`. This is the
    bulk of Tasks and needs no per-Task scp (next section). A stale/legacy host
    with no `atlas` on `PATH` surfaces this as the Task's own
    `atlas: command not found` — the fail-fast moved from a per-Task `test -e`
    round trip to **once-at-bootstrap** (`Server.cli_ready`, set when the
    bootstrap sanity gate proved `atlas --help` dispatches).
    `bootstrap-server` is an ordinary Python verb here too: [`scripts/install.sh`](../scripts/install.sh)
    creates the venv + console script over SSH *before* the bootstrap Task (see
    [03-bootstrapping.md](./03-bootstrapping.md)), so by the time it runs the
    interpreter already exists — no carve-out, no stock-`python3` branch.
  - **Shell verb** — `ssh ... env VAR=val VAR2=val2 bash -x /var/lib/atlas/bin/reboot-server.sh`.
    The legacy form, kept for the few remaining shell verbs (`reboot-server`) and
    the e2e probes. Shell verbs run their file by path.
  Both shapes are built in `_ssh/runner.py::_remote_command()`, dispatched on
  `scripts_catalog.kind(verb)` — not a filename suffix.

### Timeouts

- Connect: 30 seconds.
- Script execution: 30 minutes default, overridable per call. Most scripts
  finish in seconds; image syncs are the long pole.

## One Task = one script. Not one Task = one command.

The old design had one row per shell command. That was clean but it forced
network round-trips between every `mkdir` and `cp`, which made VM
provisioning take seconds longer than it had to and produced 8 rows per
provision.

The new design: a Task is whatever the script does. `provision-vm.py` does
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
- The whole thing runs in one process, so a failure aborts the rest (`set -e`
  in a `.sh` task; `run()` raising in a `.py` task).
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

## Tasks are Python (the zx slice we built)

The features grew, the scripts grew complicated, and the verifiability the
"Why not zx?" section promised arrived: **a Task script is now a typed,
self-contained Python program**, not a shell script. The contract is
unchanged — a Task is still one script invocation over one SSH connection,
idempotent, exits non-zero on failure, replayable — but the implementation
language is Python 3 (already on every Ubuntu 24.04 host; no new dependency).

`reboot-server.sh` is the lone holdover (two lines); everything else under
[`scripts/`](../scripts/) is `<name>.py`. The runner runs Python and shell verbs
side by side — `scripts_catalog.kind(verb)` is the authority on which is which
(it reads the on-disk file's extension), so the catalog, not a `Task.script`
suffix-sniff, draws the boundary.

### Why Python, not "harden the shell"

The *variables* were never the problem — they are pure functions of the VM
UUID in [`networking.py`](../atlas/atlas/networking.py), unit-tested already.
What grew unmaintainable was the *execution logic* that crept into shell:
idempotency gates, error recovery (`lvremove` on a failed `dd`), string
surgery that exists only because bash is bad at it (the `lsblk` MAJ:MIN
whitespace strip, the `mapfile` dance to keep `cpu.max`'s internal space).
None of that could be unit-tested in bash without a real host — exactly the
class of defect that passes static + unit checks and fails on a droplet.

Porting that logic to Python makes it **unit-testable on the Atlas host with
no droplet**: the lib modules are stdlib-only (no Frappe, no site), so their
pure functions test in milliseconds (`python3 -m unittest atlas.test_lvm
atlas.test_host` from [`scripts/lib`](../scripts/lib)). This is the spec's
own "host facts vs. unit-covered logic" split, pushed down into the scripts.

### Shape of a Task script

Three pieces, one painfully simple `main()`:

```python
# scripts/snapshot-vm.py
@dataclass(frozen=True)
class SnapshotInputs(TaskInputs):
    command: typing.ClassVar[str] = "snapshot-vm"
    virtual_machine_name: str            # → --virtual-machine-name (required)
    snapshot_rootfs_path: str            # → --snapshot-rootfs-path

@dataclass(frozen=True)
class SnapshotResult(TaskResult):
    size_bytes: int

def main() -> None:
    inputs = SnapshotInputs.from_args()          # typed; argparse gives --help
    pool = ThinPool()
    disk = pool.vm_disk(inputs.virtual_machine_name)
    snapshot = pool.from_device(inputs.snapshot_rootfs_path)
    if not disk.exists: sys.exit("disk LV not found …")
    if pool.usage.too_full_to_snapshot: sys.exit("thin pool too full …")
    disk.snapshot_into(snapshot)
    SnapshotResult(size_bytes=snapshot.size_bytes).emit()
```

- **Typed input**, not env soup. Each `TaskInputs` field is a `--kebab-case`
  CLI flag ([`scripts/lib/atlas/_task.py`](../scripts/lib/atlas/_task.py)).
  `from_args()` parses argv once; argparse gives `--help` and exit-2 on a
  missing/!int flag for free — the CLI form of `${VAR:?required}`. A `list`
  field is a repeatable flag (`--cgroup-arg a --cgroup-arg b`), which is what
  kills the shell's `mapfile`/word-splitting workaround: a value with an
  internal space stays one argv token. **This is the shape the `atlas` host CLI
  mounts directly** — each task is already a subcommand
  ([`_cli.py`](../scripts/lib/atlas/_cli.py)). The CLI is no longer just a debug
  face: the runner *executes every Python verb through it* (`atlas stop-vm …`),
  installed at bootstrap via `uv pip install` (see
  [03-bootstrapping.md § The `atlas` host CLI](./03-bootstrapping.md)).
- **Typed output**, not stdout scraping. A task that returns data emits one
  `ATLAS_RESULT=<json>` line via `TaskResult.emit()`; the controller decodes
  it with `task_results.parse_result()`. This replaced the `SIZE_BYTES=` grep
  and the bootstrap-JSON tail-line read — and fixes their silent-on-truncation
  bug (`parse_result` raises if the marker is absent).
- **OO host actions.** `ThinPool` / `LogicalVolume`
  ([`lvm.py`](../scripts/lib/atlas/lvm.py)), `VirtualMachinePaths`
  ([`paths.py`](../scripts/lib/atlas/paths.py)), `prepare_lv` / `inject_identity`
  ([`rootfs.py`](../scripts/lib/atlas/rootfs.py)). The one place that touches
  the host is `run()` in [`_run.py`](../scripts/lib/atlas/_run.py) — it echoes
  each command (the `set -x` trace into the Task log) and raises on non-zero
  (the `set -e` abort). Everything else is pure functions over strings.

### The shared `atlas` package and how it is staged

The lib lives in [`scripts/lib/atlas/`](../scripts/lib/atlas) and is
**stdlib-only today** — which is why the logic tests with no host. (That is a
convenience, not a guarded invariant: the host installs the package into a venv,
so a real dependency is fine — uv resolves it at `uv pip install`.) A Task script
imports it from **one durable copy** on the host:

- **Durable placement** (`Server.bootstrap()`): the package is placed once at
  `/var/lib/atlas/bin/atlas/`, beside the three systemd-hook scripts, so they
  and `atlas-pool.service` can `import atlas` after a reboot. The file list is
  computed from disk (`test_*.py` skipped), so a new lib module ships with no
  map edit.
- **Tasks reach it as the pip-installed `atlas` console script**:
  [`scripts/install.sh`](../scripts/install.sh) — run over SSH by
  `Server.bootstrap()` right after the upload — runs `uv pip install
  /var/lib/atlas/bin` into the Atlas venv, registering the `atlas` entry point
  (and resolving the package's imports against that same durable tree). A Python
  verb then runs as `atlas <verb>` — no `PYTHONPATH`, no per-Task scp of the
  package. Only per-verb sidecars (sync-image's guest `atlas-network.service`, in
  `SCRIPT_SIDECARS`) are uploaded. This removes ~9 scp round-trips from every Task;
  combined with SSH multiplexing it takes a remote provision from ~20s+ toward a
  few seconds.

  **Staleness trade-off (deliberate):** because the package is shipped + installed
  only at bootstrap, a controller-side change to a lib module reaches a host only
  on the next `bootstrap` — bootstrap is the single refresh point. This is the
  same contract the systemd hooks already follow (they too run the durable copy).
  Re-run `bootstrap` (idempotent) after changing anything under
  `scripts/lib/atlas/`. With install.sh creating the venv before the bootstrap
  Task, there is no carve-out: `bootstrap-server` runs as `atlas bootstrap-server`
  on the venv python like every other verb.

- **The entry scripts are durable too.** Bootstrap / `sync_scripts` ship every
  host Task entry FILE (`scripts_catalog.host_task_scripts()` yields verbs;
  `file_for` maps each to its file — provision-vm.py, start/stop/snapshot-stop,
  …) to `/var/lib/atlas/bin/` beside the package, where `uv pip install` registers
  the console script and the runner reaches each as `atlas <verb>` **in place**:
  no per-Task `mkdir`+`scp`, just the one run. The scp was the dominant latency of
  an otherwise sub-second op — dropping it took a live stop from ~2.2s to ~0.6s,
  start ~2.8s→~1.1s, and provision ~5.7s→~3.0s on a real droplet. Same staleness
  contract as the package (re-bootstrap / `sync_scripts` to refresh). The staging
  path and its stale-lib purge survive only for **shell verbs not shipped durably**
  — the **e2e probes** (resolved from the test-only directory) — and any **sidecar
  files** (`sync-image`'s guest unit) staged before a python verb runs.

### Systemd hooks are Python too, but not Tasks

`vm-disk-up.py`, `vm-network-up.py`, `vm-network-down.py`, `vm-restore.py`
run from the VM unit's `ExecStartPre`/`ExecStartPost`/`ExecStopPost`, not over
SSH. They take a **positional uuid** (`%i`), not `--flags`, and import the
durable package. They are excluded from
`scripts_catalog.allowed_scripts()` (`SYSTEMD_HOOKS`) so the Task runner
never executes them. Like every Python Task they run under the **Atlas venv
python** — the units invoke `/var/lib/atlas/venv/bin/python <hook> %i`, and
`atlas-pool.service` runs the pool bring-up inline:
`/var/lib/atlas/venv/bin/python -c "… ThinPool().ensure()"` (see
[03-bootstrapping.md § The Atlas interpreter and CLI](./03-bootstrapping.md)).
There is no shell helper library (`lvm.sh`) anymore — the durable `atlas`
package replaced it.

A second, **controller-only** bucket (`CONTROLLER_ONLY`: `issue-cert.py`,
`tunnel-up/down.py`, `mgmt-firewall-*.py`) is also excluded from
`allowed_scripts()` — those run on the *controller* via the local runner, never
over SSH onto a host. The host `atlas` CLI ([§ Tasks are Python](#shape-of-a-task-script))
discovers its commands from the filesystem and **does not** currently filter this
bucket, so its command set is a superset of the host-SSH catalog. That is a
known, deferred gap (Phase 2): running one of these on a host mostly just fails
(they need controller-side context / deps), and the intended fix is to install
the same CLI on the controller so `atlas mgmt-firewall-apply …` runs where it
belongs — at which point the CLI's *available* command set is context-dependent
(host vs controller) rather than wrong. Until Phase 2, do not assume
`_cli` command set == `allowed_scripts()`.

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
        script="provision-vm.py",
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
| `bootstrap-server.py` | Queued (`finish_provisioning`) | 30–60s; chained after `wait_for_active` + `wait_for_ssh`. |
| `sync-image.py`       | Queued (`execute_task`)  | Minutes; downloads ~600MB.                                          |
| `provision-vm.py`     | Sync                     | ~3s; operator waits.                                                |
| `start-vm.py` / `stop-vm.py` / `terminate-vm.py` | Sync | <1s.                                                  |
| `snapshot-stop-vm.py`  | Sync                    | The opt-in memory-capturing stop; pause + RAM dump + stop. Seconds, bounded by RAM size / disk write speed. |
| `warm-snapshot-vm.py`  | Sync (Image Build job, or `Virtual Machine.capture_warm_snapshot()`) | The warm capture: pause + RAM dump + disk LVM snapshot to a durable directory + resume. Same bound as the fast stop; inside a bake the enclosing job runs on `queue="long"`, and the per-VM operator action runs it directly. |
| `promote-snapshot-image.py` | Sync (via `Virtual Machine Snapshot.promote_to_image()`) | Promote a cold snapshot LV into a same-server read-only `atlas-image-<name>` base image (local `dd`) + materialize its image dir. Seconds (a local dd of the disk), bounded by disk write speed. See [08-images.md](./08-images.md#two-origins-for-a-base-image-a-url-or-a-snapshot-promote). |
| `vm-reserved-ip.py`   | Sync (via `Reserved IP.attach()`/`detach()`) | <1s; applies/removes the inbound-v4 1:1-NAT live. |
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

For the long Tasks (image sync, the in-guest bench/proxy bakes), the Task row
is also the **live progress carrier**: its output is streamed onto the row while
the script runs, not written once on exit, and a `Running Operations` view
collects every in-flight Task fleet-wide. See
[22-observability.md](./22-observability.md).

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

## Sidecar uploads ([`script_uploads.py`](../atlas/atlas/script_uploads.py))

Before a Python Task runs, `run_task` stages only **per-script sidecars** —
extra files a specific task needs — beside the script. The shared `atlas`
package is **not** among them: it lives durably at `/var/lib/atlas/bin/atlas/`
(placed by `Server.bootstrap()`) and Tasks reach it via
`PYTHONPATH=/var/lib/atlas/bin` (see [§ the shared `atlas` package and how it is
staged](#the-shared-atlas-package-and-how-it-is-staged)). So a durable Python
Task with no sidecar uploads **zero** files and stages nothing — it runs the
durable script in place. A sidecar Task stages only its sidecar(s).

The canonical sidecar is `sync-image.py`, which needs the guest
`atlas-network.service` unit staged so it can be embedded into the ext4 it
builds. These live in a small map:

```python
SCRIPT_SIDECARS: dict[str, list[tuple[str, str]]] = {
    "sync-image.py": [
        ("scripts/guest/atlas-network.service",
         "/tmp/atlas/atlas-network.service"),
    ],
}
```

The script reads a sidecar by its staged path, passed as a CLI flag
(e.g. `--guest-network-unit /tmp/atlas/atlas-network.service`).

The systemd-hook scripts (`vm-network-up.py`, `vm-network-down.py`,
`vm-disk-up.py`), the unit files, and the durable `atlas` package are placed at
`/var/lib/atlas/bin/` (and `/var/lib/atlas/bin/atlas/`) by `Server.bootstrap()`
calling `upload_files` directly. See [03-bootstrapping.md](./03-bootstrapping.md).

## Scripts catalog

The list of scripts an operator can run lives in
[`atlas/atlas/scripts_catalog.py`](../atlas/atlas/scripts_catalog.py):

- `allowed_scripts()` returns the sorted `.py` and `.sh` filenames directly
  under [`scripts/`](../scripts/). This is the whitelist used by the SSH
  runner and the `Server.run_task_dialog` controller method.
  `scripts/guest/` and `scripts/systemd/` are excluded (not host-runnable),
  and so are the systemd-hook scripts (`SYSTEMD_HOOKS`: `vm-disk-up.py`,
  `vm-network-up.py`, `vm-network-down.py`, `vm-restore.py`) — they run from
  the VM unit with a positional uuid, not as Tasks.
- `operator_visible_scripts()` is the strict subset the desk's `Run Task`
  picker is allowed to expose: `bootstrap-server.py`,
  `reboot-server.sh`, `sync-image.py`. Everything else
  (`provision-vm.py`, `start-vm.py`, `terminate-vm.py`, …) is a
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

## Task subject

The `subject` field is the operator-facing label on every Task row.
The rule, encoded in `SCRIPT_LABELS` on `Task` and applied at
`before_insert`:

- **Verb only** when the script operates on the same object the Task
  is anchored to — `Reboot`, `Start`, `Stop`, `Restart`, `Terminate`.
  The Server / Virtual Machine column on the row carries the target
  identity; the subject doesn't need to repeat it.
- **Verb + Noun** when the script creates a new object — `Bootstrap
  Server` (creates host state from nothing), `Sync Image` (creates
  server-side state for an existing image), `Create Virtual Machine`
  (creates a new VM).

The legacy `<verb> · <target>` shape (e.g. `Provision VM · verify
vnet_hdr fix on bootstrap-server-…`) is gone — the target identity
lives in the row's columns and the form's link fields. Existing rows
were rewritten by
[`atlas/patches/v1_0/rebuild_task_subjects.py`](../atlas/patches/v1_0/rebuild_task_subjects.py).

The Task list-view's Status column renders a coloured pill driven by
the `states` JSON on the DocType (`Pending` yellow, `Running` blue,
`Success` green, `Failure` red); the legacy
`task_list.js::indicator` is gone for the status. The `subject`
formatter renders the bare subject — the earlier ` · <duration>`
suffix was dropped (duration is one column away on the form).

## Per-script dialogs — no catch-all Run Task

There is no generic **Run Task** dialog on the Server form. Each
operator-visible script gets its own first-class Actions item with a
typed dialog:

- **Sync Image** — a one-field dialog (Link → Virtual Machine Image,
  `only_select: 1`, `is_active = 1` filter). Calls
  `Server.sync_image(image)`, which delegates to `Virtual Machine
  Image.sync_to_server(self.name)`.
- **Re-bootstrap** — a `confirm_destructive` dialog (type the server
  title). Calls `Server.bootstrap()`.
- **Reboot** — a `confirm_destructive` dialog (type the server title).
  Calls `Server.reboot()`.

The whitelisted `Server.run_task_dialog(script, variables)` method
survives for two non-operator callers: `Task.retry()` (so a failed
Task can be re-fired against any whitelisted script) and the
`desk_buttons` e2e suite. It is no longer surfaced as a button on the
form.

The `operator_visible_scripts()` subset stays narrow — currently just
`bootstrap-server.py`, `reboot-server.sh`, `sync-image.py`. Lifecycle
scripts (`provision-vm.py`, `start-vm.py`, `terminate-vm.py`, …) are
state-machine moves that originate from the VM controller's lifecycle
buttons, never from a Server-form picker.

## Retrying a failed Task

Failed Tasks expose a **Retry** button on the form. `Task.retry()` is a
whitelisted method that:

- For VM lifecycle scripts (`provision-vm.py`, `start-vm.py`,
  `stop-vm.py`, `snapshot-stop-vm.py`, `restart-vm.py`,
  `terminate-vm.py`): loads the linked
  Virtual Machine and calls the matching controller method
  (`vm.provision()`, `vm.start()`, …). The state-machine guards on the
  VM live there; Retry does not duplicate them. If the VM is in a
  state that disallows the action, the controller's existing
  `frappe.throw` surfaces to the operator.
- For operator-visible server scripts (`bootstrap-server.py`,
  `reboot-server.sh`, `sync-image.py`): re-invokes
  `Server.run_task_dialog(self.script, self.variables_dict)` so the
  retry is recorded as a fresh Task row with the original variables.
- For anything else (e.g. an ad-hoc `noop.sh`): throws "not retriable
  from the Task form."

A retry is a new Task row, not a mutation of the failed one. The audit
trail keeps both.
