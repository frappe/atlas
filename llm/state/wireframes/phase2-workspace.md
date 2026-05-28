# Phase 2 — Workspace data migration (post-implementation wireframe)

Patch-only phase. The workspace fixture content is the source of
truth; the patch backfills any live `Atlas` Workspace whose `content`
column still carries the legacy `bsc_block` Custom HTML reference.

## Workspace `/app/atlas` — desired layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Atlas                                                                   │
├──────────────────────────────────────────────────────────────────────────┤
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Atlas Setup (Module Onboarding widget)                            │  │
│  │  ◯ Configure Server Provider     → /app/server-provider/new        │  │
│  │  ◯ Provision your first Server   → /app/server/new                 │  │
│  │  ◯ Launch your first VM          → /app/virtual-machine/new        │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  Fleet at a glance                                                       │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐                      │
│  │ Active  │  │ Running │  │ Pending │  │ Failed  │                      │
│  │ Servers │  │   VMs   │  │   VMs   │  │ Tasks   │                      │
│  │    3    │  │    7    │  │    1    │  │    0    │                      │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘                      │
│                                                                          │
│  Recent activity                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Task quick-list — last 10 by `modified desc`                      │  │
│  │  Sync Image                          Success    2m ago             │  │
│  │  Bootstrap Server                    Success    5m ago             │  │
│  │  Start                               Success    7m ago             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

No JS console error, no `Failed to get method for command bsc_block`
dialog.

## What changed

1. `atlas/patches/v1_0/migrate_workspace_to_onboarding.py` — already
   present in the tree. Reads the canonical content from
   `atlas/atlas/workspace/atlas/atlas.json`, deletes the stale
   `Workspace Custom Block` child row pointing at
   `atlas-bootstrap-checklist`, rewrites the workspace `content`, then
   force-deletes the orphan `Custom HTML Block` if it survives in DB.
2. `atlas/patches.txt` — appended the patch under `[post_model_sync]`.
3. `atlas/tests/test_workspace_patch.py` — exercises the four
   meaningful paths: stale content → canonical, stale child row → no
   row, legacy Custom HTML Block → deleted, already-canonical → no-op.

## Site-level cleanup

Ran the patch on `atlas.tests.local` (where the live workspace still
carried the stale child row pointing at `atlas-bootstrap-checklist`).
After running, `Workspace Custom Block` filtered on `parent=Atlas` is
empty. The test's `setUp` snapshot now captures empty `custom_blocks`
which the `tearDown` restores cleanly — fixing the
`LinkValidationError` cascade.
