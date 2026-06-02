// The status → lifecycle action map. One primary per status; subtle siblings;
// rare/destructive fold into Actions ▾. Each action names the EXISTING
// whitelisted controller method on Virtual Machine (virtual_machine.py) — the
// SPA invents no server-side method. A unit test (test_action_map.py) pins
// that every method named here is actually @frappe.whitelist()'d.
//
// kind: 'primary' (one per status), 'subtle', 'action' (in Actions ▾, opens a
//       form dialog via `dialog`), 'danger' (in Actions ▾, red, confirmed).
// A 'danger' action may carry `args(doc)` to build its method params from the
// doc when there's no form to collect them (e.g. Rebuild).

export const ACTIONS = {
  Running: [
    { label: 'Stop', method: 'stop', kind: 'primary' },
    { label: 'Restart', method: 'restart', kind: 'subtle' },
    { label: 'Pause', method: 'pause', kind: 'subtle' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Stopped: [
    { label: 'Start', method: 'start', kind: 'primary' },
    { label: 'Restart', method: 'restart', kind: 'subtle' },
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
    { label: 'Resume', method: 'resume', kind: 'primary' },
    { label: 'Stop', method: 'stop', kind: 'subtle' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Pending: [],
  Failed: [
    { label: 'Provision', method: 'provision', kind: 'primary' },
    { label: 'Terminate', method: 'terminate', kind: 'danger' },
  ],
  Terminated: [{ label: 'Delete', method: '__delete__', kind: 'danger' }],
}

export function actionsFor(status) {
  return ACTIONS[status] ?? []
}
