# Phase 1 — Cross-cutting cleanup (post-implementation wireframe)

Pure-removal phase. No visible structural change; one negative-space delta.

## Atlas form (any of the five doctypes) — chrome strip

```
┌─────────────────────────────────────────────────────────────────────┐
│  ← Back  Doctype / <title-or-name>      Save (subtle)  Lifecycle ▸  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  [form body — col-lg-12, no right rail, no timeline]                │
│                                                                     │
│  ── (no "Type a reply / comment ×" placeholder leaking below) ──    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Browser tab title: `<vm.title> — Atlas` (was `<vm.title> - <uuid> | Atlas`).

## Required-field marker

```
  Title *           [____________________]
       ▲
       └── Framework asterisk now visible (was hidden).
           No second asterisk above the column (orphan never rendered
           on this Frappe build; the suppression code is gone).
```

## What changed

1. `atlas_form_overrides.js`
   - `frappe.atlas.strip_desk_chrome` selector list extended with
     `.comment-input-wrapper`, `.comment-input-placeholder`,
     `.comment-box-container`.
   - Added `frappe.atlas.set_window_title(frm)` — sets `document.title`
     from `frm.doc.title || frm.doc.name`.
   - Removed `suppress_orphan_asterisks` (function + caller).
   - `onload` now installs `frm.set_window_title` shadow + first
     `set_window_title` call; `refresh` calls it again on every form
     state change.
2. `atlas_desk.css`
   - Dropped the `.form-column .section-body > .reqd:not(.frappe-control)`
     `display: none` rule. The framework `*` is intended visual feedback.
