// All data access goes through frappe-ui's useList/useDoc/useDoctype —
// standard Frappe endpoints (frappe.client.get_list / get, /api/v2/document
// for mutations), never raw fetch and never a hand-built request envelope.
// The backend permission query scopes every list to the owner, so the SPA
// passes no owner filter of its own.
import { useList, useDoc, useDoctype } from 'frappe-ui'

// One place owns the 'Virtual Machine' doctype string for mutations
// (runDocMethod / delete). Lifecycle actions and deletes go through this so
// no page hand-builds a run_doc_method payload or calls frappe.client.delete.
// runDocMethod posts to /api/v2/document/Virtual Machine/<name>/method/<method>
// with `params` as the body (no JSON.stringify); delete auto-evicts the shared
// docStore + listStore on success, so the machines list updates without a
// manual reload. runDocMethod does NOT refetch the doc — callers still reload
// the VM + its Tasks after a lifecycle action to pull the new status.
export function useMachineDoctype() {
  return useDoctype('Virtual Machine')
}

export function useMachines() {
  return useList({
    doctype: 'Virtual Machine',
    fields: [
      'name',
      'title',
      'status',
      'image',
      'ipv6_address',
      'vcpus',
      'memory_megabytes',
      'disk_gigabytes',
      'modified',
    ],
    orderBy: 'modified desc',
    pageLength: 100,
    cacheKey: 'machines',
    // Decorate each row with the placeholder fields the list view shows but the
    // backend does not store yet (OS mark, tags, …). Remove `transform` and the
    // PLACEHOLDER block below once those land as real fields.
    transform: (rows) => rows.map(decorate),
  })
}

export function useMachine(name) {
  return useDoc({
    doctype: 'Virtual Machine',
    name,
  })
  // NOTE: useDoc's own `transform` runs unreliably (it decorates a throwaway
  // copy on a network fetch and only the IndexedDB read path), so the detail
  // page decorates resource.doc with `decorate()` in a computed instead.
}

// ─── PLACEHOLDER ────────────────────────────────────────────────────────────
// Display data the standalone mockup shows that the Virtual Machine doctype
// does not store yet: OS branding, legacy/private addresses, tags, region, and
// the bench rollup (sites / version / uptime). Synthesized here — deterministic
// per machine name — so the list and overview look real now. When the backend
// grows these fields, delete this whole block and the `transform` hooks above;
// the pages already read `vm.<field>`, so they keep working unchanged.

// Region and per-size pricing are fixed for this service (one region, one size).
export const FIXED = {
  region: { name: 'Frankfurt', country: 'Germany', flag: '🇩🇪' },
  priceMo: 24,
  priceHr: 0.036,
}

// image doc-name fragment → OS name, for the list/detail subtitle.
const OS_BRAND = [
  { match: /ubuntu/i, name: 'Ubuntu' },
  { match: /debian/i, name: 'Debian' },
]

export function osBrand(image) {
  const key = String(image || '')
  const hit = OS_BRAND.find((o) => o.match.test(key))
  // Pull a "22.04" / "24.04" style version out of the image name if present.
  const version = (key.match(/\d{2}\.\d{2}|\d+/) || [''])[0]
  return { name: hit ? hit.name : key || 'Image', version }
}

// A small stable hash of the machine name, so synthesized values stay put
// across reloads instead of jumping around.
function seedOf(name) {
  let h = 0
  for (const ch of String(name || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return h
}

// A couple of demo tags, assigned deterministically; some machines get none.
const TAG_POOL = ['production', 'staging', 'client', 'trial']

export function decorate(vm) {
  if (!vm || !vm.name) return vm
  const seed = seedOf(vm.name)
  const os = osBrand(vm.image)
  const running = vm.status === 'Running'
  const tagCount = seed % 3 // 0, 1, or 2 tags
  return {
    ...vm,
    os,
    // Legacy IPv4 + private address (this service is IPv6-first; these are
    // shown as secondary/legacy on the overview).
    ipv4_address: `142.93.${10 + (seed % 200)}.${(seed * 7) % 250}`,
    private_address: `10.0.0.${10 + (seed % 80)}`,
    region: FIXED.region,
    tags: TAG_POOL.slice(0, tagCount),
    bench: {
      status: running ? 'running' : 'stopped',
      version: 'v15',
      sites: running ? 1 + (seed % 8) : 0,
      uptime: running ? '99.9%' : '—',
    },
  }
}
// ─── END PLACEHOLDER ────────────────────────────────────────────────────────

export function useMachineTasks(name) {
  return useList({
    doctype: 'Task',
    fields: ['name', 'status', 'subject', 'script', 'creation'],
    filters: { virtual_machine: name },
    orderBy: 'creation desc',
    pageLength: 10,
    cacheKey: ['machine-tasks', name],
  })
}

export function useImages() {
  return useList({
    doctype: 'Virtual Machine Image',
    fields: ['name', 'image_name', 'title', 'default_disk_gigabytes', 'is_active'],
    orderBy: 'modified desc',
    pageLength: 100,
    cacheKey: 'images',
  })
}

export function useSnapshots() {
  return useList({
    doctype: 'Virtual Machine Snapshot',
    fields: ['name', 'title', 'virtual_machine', 'status', 'size_bytes'],
    orderBy: 'creation desc',
    pageLength: 100,
    cacheKey: 'snapshots',
  })
}
