# Phase 11 — Spec consolidation (post-implementation wireframe)

Documentation-only phase. No code changes, no DB migrations. Goal:
purge orphan references to dropped concepts (description field, Sync
to All Servers button, Sibling Tasks panel, Recent Tasks quick_list,
MultiCheck targets picker, Provision button on Pending) from the spec
so a future reader doesn't get a contradictory picture.

## Files updated

```
┌─────────────────────────────────────────────────────────────────────┐
│  spec/05-virtual-machine-lifecycle.md                              │
│  ─────────────────────────────────                                  │
│  · "they use `description` for a human-readable label"             │
│      → "they use `title` for a human-readable label (the           │
│         framework's `title_field`)"                                 │
│  · State diagram on line 23-44:                                    │
│      OLD: "(Provision button)" between Pending and Running         │
│      NEW: "(after_insert → auto_provision worker)" — Phase 4       │
│           auto-provision contract                                   │
│                                                                     │
│  spec/08-images.md                                                  │
│  ──────────────────                                                 │
│  · § "Bumping an image" rewritten end-to-end:                      │
│      OLD: "Update kernel_url, kernel_sha256, …, click Sync to All  │
│           Servers"                                                  │
│      NEW: "Image rows are immutable after insert. Insert a new     │
│           Virtual Machine Image with a distinct image_name; the    │
│           after_insert hook fans out one sync-image.sh Task per    │
│           Active Server automatically. Archive the old row."        │
│  · Closing paragraph notes that in-place URL/SHA mutation is now   │
│    refused by `_validate_immutability`.                            │
│                                                                     │
│  spec/10-desk-ui.md                                                 │
│  ────────────────────                                               │
│  · § "Form-embedded lists reuse the workspace `quick_list` widget" │
│    retitled to "Form-embedded lists — only on the workspace now":  │
│      OLD: documents Server > Recent Tasks, Task > Sibling Tasks,   │
│           VMImage > Sync Status as quick_list-based panels         │
│      NEW: documents that all three are gone; only the workspace's  │
│           Recent activity block remains as a quick_list            │
│  · § "Confirmation helpers":                                        │
│      OLD: "Sync to All Servers uses a dedicated MultiCheck dialog" │
│      NEW: "There is no Sync-to-All operator action — image sync    │
│           is automatic on image insert"                             │
│      OLD: "match the VM's 8-char short ID"                          │
│      NEW: "match the VM `title`"                                    │
│      OLD: short-ID list of confirm_destructive uses                 │
│      NEW: explicit list (Reboot, Terminate, Delete record,         │
│           Archive); dialog body is empty by contract                │
│  · § "Page title":                                                  │
│      OLD: "duplicates the description and the autoname"            │
│      NEW: "duplicates the operator-facing label and the autoname   │
│           on DocTypes that carry a separate `title` field —        │
│           Server, VM, Image"                                       │
│  · § "Why this isn't a custom SPA":                                │
│      OLD: lists `MultiCheck` as a borrowed primitive               │
│      NEW: dropped MultiCheck; the list now reads quick_list,       │
│           button group, Module Onboarding, dashboard indicator,    │
│           doctype_js, scoped CSS                                    │
│      OLD: whitelisted methods list = preview_cost, retry,          │
│           get_scripts, sync_status, capacity_for_server, …         │
│      NEW: provider_options, credential_check, archive, retry,      │
│           sync_image, capacity_for_server, …                       │
│      OLD: "Task form's read-only/headline override that            │
│           suppresses the standard six-field top row in favor of    │
│           the dashboard headline + chips"                          │
│      NEW: "Task form's disable_save() + dashboard-headline overlay │
│           that replaces the standard read-only field-list          │
│           affordance with a status-coloured headline +             │
│           collapsible Output section" (no chips)                   │
└─────────────────────────────────────────────────────────────────────┘
```

## Verification

```
┌─────────────────────────────────────────────────────────────────────┐
│  $ grep -rn "description" spec/                  → 0 hits           │
│  $ grep -rn "server_name" spec/                  → 4 hits, all are  │
│      the worker-function kwarg in spec/03-bootstrapping.md          │
│      (`finish_provisioning(server_name=<uuid>)`), correctly         │
│      documenting the post-rename signature                          │
│  $ grep -rnE "Sync to All Servers" spec/         → 1 hit, the       │
│      negative-space reference in spec/08-images.md describing the   │
│      removed contract                                                │
│  $ grep -rn "Sibling Tasks" spec/                → 2 hits, both are │
│      "Sibling Tasks panel is gone" callouts                         │
│  $ grep -rn "Recent Tasks" spec/                 → 2 hits, all      │
│      "Recent Tasks quick_list has been removed" callouts            │
│  $ grep -rn "ssh_private_key[^_]" spec/          → 1 hit, the       │
│      legacy-column migration note in spec/07-filesystem-layout.md   │
│  $ grep -rn "bsc_block" spec/                    → 0 hits           │
│                                                                     │
│  $ bench --site atlas.tests.local run-tests --app atlas             │
│      → 154 + 4 + 5 = 163 tests pass (unchanged)                     │
└─────────────────────────────────────────────────────────────────────┘
```

All hits that remain are either:
- the post-rename API kwarg name (`server_name`), which is documenting
  the new shape, not an orphan reference; or
- explicit "X is gone / has been removed / is no longer available"
  callouts, which are negative-space documentation of dropped concepts.

The latter category is intentionally retained: a reader who arrives at
the spec carrying mental model from an earlier version of Atlas needs
to be told *what was removed*, not just shown the new shape.

## File touchpoints

```
┌─────────────────────────────────────────────────────────────────────┐
│  Modified                                                           │
│  ────────                                                           │
│  spec/05-virtual-machine-lifecycle.md  — description → title;       │
│                                          state diagram updated      │
│  spec/08-images.md                     — Bumping an image §         │
│                                          rewritten for immutability │
│  spec/10-desk-ui.md                    — quick_list § rewritten;    │
│                                          MultiCheck removed;        │
│                                          short ID → title;          │
│                                          chips reference cleaned;   │
│                                          whitelisted methods list   │
│                                          refreshed                  │
│                                                                     │
│  Unchanged (already in their post-cleanup shape from earlier        │
│  sessions per the drift file's 2026-05-28 update)                   │
│  ───────────                                                         │
│  spec/02-doctypes.md                                                │
│  spec/03-bootstrapping.md                                           │
│  spec/04-tasks.md                                                   │
│  spec/07-filesystem-layout.md                                       │
└─────────────────────────────────────────────────────────────────────┘
```
