// All data access goes through frappe-ui's useList/useDoc — standard Frappe
// endpoints (frappe.client.get_list / get), never raw fetch. The backend
// permission query scopes every list to the owner, so the SPA passes no
// owner filter of its own.
import { useList, useDoc } from 'frappe-ui'

export function useMachines() {
  return useList({
    doctype: 'Virtual Machine',
    fields: ['name', 'title', 'status', 'ipv6_address', 'modified'],
    orderBy: 'modified desc',
    pageLength: 100,
    cacheKey: 'machines',
  })
}

export function useMachine(name) {
  return useDoc({
    doctype: 'Virtual Machine',
    name,
  })
}

export function useMachineTasks(name) {
  return useList({
    doctype: 'Task',
    fields: ['name', 'status', 'script', 'creation'],
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
