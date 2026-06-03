// The status → lifecycle action map. Every action lives in the header's ⋯
// menu (the page's one primary button is "Open Bench", not a lifecycle verb).
// Each action names the EXISTING whitelisted controller method on Virtual
// Machine (virtual_machine.py) — the SPA invents no server-side method. A unit
// test (test_action_map.py) pins that every method named here is actually
// @frappe.whitelist()'d.
//
// kind controls how the menu item renders + what clicking it does:
//   'default'    — neutral, runs immediately (Start, Resume, Provision).
//   'disruptive' — red, runs immediately. Reversible but interrupts the VM
//                  (Stop, Restart, Pause); red signals the disruption.
//   'action'     — neutral, opens a form dialog via `dialog` (Snapshot, Resize).
//   'danger'     — red, confirms first. Irreversible (Rebuild, Terminate,
//                  Delete) and surfaced in the Danger zone card.
// A 'danger' action may carry `args(doc)` to build its method params from the
// doc when there's no form to collect them (e.g. Rebuild).

export const ACTIONS = {
  Running: [
    { label: 'Stop', method: 'stop', kind: 'disruptive' },
    { label: 'Restart', method: 'restart', kind: 'disruptive' },
    { label: 'Pause', method: 'pause', kind: 'disruptive' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Stopped: [
    { label: 'Start', method: 'start', kind: 'default' },
    { label: 'Restart', method: 'restart', kind: 'disruptive' },
    { label: 'Snapshot', method: 'snapshot', kind: 'action', dialog: 'snapshot' },
    // Rebuild takes no input — it replaces the disk from the VM's own image —
    // so it's a confirm (danger), not a form dialog. args() reads the doc.
    {
      label: 'Rebuild',
      method: 'rebuild',
      kind: 'danger',
      args: (doc) => ({ source_type: 'image', source: doc.image }),
    },
    { label: 'Resize', method: 'resize', kind: 'action', dialog: 'resize' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Paused: [
    { label: 'Resume', method: 'resume', kind: 'default' },
    { label: 'Stop', method: 'stop', kind: 'disruptive' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Pending: [],
  Failed: [
    { label: 'Provision', method: 'provision', kind: 'default' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Terminated: [{ label: 'Delete', method: '__delete__', kind: 'danger' }],
}

export function actionsFor(status) {
  return ACTIONS[status] ?? []
}
