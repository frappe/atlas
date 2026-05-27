# Where to start — solution

Maps to [research/08-where-to-start.md](../research/08-where-to-start.md).

The three highest-leverage fixes, in the order they should land. Each
block lists the exact files to touch and the leaf-level changes inside
them. All three are independently shippable — none blocks another.

## 1. Replace the Run Task dialog with a script-aware form

Detail in [03-server-solution.md §3](./03-server-solution.md#3-run-task-dialog-is-the-worst-offender).

### Files to touch

| File                                              | Change                                                                                      |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `atlas/atlas/scripts_catalog.py`                  | Add `operator_visible_scripts()` returning the three-script whitelist.                       |
| `atlas/atlas/doctype/server/server.py`            | `get_scripts()` returns `operator_visible_scripts()` (was `allowed_scripts()`).              |
| `atlas/atlas/doctype/server/server.js`            | New per-script field schema; on Script change call `dialog.set_fields([...])`. Add "advanced" toggle gated on `frappe.user_roles.includes("System Manager")`. |
| `atlas/tests/e2e/use_cases/desk_buttons.py`       | New negative case: hidden scripts no longer in the picker output. Existing "advanced" path still tested. |

### Wireframe (recap)

```
Script * [ bootstrap-server.sh ▾ ]
   ↓ on change
Firecracker Version  [ v1.15.1 ]    Architecture  [ x86_64 ▾ ]
□ Show advanced (System Manager)
                                              [ Cancel ]   [ Run → ]
```

### Sequencing

- Land the `operator_visible_scripts()` split first (pure-Python, easy
  to test).
- Then the per-script form (client-only change).
- The "advanced" toggle is last; until it ships, System Managers can
  still hit the API directly with `run_doc_method`.

### Risk

Low. The controller's `run_task_dialog` already validates against the
broader `allowed_scripts()` set; we're only shrinking the picker, not
the API surface. The desk-button-coverage e2e test catches the negative
path automatically.

---

## 2. Make Task detail useful

Detail in [06-task-solution.md](./06-task-solution.md).

### Files to touch

| File                                              | Change                                                                                          |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `atlas/atlas/doctype/task/task.json`              | Add `subject: Data (read-only)`. Set `title_field = "subject"`.                                  |
| `atlas/atlas/doctype/task/task.py`                | `before_insert`: compute subject. `retry()` whitelisted method. Publish `task_update`+`task_log`.|
| `atlas/atlas/doctype/task/task.js`                | Dashboard headline + indicators. Retry button. Stdout/stderr log viewer with ANSI colour + tail.|
| `atlas/atlas/ssh.py`                              | Emit `task_log` realtime events on each line; emit `task_update` on status transitions.         |
| `atlas/atlas/api/task.py` (new)                   | `stdout_raw`/`stderr_raw` endpoints returning `text/plain`.                                     |
| `atlas/patches/`                                  | Patch to backfill `subject` on existing Task rows.                                              |
| `atlas/atlas/doctype/virtual_machine/virtual_machine.js` | Listen for `task_update` to refresh the form. Set red intro on last-failure.            |
| `atlas/atlas/doctype/virtual_machine/virtual_machine.py` | Flip `status = Failed` when provision Task ends in Failure.                              |

### Wireframe (recap)

```
✗  Failed in 16s. Exit code 1.
   provision-vm.sh: line 20: VIRTUAL_MACHINE_NAME: required

● Server bootstrap-server-1779879805 →
● Virtual Machine verify vnet_hdr fix · 8f3cf032 →
● Triggered by Administrator

Output
─── stderr (2.9 KB) ──── Copy   Wrap   ↗ Open   🔍
┌────────────────────────────────────────────────┐
│ + set -euo pipefail                            │
│ provision-vm.sh: line 20: VIRTUAL_MACHINE_NAME │
│ ...                                            │
└────────────────────────────────────────────────┘

Sibling tasks (same VM)
● Failure  Provision VM   17m ago    8k6u4v3bi1 →
```

### Sequencing

1. **Subject field + title_field.** Backfill via patch. Lowest-risk
   schema change.
2. **Realtime events** (`task_update`, `task_log`) in the SSH runner.
3. **Form layout overhaul** (headline, indicators, log viewer, retry).
4. **VM-status repair** (Failed on failure + intro link).

### Risk

Medium. The log-viewer rewrite is the riskiest piece because it
touches Code-field rendering — falling back to a plain `<pre>` driven
from realtime events keeps risk bounded if the Code-field override
proves fragile.

---

## 3. Confirm and preview every destructive/expensive action

Detail in [07-cross-cutting-solution.md §3](./07-cross-cutting-solution.md#3-no-confirmations-on-anything-destructive-or-expensive).

### Files to touch

| File                                                       | Change                                                                            |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `atlas/public/js/atlas_form_overrides.js` (new)            | `frappe.atlas.confirm_cost`, `confirm_destructive`, `add_primary/secondary/action/danger`, `task_started` helpers. |
| `atlas/hooks.py`                                           | `doctype_js` registering the shared script for all five doctypes.                  |
| `atlas/atlas/doctype/server_provider/server_provider.js`   | Provision Server → typed confirm with cost preview. Test Connection under Actions. |
| `atlas/atlas/doctype/server/server.js`                     | Reboot → typed confirm with running-VM count.                                      |
| `atlas/atlas/doctype/virtual_machine/virtual_machine.js`   | Terminate → typed confirm with VM short ID.                                        |
| `atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.js` | Sync to All Servers → typed confirm with server count + bandwidth estimate. |
| `atlas/atlas/api/digitalocean.py`                          | New `monthly_cost(region, size)` helper backed by a small static dict.             |

### Wireframes (recap)

```
Provision Server confirmation:
⚠  Create a billable droplet?
   This will create a s-2vcpu-4gb-intel droplet in blr1 (≈ $24/mo).
   It starts billing immediately and cannot be paused.
                                  [ Cancel ]   [ Provision ]

Reboot confirmation:
⚠  Reboot bootstrap-server-1779879805?
   This server is running 4 virtual machines. All will lose
   connectivity until the host returns.
   Type the server name to confirm:
   [                                                          ]
                                  [ Cancel ]   [ Reboot ]

Terminate confirmation:
⚠  Terminate verify vnet_hdr fix?
   IPv6 [2400:6180:100:d0:0:1:4ae1:d001]
   This deletes the VM's disk artifacts on the host. UUID and Task
   history are preserved.
   Type the short ID to confirm:
   [ 8f3cf032 ]
                                  [ Cancel ]   [ Terminate ]

Sync to All Servers confirmation:
⚠  Sync to N active servers?
   Image: ubuntu-24.04  (≈ 620 MB)
   Targets:
     • bootstrap-server-1779879805   blr1   Active
     • bootstrap-server-1779879806   sgp1   Active
   Each download takes ~3 min and consumes 620 MB.
                                  [ Cancel ]   [ Sync to All ]
```

### Sequencing

1. **Helpers landed first.** Tiny shared module — `frappe.atlas.*`
   helpers + the `hooks.py` registration. Self-contained.
2. **Per-doctype wiring**, in the order operators are most likely to
   hit them: Provision Server → Reboot → Terminate → Sync to All.
3. **Cost preview**. The static `monthly_cost` table is operator-
   maintained; if DO changes prices, we update the dict — same
   policy as `default_image`.

### Risk

Low. Each confirm is purely a client-side gate around an existing call.
The new helpers are 60–80 LOC total. No schema changes; no controller
changes.

---

## What lands after the top three

Once the three above are in, the next-best uses of effort are:

1. **Workspace bootstrap checklist + dashboard rebuild**
   ([01-workspace-solution.md §1, §3, §4](./01-workspace-solution.md)).
   High first-impression impact for new operators. **(Landed.)**
2. **Hide the app launcher** (skip `/desk`, land on `/app/atlas` directly).
   One-line `hooks.py` change. **(Deferred — see 01-workspace §2.)**
3. **VM-form upgrades** — short-ID list rendering, IPv6 copy chip,
   capacity preview, terminated-VM "Re-provision as new" action.
   **(Landed: list short-ID + IPv6 copy chip [05 §1], Pending IPv6 chip
   + auto-expand [05 §2], SSH command + copy [05 §5], terminated
   Re-provision + Delete record [05 §3], creation form polish —
   description nudge, size presets, capacity preview [05 §6]. The
   provider-default SSH key auto-fill from 05 §6 remains deferred —
   it needs a `default_vm_public_key` field on `Server Provider`.)**
4. **Image sync status panel** so the operator can see "which servers
   have this image?" without grepping Task history. **(Landed: per-active
   server table + Sync now shortcut [04 §3].)**
5. **VM status repair on provision failure** so failed provisions stop
   leaving the VM stuck in Pending. **(Landed: Task.on_update
   propagates Failure → VM Failed [06 §5].)**
6. **Server form running-Task headline + Recent Tasks panel** so
   in-flight activity is visible without leaving the Server form.
   **(Landed [03 §2].)**
7. **Image kernel/rootfs lock after first successful sync** so editing
   doesn't silently invalidate the audit trail. **(Landed [04 §4].)**

After that the long tail of polish: ANSI colours, autocomplete on
provider region/size/image, and live autocomplete for default
region/size/image
([02 §5](./02-server-provider-solution.md#5-default-regionsizeimage-are-free-text-data-fields)).
The API-token validity indicator
([02 §4](./02-server-provider-solution.md#4-api-token-and-ssh-private-key-unverifiable))
has landed. The VM-creation form polish — size presets, capacity
preview, description nudge
([05 §6](./05-virtual-machine-solution.md#6-creation-form-is-too-generic)) —
has also landed; only the deferred `default_vm_public_key` field
remains.

## What deliberately does not land

- **No bench-side configuration UI.** Bootstrap from
  `atlas/bootstrap.py` stays the CLI escape; it's already operator-
  documented in `spec/README.md`.
- **No custom SPA, no web terminal.** A copy-to-clipboard SSH chip is
  enough.
- **No metrics/alerting dashboards.** Spec is explicit:
  `journalctl` is enough. The "Recent activity" feed + Failed Tasks
  (24h) count is the only operational signal on the workspace.
