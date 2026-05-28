# Phase 5 — Task (post-implementation wireframe)

Touches: subject simplification, drop chips/sibling-tasks, output to
collapsible section, states-driven status pill, list-view de-duplication.

## Task list

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Task                                                          + Add Task      │
├────────────────────────────────────────────────────────────────────────────────┤
│  Subject                Status        Script          Server              VM   │
│  ────────────────────  ─────────────  ──────────────  ─────────────────  ───── │
│  Sync Image · 14s      [Success]      sync-image.sh   vm-test-server    —     │
│  Reboot · 21s          [Success]      reboot-server.. vm-test-server    —     │
│  Create Virtual Mach.. [Running]      provision-vm..  vm-test-server    abc1.. │
│  Start · 3s            [Failure]      start-vm.sh     vm-test-server    abc1.. │
└────────────────────────────────────────────────────────────────────────────────┘
```

Status column carries its own coloured pill (driven by DocType `states`
array). Subject reads as the verb (or verb + noun for *new-object*
operations); the duration suffix (` · 14s`) survives so a glance still
tells the operator how long it took. The legacy `· <target>` suffix is
gone — Server / VM columns already carry that.

## Task form (Success)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Task / Sync Image                                       Retry (hidden)      │
├──────────────────────────────────────────────────────────────────────────────┤
│  Completed in 14s. Exit code 0.                       (green headline)       │
├──────────────────────────────────────────────────────────────────────────────┤
│  Overview                                                                    │
│  ─────────────────────                                                       │
│  Subject              [Sync Image]                                           │
│  Server               [vm-test-server]                                       │
│  Virtual Machine      [—]                                                    │
│  Script               [sync-image.sh]                                        │
│  Triggered By         [Administrator]                                        │
│  Status               [Success]                                              │
│  Exit Code            [0]    Duration (ms)   [13841]                         │
│                                                                              │
│  Timing                                                                      │
│  ─────────────────────                                                       │
│  Started   2026-05-28 12:00:00     Ended    2026-05-28 12:00:14              │
│                                                                              │
│  Inputs                                                                      │
│  ─────────────────────                                                       │
│  Variables (JSON)                                                            │
│  {                                                                           │
│    "IMAGE_NAME": "ubuntu-24.04",                                             │
│    "SERVER_NAME": "vm-test-server"                                           │
│  }                                                                           │
│                                                                              │
│  ▸ Output            (collapsible section, was a Tab)                        │
│    stdout / stderr   (24em min-height)                                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

Removed: header chips ("Server: …", "VM: …", "Triggered by …" — all
redundant with the columns below); the Sibling Tasks quick_list section.

## Failure form

```
Failed in 16s. Exit code 1.        (red headline — no stderr-tail snippet)
```

The bespoke "first line of stderr" tail-extraction is gone. Operator
expands the Output section to read the actual stderr.

## What changed

1. **Schema** ([task.json](../../../atlas/atlas/doctype/task/task.json))
   - `tab_output` changed from Tab Break to collapsible Section Break,
     folded under Overview
   - Added `states` JSON: Pending/Yellow, Running/Blue, Success/Green,
     Failure/Red
2. **Controller** ([task.py](../../../atlas/atlas/doctype/task/task.py))
   - `SCRIPT_LABELS` rewritten per verb / verb-noun rule
   - `_build_subject` reduced to just the label lookup
   - Dropped `_target_short` (no longer needed — target identity
     lives in dedicated columns)
3. **JS** ([task.js](../../../atlas/atlas/doctype/task/task.js))
   - Dropped `render_chips` (Server / VM / Triggered By indicators)
   - Dropped `render_sibling_tasks` (quick_list under the headline)
   - Dropped `first_stderr_line` extraction; Failure headline is the
     single "Failed in Xs. Exit code N." line
4. **list_js** ([task_list.js](../../../atlas/atlas/doctype/task/task_list.js))
   - Dropped `get_indicator` (DocType `states` now drives the pill)
5. **Migration patch** ([rebuild_task_subjects.py](../../../atlas/patches/v1_0/rebuild_task_subjects.py))
   - Replaces the legacy `backfill_task_subject` patch
   - Walks every Task and rewrites `subject` per the new `SCRIPT_LABELS`
6. **Tests** ([test_task.py](../../../atlas/atlas/doctype/task/test_task.py))
   - Subject assertions updated to expect verb / verb-noun labels
   - Added `test_states_array_paints_status_pills` (pins the states
     JSON schema)
7. **VM Image JS**: `frappe.atlas.task_started(frm, "Sync image", …)`
   → `"Sync Image"` (capitalisation matches `SCRIPT_LABELS`)
