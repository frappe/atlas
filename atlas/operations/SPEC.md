# Operations module specification

[Atlas app specification](../SPEC.md)

Behavior: [Roll out a build](../../docs/region/hosts-and-providers.md#roll-out-a-build), [run a script on many hosts](../../docs/region/hosts-and-providers.md#run-a-script-on-many-hosts), and [drain a host](../../docs/compute/migration.md#drain-a-host).

## Types

| Type | Owns |
|---|---|
| `MetaldUpdate` (DocType) | Pinned build, rollout settings, status, and Desk actions. |
| `MetaldUpdateServer` (DocType) | Server batch, status, versions, and upgrade SSH Task. |
| `run_metald_update_steps` | Runs one step of each Planning or Running update every 5 seconds. |
| `read_server_versions` | Reads one host and records a planning or check result. |
| `MetalServerConsole` (Single DocType) | Desk form that starts a Metal Server Console Log. It does not save. |
| `MetalServerConsoleLog` (DocType) | One script run as root on many Metal Servers, its servers, and their results. |
| `ServerRun` | The run on one server. Its thread writes only to this object. |
| `MetalServerDrain` (DocType) | Source host, optional VM selection, destination limits, migration attempts, and drain progress. |
| `MetalServerDrainVirtualMachine` (Child DocType) | One VM selected for a drain. |

## Invariants

- A step locks the update row, works from stored row statuses, and only queues jobs. It makes no Metal or SSH call, so a lost or repeated step is safe.
- A step does not start an upgrade while the Metal Server `metald_job_id` job runs.
- Metal Server stores no version. Versions come from `MetalClient.get_versions`, and a `404` means that the server needs the build.
- Atlas keeps each File that an update links, so a duplicate can roll the servers back.
- A Metal Server Console Log runs one thread per server in one `long` job. Only the job thread writes to the database.
- A drain uses its VM selection only when `move_only_selected_vms` is set. Candidate selection, completion, and progress use the same scope.
- `is_sleepy_rebalance` identifies automatic drains. They preserve source maintenance and recheck eligibility in the [rebalance policy](../vm/core/sleepy_rebalance.py). Manual drains retain their normal ordering and completion rules.

## Investigation

Behavior: [Investigate a problem with an agent](../../docs/operate/investigate.md). The Flow agents and their tools are in `investigator/`. The records are the Investigation DocTypes.

| Type | Owns |
| --- | --- |
| `Investigation` (DocType) | Problem, context, outcome, status, Flow Session, and the log. One for each chat (`get_for_session`). Starts the investigator for a Central investigation and records its run. `record_tool_call` logs a tool call. |
| `InvestigationEntry` (Child DocType) | One tool call, decision, or note: kind, status, user, approval, a text reference, and the scrubbed detail. `append_to` inserts one row. |
| `TelemetryQuery` | Read-only SQL on the regional Datum ClickHouse, the saved workbook queries, the alert checks, and telemetry age. |
| `validate_read_only_command` | Parser that accepts only read-only shell commands. |
| `desk_actions` | `ALLOWED_ACTIONS`: the Desk actions an agent may run, normal or dangerous. Also the editable fields. |
| `get_agents` | The `flow_agents` hook: Atlas Investigator and Atlas Engineer Assistant. |
| `load_knowledge_base` | Loads `docs/` and every `SPEC.md` into the Atlas knowledge base. |
| `investigator/tools/` | Flow tools, grouped by subject. `decorators.py` adds the role check, the scrubbing, and the log row. |

The skills are [`incident-investigation`](../../skills/incident-investigation/SKILL.md), with its playbooks and telemetry reference, and [`simplified-technical-english`](../../skills/simplified-technical-english/SKILL.md). `enable_skill_commands` runs after migrate and lets engineers start them with `/skill:<name>`. Host scripts are in [`HostDiagnostics`](../metal_server/core/host_diagnostics.py). The scrubber is [`atlas/core/scrub.py`](../atlas/core/scrub.py).

### Invariants

- While **Enable Investigation** in Atlas Settings is off, `validate_investigation_enabled` refuses a Central investigation and every tool call.
- Every tool result goes through `scrub_value`. A tool returns nothing that the scrubber did not see.
- `read_tool` needs Atlas Investigator or System Manager. `change_tool` and `engineer_tool` need System Manager. `change_tool` always needs approval in the chat.
- The investigator agent has no `change_tool`. Central investigations run as `investigator@atlas.internal`, which has only the Atlas Investigator and Flow User roles.
- No tool terminates, archives, or deletes, and no tool returns a password or a token. `ALLOWED_ACTIONS` is an allowlist: a method that is not in it is blocked.
- `run_desk_action` runs only a normal action, and not on a VM of a customer tenant. A dangerous action, or a normal action on a customer VM, goes through `run_dangerous_desk_action`, which needs approval.
- A read tool reads over SSH with `HostDiagnostics.read` or `SSHRunner` and stores no record. The Investigation log row is its record. `run_command` is a Metal Server Console Log with `scrub_output` set on a host, and an SSH Task on a tenant 0 VM. No tool connects to a VM of a customer tenant: Atlas has no SSH key on it.
- The saved queries come from `scripts/insights/generate_workbook.py`, so the dashboards and the agents use the same SQL.
- `_guarded` logs every tool call, also a refused or failed one. For a failed call it rolls back the tool's writes, inserts the row, and commits, because Flow rolls back the call.
- A log row is only inserted with `InvestigationEntry.append_to`. Code changes an Investigation with `db_set`, never `save`, because a save rewrites the table and loses a row that a parallel tool call inserted.
- A reference is two Data fields, not a Dynamic Link, so that a console log or SSH Task can still be deleted.
- `sync_with_flow_session` runs on each save of a Flow Session. It copies the status of the latest Flow Run, and the title of an engineer chat. Flow can change a run with `db_set`, so a Flow Run hook would miss changes.
- `Investigation.start_investigator` commits the Flow Session link before the run starts, because a short run can end before `chat` returns.

### Tests

- `investigator/test_command_policy.py`: accepted and refused commands.
- `doctype/investigation/test_investigation.py`: one Investigation for each chat, logged calls with text references and scrubbed detail, and rows that survive a summary change.
- `investigator/test_desk_actions.py`: every allowed action is a whitelisted method, and other actions and read-only fields are refused.
- `../atlas/core/test_scrub.py`: masked and kept values, and line-by-line stability.
- `doctype/metal_server_console_log/test_metal_server_console_log.py`: scrubbed stored and streamed output.
