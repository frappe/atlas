# Phase 9 — E2E harness field renames + auto-provision wait (post-implementation wireframe)

No UI surface. This phase brings the e2e use-case suite in line with the
new schema and the Phase 4 auto-provision contract: VM rows now flip
Pending → Running via a background worker without an operator click.

## New helper: `wait_for_vm_running`

```
┌─────────────────────────────────────────────────────────────────────┐
│  atlas/tests/e2e/_tasks.py                                          │
│  ─────────────────────────                                          │
│  wait_for_vm_running(virtual_machine_name, timeout_seconds=60)     │
│                                                                     │
│  Poll Virtual Machine.status:                                       │
│    Pending  → keep waiting                                          │
│    Running  → return doc                                            │
│    Failed   → raise AssertionError                                  │
│    timeout  → raise AssertionError                                  │
│                                                                     │
│  Re-exported through _shared.py.                                    │
└─────────────────────────────────────────────────────────────────────┘
```

## Use-case rewrites — VM lifecycle paths

```
┌─────────────────────────────────────────────────────────────────────┐
│  Before Phase 9                          After Phase 9              │
│  ──────────────                          ──────────────             │
│  vm = frappe.get_doc({                   vm = frappe.get_doc({      │
│    "description": "...",                   "title": "...",          │
│    ...                                     ...                      │
│  }).insert()                             }).insert()                │
│  vm.provision()       # explicit          frappe.db.commit()        │
│  assert vm.status == "Running"           wait_for_vm_running(...)   │
│                                          vm.reload()                │
│                                          assert vm.status == "Running"│
└─────────────────────────────────────────────────────────────────────┘
```

Applied in:
- `use_cases/virtual_machine_provisioning.py::_check_provision_happy_path`
- `use_cases/virtual_machine_lifecycle.py::_check_full_lifecycle`
- `use_cases/desk_buttons.py::_check_virtual_machine_buttons`

## Negative-path restructure: image-missing branch

```
┌─────────────────────────────────────────────────────────────────────┐
│  Before                                  After                      │
│  ──────                                  ─────                      │
│  insert VM           (Pending)           move rootfs aside          │
│  move rootfs aside                       insert VM    (auto-fires)  │
│  vm.provision()      → raises            poll VM.status → Failed    │
│  restore rootfs                          restore rootfs             │
│                                          (optional) retry           │
│                                          vm.provision() → Running   │
│                                                                     │
│  Why: auto_provision worker fires immediately, so we can't insert  │
│  a VM and then move the rootfs underneath it — the worker would    │
│  race the move. Setting the trap first makes the negative path     │
│  deterministic.                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Dropped: Pending state guards from e2e

```
┌─────────────────────────────────────────────────────────────────────┐
│  Dropped from virtual_machine_lifecycle.py::_check_pending_guards  │
│  Dropped from desk_buttons.py::_check_virtual_machine_buttons      │
│                                                                     │
│    with expect_validation_error("cannot start"): vm.start()         │
│    with expect_validation_error("cannot stop"):  vm.stop()          │
│    with expect_validation_error("cannot restart"): vm.restart()     │
│                                                                     │
│  Why: auto-provision races the assertions to Running. The guards   │
│  are still enforced by the controller (`if self.status != ...`);   │
│  unit tests in test_virtual_machine_lifecycle.py exercise the      │
│  Pending branch with synthetic VM docs.                            │
└─────────────────────────────────────────────────────────────────────┘
```

## New host-side probe: SSH-key path

```
┌─────────────────────────────────────────────────────────────────────┐
│  use_cases/virtual_machine_provisioning.py::_assert_provider_ssh_key_path│
│                                                                     │
│  Called once per happy-path run, immediately before the guest      │
│  SSH probe (phase5-guest-identity.sh):                              │
│                                                                     │
│  1. Lookup: Server.provider → Server Provider.ssh_private_key_path │
│  2. Assert: path is a regular file                                  │
│  3. Assert: file mode is 0600 (or 0400, equally safe)              │
│                                                                     │
│  Why: surfaces a misconfigured host (key missing, wrong perms)     │
│  as a clean Python AssertionError instead of a noisy SSH timeout   │
│  later in phase5-guest-identity.sh. The Phase 3 contract is that   │
│  Atlas reads the key from disk at SSH-connect time; this probe     │
│  fails fast when that contract is violated.                        │
└─────────────────────────────────────────────────────────────────────┘
```

## Caught during the e2e run

```
┌─────────────────────────────────────────────────────────────────────┐
│  `_check_ipv6_exhaustion` was inserting 14 synthetic VMs in a loop  │
│  without a `title`. Phase 4 made `title` reqd on Virtual Machine —  │
│  the test predated that and was silently broken in the new schema.  │
│  Fix: pass `title=f"ipv6-exhaust-{i}"` per row. Verified by         │
│  re-running vm-provisioning against the shared droplet.             │
└─────────────────────────────────────────────────────────────────────┘
```

## File touchpoints

```
┌─────────────────────────────────────────────────────────────────────┐
│  Modified                                                           │
│  ────────                                                           │
│  atlas/tests/e2e/_tasks.py                                          │
│    + wait_for_vm_running()                                          │
│  atlas/tests/e2e/_shared.py                                         │
│    + re-export wait_for_vm_running                                  │
│  atlas/tests/e2e/use_cases/desk_buttons.py                          │
│    description → title; insert+wait instead of insert+provision    │
│  atlas/tests/e2e/use_cases/virtual_machine_provisioning.py          │
│    description → title; insert+wait; rootfs-trap restructure;      │
│    + _assert_provider_ssh_key_path()                                │
│  atlas/tests/e2e/use_cases/virtual_machine_lifecycle.py             │
│    description → title; drop Pending-guards; insert+wait            │
│                                                                     │
│  Unchanged                                                          │
│  ─────────                                                          │
│  atlas/tests/e2e/_config.py        (already had get_ssh_private_key_path)│
│  atlas/tests/e2e/_droplets.py      (already used title/path fields) │
│  atlas/tests/e2e/_image.py         (only references image_name, the autoname)│
│  atlas/tests/e2e/use_cases/image_sync.py    (Phase 6 deferred the Sync DocType)│
│  atlas/tests/e2e/use_cases/server_provisioning.py  (already used title)│
│  atlas/tests/e2e/use_cases/run_task.py     (no VM lifecycle touches)│
│  atlas/tests/e2e/use_cases/ssh_primitive.py (already used title)    │
│  atlas/tests/e2e/scripts/phase5-guest-identity.sh   (guest probe;  │
│    the new host-side check is a sibling, not an extension of this) │
└─────────────────────────────────────────────────────────────────────┘
```
