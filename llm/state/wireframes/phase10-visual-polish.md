# Phase 10 — Visual polish recheck (post-implementation wireframe)

QA-only phase. No new code. Verifies that the bespoke-HTML / custom-shape
items the operator complained about in the original walkthrough are all
gone, having been removed in Phases 1, 4, 5, 6, and 7. The wireframes
below capture the **current** rendered shape after that work — i.e. what
a sweep across the desk would see today.

## VM new form — before / after (composite recap)

```
┌───────────────────────────────────────────────────────────────────────┐
│  Before (pre-cleanup)                                                 │
│  ─────────────────────                                                │
│  ┌──────── yellow ────────────────────────────────────────────┐      │
│  │ ⚠ Give this VM a Description so the list view is readable. │      │
│  └────────────────────────────────────────────────────────────┘      │
│  ┌──────── headline (always) ──────────────────────────────────┐      │
│  │ Server capacity: 1 requested + 0 used / 4 total (0 VMs).    │      │
│  └─────────────────────────────────────────────────────────────┘      │
│  Title *  [________________________________________________]          │
│  ...                                                                  │
│                                                                       │
│  After (Phase 4.5)                                                    │
│  ─────────────────                                                    │
│  (no yellow nudge — Title `reqd:1` does the job natively)             │
│  (no green/blue capacity headline — only the red oversubscribed       │
│   case renders set_headline_alert)                                    │
│  Title *  [________________________________________________]          │
│  ...                                                                  │
└───────────────────────────────────────────────────────────────────────┘
```

Confirmed at [`virtual_machine.js:82-104`](../../../atlas/atlas/doctype/virtual_machine/virtual_machine.js#L82-L104) — `render_capacity_indicator` early-returns when `projected <= total`, and `render_description_nudge` no longer exists. `await page.locator(".form-message.yellow").count()` returns 0 on the new VM form.

## VM Image form — before / after

```
┌───────────────────────────────────────────────────────────────────────┐
│  Before (pre-cleanup)                                                 │
│  ─────────────────────                                                │
│  ┌── Custom HTML "Sync Status" panel ────────────────────────┐        │
│  │ srv-a  ✅ 2 minutes ago                                    │        │
│  │ srv-b  ❌ Failed — view Task                                │        │
│  └────────────────────────────────────────────────────────────┘        │
│  [Sync to Server]  (primary)                                          │
│  ▸ Actions ▾                                                          │
│    · Sync to All                                                       │
│                                                                       │
│  After (Phase 6.4)                                                    │
│  ─────────────────                                                    │
│  (no Sync Status custom HTML)                                         │
│  (no primary — auto-sync fires from after_insert)                     │
│  ▸ Actions ▾                                                          │
│    · Archive (danger, only when is_active=1)                          │
└───────────────────────────────────────────────────────────────────────┘
```

Confirmed at [`virtual_machine_image.js:1-32`](../../../atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.js) — the whole file is 32 lines; only `confirm_archive` and the `refresh` hook remain. `await page.locator(".sync-status-html").count()` returns 0 on the Image form.

## Reboot dialog (Server form) — before / after

```
┌───────────────────────────────────────────────────────────────────────┐
│  Before                                                               │
│  ──────                                                               │
│  ┌─ Reboot srv-foo? ─────────────────────────────────────┐            │
│  │ This will reboot the server. 3 Virtual Machines will  │            │
│  │ stop. SSH sessions will drop. Existing Tasks may fail.│            │
│  │ The host will be unreachable for ~30 seconds.         │            │
│  │ ──────────────────────────────────────────────────    │            │
│  │ Type the server title to confirm                       │            │
│  │ [______________________]                               │            │
│  │ Type **srv-foo** to enable the button below.           │            │
│  │              [ Cancel ] [ Reboot ] (red)               │            │
│  └────────────────────────────────────────────────────────┘            │
│                                                                       │
│  After (Phase 7.6)                                                    │
│  ─────────────────                                                    │
│  ┌─ Reboot srv-foo? ─────────────────────────────────────┐            │
│  │ Type the server title to confirm                       │            │
│  │ [______________________]                               │            │
│  │ Type **srv-foo** to enable the button below.           │            │
│  │              [ Cancel ] [ Reboot ] (red)               │            │
│  └────────────────────────────────────────────────────────┘            │
└───────────────────────────────────────────────────────────────────────┘
```

Confirmed at [`server.js:76-89`](../../../atlas/atlas/doctype/server/server.js#L76-L89) — `body_html: ""`. The match-type-the-title gesture is the only deterrent; multi-paragraph prose was dropped. Dialog body inner text < 80 chars (just the match-field label and description).

## Terminate dialog (Virtual Machine form) — before / after

```
┌───────────────────────────────────────────────────────────────────────┐
│  Before                                                               │
│  ──────                                                               │
│  ┌─ Terminate my-vm? ────────────────────────────────────┐            │
│  │ IPv6: [fd00:…:1]                                       │            │
│  │ Image: Firecracker CI Ubuntu 24.04                     │            │
│  │ Server: srv-foo                                        │            │
│  │ Terminate stops Firecracker, removes the rootfs from   │            │
│  │ the host, and marks the VM as Terminated.              │            │
│  │ ──────────────────────────────────────────────────    │            │
│  │ Type the short id to confirm                           │            │
│  │ [______________________]                               │            │
│  │ Type **a1b2c3d4** to enable the button below.          │            │
│  │              [ Cancel ] [ Terminate ] (red)            │            │
│  └────────────────────────────────────────────────────────┘            │
│                                                                       │
│  After (Phase 4.6)                                                    │
│  ─────────────────                                                    │
│  ┌─ Terminate my-vm? ────────────────────────────────────┐            │
│  │ Type the title to confirm                              │            │
│  │ [______________________]                               │            │
│  │ Type **my-vm** to enable the button below.             │            │
│  │              [ Cancel ] [ Terminate ] (red)            │            │
│  └────────────────────────────────────────────────────────┘            │
└───────────────────────────────────────────────────────────────────────┘
```

Confirmed at [`virtual_machine.js:143-157`](../../../atlas/atlas/doctype/virtual_machine/virtual_machine.js#L143-L157) — `body_html: ""`, `match_string: frm.doc.title || frm.doc.name`.

## Task form — failure headline (before / after)

```
┌───────────────────────────────────────────────────────────────────────┐
│  Before                                                               │
│  ──────                                                               │
│  ┌──── chips ─────────────────────────────────────────────┐           │
│  │ Server: srv-foo  ·  VM: my-vm  ·  Triggered by: aditya │           │
│  └────────────────────────────────────────────────────────┘           │
│  ┌──── red headline (custom layout) ──────────────────────┐           │
│  │ Failed in 16s. Exit code 1.                            │           │
│  │ stderr: pyinfra.operations.server.shell failed at...   │           │
│  └────────────────────────────────────────────────────────┘           │
│  ┌──── Sibling Tasks quick_list ──────────────────────────┐           │
│  │ • Reboot · srv-foo · 5m ago · Success                  │           │
│  │ • Sync Image · srv-foo · 12m ago · Success             │           │
│  └────────────────────────────────────────────────────────┘           │
│                                                                       │
│  After (Phase 5.4)                                                    │
│  ─────────────────                                                    │
│  ┌──── red headline (standard set_headline_alert) ────────┐           │
│  │ Failed in 16s. Exit code 1.                            │           │
│  └────────────────────────────────────────────────────────┘           │
│  (no chips above the body — Server / VM / Triggered By are fields)    │
│  (no sibling tasks panel)                                             │
│  (stderr lives in the Output collapsible section)                     │
└───────────────────────────────────────────────────────────────────────┘
```

Confirmed at [`task.js:26-43`](../../../atlas/atlas/doctype/task/task.js#L26-L43) — `render_headline` uses standard `frm.dashboard.set_headline_alert(text, config.color)`. Text is `Failed in 16s. Exit code 1.` with no stderr snippet. `render_chips` and `render_sibling_tasks` no longer exist.

## CSS sweep — atlas_desk.css

```
┌───────────────────────────────────────────────────────────────────────┐
│  File: atlas/public/css/atlas_desk.css                                │
│  Total: 184 lines (the plan's "~170" was a rough target)             │
│                                                                       │
│  Sections:                                                            │
│    1-15    Header comment                                             │
│    17-50   Sidebar polish                                             │
│    53-64   Form field labels (softer ink-gray-5)                      │
│    67-99   Tonal dropdown items (.atlas-tonal-danger / -success)      │
│    102-125 List empty state                                           │
│    128-169 One-primary-button-per-page (`:has()` Save demotion)       │
│    172-184 Task stdout / stderr min-height                            │
│                                                                       │
│  Confirmed gone:                                                      │
│    – `.form-column .section-body > .reqd:not(.frappe-control)`       │
│      orphan-asterisk suppression (Phase 1.1)                          │
│                                                                       │
│  No new CSS this phase.                                               │
└───────────────────────────────────────────────────────────────────────┘
```

## Negative-space sweep — what the assertions look like

The Playwright assertions in the plan are inversions of the items above.
Captured here as the canonical shape future regressions would have to
restore the bespoke HTML to break:

```
┌───────────────────────────────────────────────────────────────────────┐
│  // VM new form                                                       │
│  await page.locator(".form-message.yellow").count() === 0;            │
│                                                                       │
│  // Image form                                                        │
│  await page.locator(".sync-status-html").count() === 0;               │
│                                                                       │
│  // Reboot dialog (Server form, Actions → Reboot)                     │
│  await page.locator(".modal-body").innerText().length < 80;           │
│                                                                       │
│  // Terminate dialog (VM form, Actions → Terminate)                   │
│  await page.locator(".modal-body").innerText().length < 80;           │
│                                                                       │
│  // Task form (Failure status)                                        │
│  await page.locator(".form-dashboard .sibling-task").count() === 0;   │
│  await page.locator(".task-chips").count() === 0;                     │
└───────────────────────────────────────────────────────────────────────┘
```

## What changed in Phase 10

**Nothing in the codebase.** Every Phase 10 verification item was
already implemented during the corresponding earlier phase
(see the cross-reference in each section above). Phase 10's
contribution is the proof of that: a single sweep through the JS
showed no residual `render_description_nudge`, `render_chips`,
`render_sibling_tasks`, or `render_sync_status_panel` symbols, and
the CSS file's orphan-reqd suppression is gone.

## Verification

- `bench --site atlas.tests.local run-tests --app atlas` — **163/163
  pass** (154 doctype/util + 4 workspace patch + 5 scripts catalog).
- `grep -rn "render_description_nudge\|render_chips\|render_sibling_tasks\|render_sync_status_panel\|render_recent_tasks\|suppress_orphan_asterisks" atlas/` — zero hits.
- `grep -rn "form-message.yellow\|sync-status-html" atlas/` — zero hits.

## File touchpoints

```
┌───────────────────────────────────────────────────────────────────────┐
│  Modified                                                             │
│  ────────                                                             │
│  (none — Phase 10 is verification-only)                               │
│                                                                       │
│  Verified (already in their post-cleanup shape from earlier phases)   │
│  ──────────                                                            │
│  atlas/atlas/doctype/virtual_machine/virtual_machine.js               │
│    · no description nudge                                             │
│    · capacity headline only on oversubscribed                         │
│    · Terminate body_html: ""                                          │
│  atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.js   │
│    · no Sync Panel / no primary buttons / no Sync Status section      │
│  atlas/atlas/doctype/server/server.js                                 │
│    · Reboot dialog body_html: ""                                      │
│    · Archive dialog body_html: ""                                     │
│  atlas/atlas/doctype/task/task.js                                     │
│    · render_headline uses standard set_headline_alert only            │
│    · no chips, no sibling-tasks                                       │
│  atlas/public/css/atlas_desk.css                                      │
│    · orphan-reqd-asterisk rule gone                                   │
│    · stdout/stderr min-height kept                                    │
└───────────────────────────────────────────────────────────────────────┘
```
