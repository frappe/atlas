# Investigate a problem with an agent

Atlas runs two [Flow](https://github.com/frappe/frappe/tree/develop/frappe/flow) agents that investigate a regional problem. They read telemetry, records, and hosts with fixed tools. Every tool result is scrubbed, so the model never receives a credential.

| Agent | Who starts it | Can change state |
| --- | --- | --- |
| Atlas Investigator | Central, through `POST /api/atlas/investigations` | No. It has only read-only tools. |
| Atlas Engineer Assistant | An Atlas engineer, in the Flow chat (`/flow`) | Yes, after the engineer approves each call in the chat. |

No agent can terminate a VM, archive a host, delete a record, or read a password or a console token.

## Configuration

Set these values in **Atlas Settings > LLM Investigation**:

| Field | Value |
| --- | --- |
| Investigation Model | A Flow Model. Without it, Atlas does not create the agents. |
| Enable Investigation | Off by default. While it is off, Atlas refuses `POST /api/atlas/investigations` and every agent tool call. |
| Telemetry Query URL, Database, Username, Password | A read-only ClickHouse user on the regional Datum database. |

Create the ClickHouse user with `readonly = 2 CONST` and only `SELECT` on the Datum database:

```sql
CREATE USER atlas_investigator IDENTIFIED WITH sha256_password BY '<password>'
  SETTINGS readonly = 2 CONST, max_execution_time = 30 MAX 60;
GRANT SELECT ON datum.* TO atlas_investigator;
```

Then run `migrate` to create the agents and the skills. Set an embedding model in **Flow Knowledge Settings**, and load the Atlas knowledge base with `execute atlas.operations.investigator.knowledge.load_knowledge_base`. Run the load again after a deploy that changes the documents.

## How an investigation works

1. The agent loads the `incident-investigation` skill.
2. It runs the alert queries and checks the telemetry age of each resource.
3. It checks the raw data behind each alert, because an alert query can be wrong.
4. It finds what changed on each affected host, then tests 2 to 4 possible causes against Datum.
5. It reads a host over SSH only for data that Datum does not have: the Firecracker log, journal lines below warning, live state, or a host with stale telemetry.
6. It writes a report of at most 25 lines: the cause, impact, confidence, an ASCII cause chain and timeline, evidence, ruled-out causes, and next steps.

Every reply uses ASD-STE100 Simplified Technical English as a short bullet list. The `simplified-technical-english` skill has the rules. An engineer can start either skill from the **Skills** menu or with `/skill:<name>` in the Flow chat.

A Central investigation that reaches the iteration limit gets one more turn to write its report from the evidence it has.

## Tools

| Group | Tools | Approval |
| --- | --- | --- |
| Telemetry | `list_failing_alert_checks`, `get_telemetry_freshness`, `list_saved_queries`, `run_saved_query`, `query_telemetry`, `get_counter_increase`, `get_reconciler_status` | None |
| Records | `find_resources`, `describe_host`, `describe_virtual_machine`, `list_recent_errors`, `read_error_log`, `get_host_changes` | None |
| Host reads | `read_firecracker_log`, `read_virtual_machine_journal`, `read_metald_journal`, `check_host_health`, `run_read_only_command` | None |
| Desk actions (engineer only) | `list_desk_actions`, `run_desk_action` | None |
| Changes (engineer only) | `run_dangerous_desk_action`, `set_record_value`, `run_command` | Each call |
| Records (engineer only) | `save_investigation` | None |

An agent can run only the Desk actions in `ALLOWED_ACTIONS` in `atlas/operations/investigator/desk_actions.py`. Every other method is blocked, such as terminate, archive, deletion, and methods that return a secret.

- A normal action is the method behind an **Actions** or **Diagnostics** button. `run_desk_action` runs it without approval, except on a VM of a customer tenant.
- A dangerous action is a **Dangerous Actions** button or a fleet change, such as Upgrade Metald or a Metald Update rollout. `run_dangerous_desk_action` runs it after approval.

Update `ALLOWED_ACTIONS` when you add a button or move it between groups.

`set_record_value` changes one editable field, such as **In Maintenance** of a Metal Server, and saves the record as the form does.

`run_read_only_command` accepts a command only when a parser finds only read-only programs and arguments. It refuses redirection, command substitution, state-changing subcommands such as `systemctl restart` or `ip link set`, and paths that can hold a secret.

No tool connects to a VM of a customer tenant, because Atlas has no SSH key on it.

A read tool runs over SSH and returns the scrubbed output directly. Its record is the row in the Investigation log. A `run_command` call on a host is a Metal Server Console Log with **Scrub Output** on, and on a tenant 0 VM it is an SSH Task.

## Investigation records

An **Investigation** stores the problem, the context, the outcome, and an audit log. Each chat with an Atlas agent has exactly one Investigation:

- Central creates one with `POST /api/atlas/investigations` and reads it with `GET /api/atlas/investigations/{id}`. Atlas fills the outcome when the run ends.
- An engineer chat gets one at its first tool call. Its problem is the first message, and its title is the title of the chat. The agent calls `save_investigation` to write the problem, the outcome, and the decisions. A second call updates the same record.

The status follows the latest run of the chat. A running run, or a run that waits for approval, is Running. A completed run is Completed, and a failed run is Failed. A new message in the chat makes the Investigation Running again. Atlas marks a queued or running Investigation as Failed when it and its chat have no activity for 3 hours.

Atlas writes one log row for each tool call, when the tool runs. The model cannot change these rows, and nobody can edit them in the Desk. A row has:

| Field | Value |
| --- | --- |
| Kind | Tool Call for a read, Action for a Desk action, a command, or a field change, Decision from `save_investigation` |
| Status | Success, Failed, or Refused. A refused or failed call is logged too. |
| User, Approved | The chat owner. Approved is set when the person in the chat approved the call. It is not set when the chat skips approval. |
| Reference | The console log or SSH Task of the call, else the record that a Desk action acted on |
| Detail | The scrubbed arguments, and the scrubbed result or error |

The reference is text, not a Link field, so that you can still delete an old console log or SSH Task. The form shows it as a link. A denied approval runs no tool, so it has no row.
