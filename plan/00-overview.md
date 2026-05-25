# Atlas — Implementation Plan

Eight phases. Each phase is independently testable and ends with an end-to-end
run against a real DigitalOcean droplet.

The spec lives in [`../spec/`](../spec/README.md). The plan does not duplicate
the spec; it sequences the build, names every file, and pins every test. When
the spec is ambiguous, the plan picks an answer and flags it in
[`drift.md`](./drift.md) for later resolution.

## Phases

| # | Title                              | File                                                                  | Days |
|---|------------------------------------|-----------------------------------------------------------------------|------|
| 1 | SSH plumbing + Task DocType        | [phase-1-ssh-and-task.md](./phase-1-ssh-and-task.md)                  | 1    |
| 2 | DigitalOcean API client            | [phase-2-digitalocean-client.md](./phase-2-digitalocean-client.md)    | 0.5  |
| 3 | Server Provider + Server (bootstrap) | [phase-3-server-and-bootstrap.md](./phase-3-server-and-bootstrap.md) | 1    |
| 4 | Virtual Machine Image + sync       | [phase-4-image-and-sync.md](./phase-4-image-and-sync.md)              | 0.5  |
| 5 | Virtual Machine DocType (provision)| [phase-5-vm-provision.md](./phase-5-vm-provision.md)                  | 1    |
| 6 | VM lifecycle (start/stop/delete)   | [phase-6-vm-lifecycle.md](./phase-6-vm-lifecycle.md)                  | 0.5  |
| 7 | Run-Task escape hatch + polish     | [phase-7-run-task-and-polish.md](./phase-7-run-task-and-polish.md)    | 0.5  |
| 8 | Permissions + docs + handoff       | [phase-8-permissions-and-docs.md](./phase-8-permissions-and-docs.md)  | 0.5  |

Total: ~5.5 dev-days.

## Phase contract

Every phase file follows the same shape:

1. **Goal** — one-paragraph description.
2. **You can do this at the end** — operator-visible milestone.
3. **Files added or changed** — concrete paths.
4. **Implementation notes** — only the non-obvious; never restate the spec.
5. **End-to-end test** — the bench command that proves it works.
6. **What we are NOT doing in this phase** — explicit exclusions.
7. **Spec drift introduced** — link into [`drift.md`](./drift.md).

## Conventions used by every phase

### Code style

- Follows [`../llm/Taste.md`](../llm/Taste.md): full words, classes where they
  fit, files between 100–300 lines, modules under 15 files.
- Type-annotated whitelisted methods (already enforced via
  `require_type_annotated_api_methods = True` in [`../atlas/hooks.py`](../atlas/hooks.py)).
- `ruff` per [`../pyproject.toml`](../pyproject.toml). Tabs for indent.
- Tests live next to the doctype they cover
  (`atlas/atlas/doctype/<name>/test_<name>.py`).

### DocType layout

Standard Frappe paths:

```
atlas/atlas/doctype/<doctype_name>/
├── __init__.py
├── <doctype_name>.json    # field schema
├── <doctype_name>.py      # controller
├── <doctype_name>.js      # form scripts (buttons)
└── test_<doctype_name>.py # unit tests with mocked SSH/DO
```

The module is `Atlas` (see [`../atlas/modules.txt`](../atlas/modules.txt)).

### Secret access

Every phase that needs a secret (SSH key, DO token) reads it via
`atlas/atlas/secrets.py::get_secret(doctype, name, field) -> str` (introduced
in phase 1). The body just calls
`frappe.utils.password.get_decrypted_password()` today; the indirection lets
us swap the backend later without touching callers. This is the "Secret
indirection" hedge from [`../spec/09-roadmap.md`](../spec/09-roadmap.md).

### Shell scripts call sudo

Every privileged command in `../scripts/*.sh` is prefixed with `sudo`. The
SSH user is `root` today so this is a no-op. The day we add an `atlas`
unprivileged user, "rewrite every script" turns into "create the user." This
is the second roadmap hedge we're adopting upfront.

The existing scripts in [`../scripts/`](../scripts/) do **not** use `sudo`
today. Phase 3 is where they all get updated, because that's the first phase
that actually runs them.

### Background jobs

Long Tasks run via `frappe.enqueue` with `queue="long"`:

- `bootstrap-server.sh` (30–60s)
- `sync-image.sh` (minutes)

Short Tasks run synchronously in the request:

- `provision-vm.sh` (~3s)
- `start-vm.sh`, `stop-vm.sh`, `delete-vm.sh` (<1s)

Both paths funnel through `atlas/atlas/ssh.py::run_task()`. The decision of
"enqueue or not" lives in the calling DocType method, not in `run_task`.

### End-to-end tests

Every phase ships a bench command:

```
bench --site atlas.local execute atlas.tests.e2e.phase_N.run
```

**Phase 1** runs against a droplet the operator has spun up by hand
(connection in site config); no DO API access, no cleanup. See
[phase-1-ssh-and-task.md](./phase-1-ssh-and-task.md#e2e).

**Phases 2 onward** create their own throwaway droplets via the DO client.
Each runner:

1. Reads `atlas_do_token` from the site's `common_site_config.json`.
2. **Pre-sweep**: lists droplets tagged `atlas-e2e` and deletes any older
   than 30 minutes. Catches leaked droplets from crashed runs.
3. Wraps the test body in `try/finally`; the `finally` deletes whatever
   droplets the run created, regardless of outcome.
4. Asserts on `Task` rows (status, exit_code, stdout patterns).
5. Prints a one-line summary: `phase-N: OK in 87s` or `phase-N: FAIL ...`.

Tag every e2e-created droplet with `atlas-e2e` so the pre-sweep is safe.

### Permissions

For the iteration: `read` and `write` for `System Manager` only on all five
DocTypes. Tightened in phase 8 to match the spec
("Read permission for System Manager"); `write` becomes implicit via the
buttons.

### `frappe.enqueue` and Task ownership

When a button enqueues a task, the button handler:

1. Inserts the `Task` row with `status = "Pending"` and the variables.
2. Calls `frappe.enqueue("atlas.atlas.ssh.execute_task",
                         task_name=task.name, queue="long")`.
3. Returns immediately with a message: "Task queued: {task.name}".

`execute_task(task_name)` loads the row, sets `status = "Running"`, runs the
script via `run_task_inner()`, and updates the row. This separates "create a
task" (sync, in request) from "run a task" (sync or queued).

For sync calls (provision/start/stop/delete), the button handler calls
`run_task()` directly, which both creates and runs the row.

## Non-goals across all phases

These are excluded from every phase, per
[`../spec/09-roadmap.md`](../spec/09-roadmap.md):

- No CLI. No custom web pages or portal.
- No host-key pinning beyond `accept-new`.
- No jailer, no unprivileged user (just the `sudo` prefix).
- No image build pipeline. No multi-arch.
- No snapshots, resize, migrate, live migration.
- No metrics, no alerting, no health-check reconciler.
- No address reuse on archive.
- No log-spill-to-file. The Task `Code` field holds full stdout/stderr.
- No automatic retries.
- No automatic server reuse outside e2e. The `ensure_bootstrapped_server`
  reuse policy applies only to the e2e harness, where re-bootstrapping a
  fresh droplet for every phase is prohibitively slow and expensive.
  Production code always provisions explicitly.

## Open items intentionally deferred

These are **not** blockers for this iteration but should be picked up after
phase 8:

- Host-key pinning (`Server.ssh_host_public_key` field, captured on first
  successful SSH).
- Spill Task stdout/stderr over ~64 KB to a file under
  `sites/{site}/private/files/atlas-tasks/`.
- Move from `root` SSH user to `atlas` + `sudo` allowlist.
- Bare-metal `Server Provider` type.
- A `health-check` background job.

## File tree at end of phase 8

```
atlas/atlas/
├── atlas/                                  # the Atlas module
│   ├── digitalocean.py                     # DO HTTP client (phase 2)
│   ├── networking.py                       # IPv6 allocator (phase 5)
│   ├── secrets.py                          # get_secret() (phase 1)
│   ├── ssh.py                              # run_task, upload_files (phase 1)
│   └── doctype/
│       ├── server/                         # phase 3
│       ├── server_provider/                # phase 3
│       ├── task/                           # phase 1
│       ├── virtual_machine/                # phase 5/6
│       └── virtual_machine_image/          # phase 4
├── hooks.py
├── modules.txt
└── tests/
    ├── __init__.py
    └── e2e/
        ├── __init__.py
        ├── _shared.py                      # tag, sweep, helpers
        ├── phase_1.py
        ├── phase_2.py
        ├── phase_3.py
        ├── phase_4.py
        ├── phase_5.py
        ├── phase_6.py
        ├── phase_7.py
        └── phase_8.py
scripts/                                    # already exists, edited in phase 3
```
