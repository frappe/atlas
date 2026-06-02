// Small formatting helpers shared across pages.
import { dayjs } from 'frappe-ui'

// The house dayjs (frappe-ui re-export, relativeTime plugin enabled) owns the
// "x ago" wording so it tracks the library's locale/format instead of a
// bespoke ladder. Frappe stamps timestamps as "YYYY-MM-DD HH:mm:ss"; dayjs's
// customParseFormat plugin reads that directly.
export function relativeTime(value) {
  return value ? dayjs(value).fromNow() : ''
}

export function gigabytes(bytes) {
  if (!bytes) return '—'
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`
}
