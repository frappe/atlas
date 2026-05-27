# Desk UX solutions — 2026-05-27

One solution document per UX research file. Each solution is written
against the **same Frappe desk** the operator already runs in: standard
`frappe.ui.Dialog`, `frappe.confirm`, `frappe.warn`, `frm.add_custom_button(label, fn, group)`,
form intros, dashboard indicators, workspace shortcuts, list view
indicators, and connection-dashboard links.

The aim is to fix UX with standard components — not by replacing Desk
with a custom SPA. Where Desk's own chrome (right rail, Comments, Tags,
Share) actively gets in the way the document says so explicitly and
flags it as a "strip Desk" item.

## Files

- [01-workspace-solution.md](./01-workspace-solution.md)
- [02-server-provider-solution.md](./02-server-provider-solution.md)
- [03-server-solution.md](./03-server-solution.md)
- [04-virtual-machine-image-solution.md](./04-virtual-machine-image-solution.md)
- [05-virtual-machine-solution.md](./05-virtual-machine-solution.md)
- [06-task-solution.md](./06-task-solution.md)
- [07-cross-cutting-solution.md](./07-cross-cutting-solution.md)
- [08-where-to-start-solution.md](./08-where-to-start-solution.md) — the
  three highest-leverage fixes, with the exact files to touch first.

Each solution document follows the same shape:

1. **Problem** — one-line restatement of the research finding.
2. **Proposed solution** — what to do, in terms of standard Frappe
   primitives (server method, button group, dialog, indicator, dashboard
   link, list filter, workspace card, …).
3. **Wireframe** — ASCII layout of the affected surface so a reader can
   eyeball what the operator will see.
4. **Frappe components used** — the concrete API calls (`frm.add_custom_button`,
   `frappe.warn`, `frm.dashboard.set_headline_alert`, …).
5. **Fighting Desk?** — yes/no, and what to strip if yes.

## Principles applied across every solution

- **Standard components first.** A Frappe `Dialog` with conditional
  fields beats a custom Vue form. `frappe.warn` (red, typed confirm)
  beats a hand-rolled red modal.
- **Action hierarchy via button groups.** Common actions live as bare
  custom buttons; rare/destructive actions go under a "More" group so
  they don't compete visually. Destructive actions use
  `frm.change_custom_button_type(label, group, "danger")` for the red
  pill.
- **Form intros for guidance.** `frm.set_intro(html, color)` replaces
  every "we should tell the operator what's coming next" comment.
- **Dashboard indicators for state.** `frm.dashboard.add_indicator(text, color)`
  replaces the duplicate status pill / "what's normal" callouts at the
  top of the form.
- **Connection dashboards bidirectionally.** Every parent → child link
  in the spec also flows back: Task → Virtual Machine, Task → Server.
- **Realtime over polling.** Tasks already exist; the controller emits a
  `frappe.publish_realtime("task_update", {...})` event on every state
  change and the Task form subscribes via
  `frappe.realtime.on("task_update", …)`.
- **Bootstrap-script-driven hosting.** The desk hides every script that
  shouldn't be operator-triggered (`provision-vm.sh`, `start-vm.sh`,
  `stop-vm.sh`, `terminate-vm.sh`, `vm-network-up.sh`,
  `vm-network-down.sh`) by moving the catalog filter into
  `scripts_catalog.operator_visible_scripts()`. The hidden scripts still
  run from VM/Image controllers — only the picker shrinks.

## What we deliberately do not build

- **No custom SPA.** Desk + standard dialogs + intros + indicators is
  enough. We strip the right rail and Comments panel where they get in
  the way; we don't replace the form.
- **No web terminal.** "SSH to this VM" is a copy-to-clipboard chip
  showing `ssh root@[ipv6]`, not an in-browser shell.
- **No bespoke log viewer.** Frappe `Code` field with monospace + tail
  via `publish_realtime` covers 90% of "the log panel needs love."
- **No new DocTypes.** Every solution lands in client scripts, server
  methods, dashboards, workspace, and list view settings on the
  existing five DocTypes.
