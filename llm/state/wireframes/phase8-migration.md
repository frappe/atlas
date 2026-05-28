# Phase 8 — Migration patch ordering + smoke test (post-implementation wireframe)

No UI surface. This phase reorders `patches.txt` to match the plan and
verifies a clean `bench migrate` on a legacy-shape site lands every
schema and data transformation in the right order.

## `atlas/patches.txt` — desired ordering

```
┌─────────────────────────────────────────────────────────────────────┐
│  [pre_model_sync]                                                   │
│  ─────────────────                                                  │
│  1. rename_server_to_uuid              tabServer.name  → uuid       │
│                                        FK fixups on tabTask.server  │
│                                        + tabVirtual Machine.server  │
│                                        (column rename                │
│                                         server_name → title)        │
│  2. rename_vm_description_to_title     tabVirtual Machine           │
│                                        description → title          │
│  3. rename_image_description_to_title  tabVirtual Machine Image     │
│                                        description → title          │
│  4. migrate_ssh_key_to_disk            tabServer Provider           │
│                                        ssh_private_key (Password)   │
│                                        → ssh_private_key_path       │
│                                        (Data, on-disk file 0600)    │
│                                                                     │
│  [post_model_sync]                                                  │
│  ──────────────────                                                 │
│  1. rename_archived_to_terminated      (pre-existing)               │
│  2. install_atlas_sidebar              (pre-existing)               │
│  3. rebuild_task_subjects              tabTask.subject              │
│                                        ← verb / verb-noun rule      │
│  4. migrate_workspace_to_onboarding    Workspace Atlas              │
│                                        bsc_block → onboarding       │
└─────────────────────────────────────────────────────────────────────┘
```

Every pre-migrate patch is independent (different tables), so order
within `[pre_model_sync]` is for the plan's documented order, not a
correctness requirement.

## Smoke-test verification

Ran `bench --site atlas.tests.local migrate` on a site that had
already been through every prior phase. Result: clean migrate, no
exceptions, all `after_migrate` hooks executed.

Assertions captured from `bench execute` calls:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Server identity                                                    │
│  ───────────────                                                    │
│  SELECT COUNT(*), SUM(name REGEXP '^[0-9a-f-]{36}$')                │
│    FROM tabServer                                                   │
│  →  total = 24, uuid_count = 24    (100% UUID-named)                │
│                                                                     │
│  Sample row:                                                        │
│    name  = 45bcd1c4-27f7-4acf-b10c-28742cca1497                     │
│    title = atlas-e2e-shared-1779964088                              │
│                                                                     │
│  Task subjects                                                      │
│  ──────────────                                                     │
│  Distinct (subject, script) pairs:                                  │
│    Sync Image              ← sync-image.sh                          │
│    Bootstrap Server        ← bootstrap-server.sh                    │
│    Create Virtual Machine  ← provision-vm.sh                        │
│    Start                   ← start-vm.sh                            │
│    Stop                    ← stop-vm.sh                             │
│    Terminate               ← terminate-vm.sh                        │
│    Reboot                  ← reboot-server.sh                       │
│    phase1-probe.sh         ← phase1-probe.sh   (test fixture)       │
│    noop.sh                 ← noop.sh           (test fixture)       │
│                                                                     │
│  SSH key migration                                                  │
│  ───────────────────                                                │
│  Every Server Provider row carries ssh_private_key_path; sample:    │
│    atlas-e2e-provider → /Users/aditya/.atlas/keys/atlas-e2e-provider.pem │
│  On-disk file exists with 0600 permissions.                         │
│                                                                     │
│  Workspace                                                          │
│  ─────────                                                          │
│  tabWorkspace.content LIKE '%bsc_block%'   →  has_stale = 0         │
│  tabWorkspace.content LIKE '%onboarding%'  →  has_onboarding = 1    │
└─────────────────────────────────────────────────────────────────────┘
```

## Idempotency check

Re-ran every pre- and post-migrate patch via
`bench execute atlas.patches.v1_0.<patch>.execute` on the
already-migrated site. Each invocation was a no-op (each patch carries
a "skip if already migrated" guard checking column existence /
target-shape).

## What changed in `atlas/patches.txt`

- Reordered `[pre_model_sync]` block to match plan's documented
  sequence (rename_server_to_uuid → rename_vm_* → rename_image_* →
  migrate_ssh_key_to_disk). All four patches are independent so the
  reorder is presentational — no functional change.
- `[post_model_sync]` already matched the plan post-Phase-5 (the
  legacy `backfill_task_subject` entry was replaced with
  `rebuild_task_subjects` in commit a796e52).

## What did *not* change

- `install_virtual_machine_image_sync` is still absent (no
  `Virtual Machine Image Sync` DocType exists — Phase 6 drift). The
  patch was a tracking-surface backfill for that DocType; with the
  DocType deferred, the patch has nothing to populate. Documented in
  the drift table.
- `atlas/patches/v1_0/backfill_task_subject.py` is still in the tree,
  orphan to `patches.txt`. Removing it would land an ImportError if
  the patch tracker re-evaluates the (already-applied) row. The file
  is unreferenced and harmless.
