// Status → Badge theme, defined once (frappe-ui PATTERNS "Status badges").
// Color is the ONLY place we encode state; everything else stays ink-gray.
const THEME = {
  // Virtual Machine
  Running: 'green',
  Stopped: 'gray',
  Pending: 'orange',
  Paused: 'blue',
  Failed: 'red',
  Terminated: 'gray',
  // Snapshot / Image
  Available: 'green',
  Active: 'green',
}

export function statusTheme(status) {
  return THEME[status] ?? 'gray'
}
