# Spec drift

Per-phase list of places the implementation plan deviates from the spec or
makes a choice the spec didn't pin. Each entry: the spec location, the
implementation choice, and a "resolve by" suggestion. Walk this with the
operator at the end of phase 8.

---

## Phase 1

### 1.1 Task `name` is `hash` not "UUID"

- Spec: [`spec/02-doctypes.md:281`](../spec/02-doctypes.md#task) says
  `name = (autoname hash)` with the note "UUID."
- Implementation: Frappe's `autoname = "hash"` produces a 10-char random
  hex string, not a UUID. We use it as-is.
- Resolve: update the spec to say "10-char hash" — they're interchangeable
  for audit rows.
- **Resolved (phase 8): spec updated.** `02-doctypes.md` Task row now reads
  "10-char random hex (Frappe `autoname = "hash"`)."

### 1.2 `run_task(server, ...)` vs `run_task(connection, ...)`

- Spec: [`spec/04-tasks.md:28`](../spec/04-tasks.md) says `run_task(server,
  script, variables, virtual_machine=None)`.
- Implementation: `run_task(connection={...}, ...)` is the low-level
  primitive; `run_task_on_server(server, ...)` (phase 3) is the convenience
  wrapper that builds the dict from a Server doc.
- Resolve: keep both. Document the wrapper in `04-tasks.md`.
- **Resolved (phase 8): spec updated.** `04-tasks.md` "How it runs"
  documents both `run_task(connection, ...)` and `run_task_on_server(...)`.

### 1.3 No reconciler for orphaned `Running` Task rows

- Spec doesn't address this case directly.
- Implementation: if the worker dies between "set Running" and the final
  update, the row stays Running. We don't recover.
- Resolve: log this in `09-roadmap.md` as "next-iteration: stuck-task
  reaper." Acceptable for iteration 1.
- **Resolved (phase 8): roadmap updated.** `spec/09-roadmap.md` now has a
  "Stuck-task reaper" concrete-next-steps entry.

---

## Phase 2

No drift. Spec doesn't describe the DO client at this level.
**Resolved (phase 8): nothing to do.**

---

## Phase 3

### 3.1 `Server.provider_resource_id` set on insert, not after

- Spec: [`spec/02-doctypes.md:68`](../spec/02-doctypes.md#server) implies
  this is set later.
- Implementation: we have the droplet ID before the Server insert; we set
  it then.
- Resolve: no change needed. Spec is ambiguous; we picked the obvious
  order.
- **Resolved (phase 8): no change.** Spec is consistent with impl.

### 3.2 `Server.region` and `Server.size` materialized on the row

- Spec: [`spec/02-doctypes.md:72`](../spec/02-doctypes.md#server) shows
  them as Server fields. Doesn't say where they come from.
- Implementation: copied from the Provider's defaults at insert time. Form
  shows them read-only.
- Resolve: spec is consistent with our choice. No change.
- **Resolved (phase 8): no change.**

### 3.3 `Server.ipv6_address` = "host ::1" vs whatever DO gives us

- Spec: [`spec/02-doctypes.md:74`](../spec/02-doctypes.md#server)
  parenthetical reads "host's ::1 of /64."
- Implementation: we store the actual public v6 address DO assigns. In
  practice it's `::1` of the /64, but if DO ever gives us a different one,
  we record the truth.
- Resolve: amend spec parenthetical to "typically `::1` of the /64; whatever
  DO assigns."
- **Resolved (phase 8): spec updated.** Parenthetical reworded.

### 3.4 `/124` carve-out: first /124 of the /64

- Spec: [`spec/06-networking.md:18`](../spec/06-networking.md) says "only the
  first /124 is actually routable inside DO's fabric."
- Implementation: `carve_virtual_machine_range(prefix_cidr)` returns the
  first /124 of the /64. Assumed.
- Resolve: verify on a real droplet (phase 3 e2e implicitly does this). If
  DO's behavior changes, the function changes.
- **Resolved (phase 8): verified by phase 3 e2e.** No code or spec change.

### 3.5 Bootstrap helpers uploaded by `Server.bootstrap()`, not by a separate Task

- Spec: [`spec/03-bootstrapping.md:42`](../spec/03-bootstrapping.md)
  says "uploading them is the caller's job, so that we keep the contents
  of `atlas/scripts/` as the single source of truth." It also says the
  pre-copy step is "not a Task."
- Implementation: matches exactly. `Server.bootstrap()` calls
  `upload_files()` (not a Task) then `run_task_on_server()` (one Task).
- Resolve: nothing to do. Pinning this here so we don't drift later.
- **Resolved (phase 8): no change.**

---

## Phase 4

### 4.1 `GUEST_NETWORK_UNIT` upload formalized via `script_uploads.py`

- Spec: [`spec/08-images.md:38`](../spec/08-images.md) says the guest unit
  is "uploaded to the server alongside `sync-image.sh` before the script
  runs."
- Implementation: a `SCRIPT_UPLOADS` map in `script_uploads.py`. Every
  script declares its sidecar uploads.
- Resolve: amend `08-images.md` to point at this hookpoint.
- **Resolved (phase 8): spec updated.** `08-images.md` now points at
  `atlas/atlas/script_uploads.py`.

### 4.2 No concurrent-sync guard

- Spec doesn't say.
- Implementation: two concurrent syncs of the same image-on-server are a
  race. We don't guard.
- Resolve: add to `09-roadmap.md` as "Server lock doctype" follow-up.
- **Resolved (phase 8): roadmap updated.** `spec/09-roadmap.md` now has a
  "Server lock doctype" entry.

### 4.3 ext4 size assertion in e2e

- Spec doesn't say what the resulting ext4 should look like beyond "of
  `default_disk_gigabytes`."
- Implementation: e2e asserts ext4 file size is within 5% of nominal.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

---

## Phase 5

### 5.1 VM `name` set in `before_insert` with `uuid.uuid4()`, not `autoname`

- Spec: [`spec/02-doctypes.md:146`](../spec/02-doctypes.md#virtual-machine)
  says `autoname` on insert.
- Implementation: Frappe's `autoname` doesn't produce UUIDs out of the box;
  `before_insert` is the standard way. Functionally equivalent.
- Resolve: amend the spec hint.
- **Resolved (phase 8): spec updated.** VM `name` row now reads "Set in
  `before_insert` via `uuid.uuid4()`."

### 5.2 `last_started` set on Provision (not just on Start)

- Spec: [`spec/05-virtual-machine-lifecycle.md:74`](../spec/05-virtual-machine-lifecycle.md)
  says Provision ends with `status = Running`, `last_started = now()`.
- Implementation: matches exactly. Pinning so phase 6's `start()` doesn't
  forget to update it.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

### 5.3 IPv6 allocator: skip `::0` and `::1`

- Spec: [`spec/06-networking.md:42`](../spec/06-networking.md) says `::1` is
  the host, addresses start at `::2`. Doesn't mention `::0`.
- Implementation: `ipaddress.IPv6Network.hosts()` already excludes `::0`
  for non-/127 subnets, so the explicit `index < 2` skip only excludes
  `::1`. Test pins behavior.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

### 5.4 Provision requires image already on server (does not auto-sync)

- Spec: [`spec/05-virtual-machine-lifecycle.md:71`](../spec/05-virtual-machine-lifecycle.md)
  says "Ensure the image is on the server. If not, run sync-image.sh (this
  is its own Task; provisioning waits on it)."
- Implementation (resolved 2026-05-25 with operator): **Provision fails
  fast if the image is absent.** It does not enqueue or wait for a sync.
  The operator runs **Sync to Server** explicitly (a multi-minute action),
  then Provision (which becomes fast and predictable).
- Resolve: amend [`spec/05-virtual-machine-lifecycle.md:71`](../spec/05-virtual-machine-lifecycle.md)
  to say "Verify the image is on the server. If not, fail with a clear
  error pointing the operator at Sync to Server."
- **Resolved (phase 8): spec updated.** Step 2 of "Provision" rewritten;
  trailing paragraph noting "one Task per VM creation" updated accordingly.

---

## Phase 6

### 6.1 `delete-vm.sh` Python wrapper called `delete_vm`, not `delete`

- Spec: [`spec/02-doctypes.md:170`](../spec/02-doctypes.md#virtual-machine)
  says button label "Delete."
- Implementation: button label is "Delete"; the Python method is
  `delete_vm` to avoid colliding with `frappe.model.document.Document.delete`.
- Resolve: nothing. Button label is what the operator sees.
- **Resolved (phase 8): no change.**

### 6.2 Status change to `Archived` happens in Python (not in the script)

- Spec: [`spec/05-virtual-machine-lifecycle.md:111`](../spec/05-virtual-machine-lifecycle.md)
  says "Then Python sets `status = Archived`."
- Implementation: matches. Pinning.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

### 6.3 Failed `Delete` does not archive

- Spec doesn't specify.
- Implementation: status updates only on successful Task. Operator clicks
  Delete again (idempotent).
- Resolve: amend spec to clarify.
- **Resolved (phase 8): spec updated.** `spec/05-virtual-machine-lifecycle.md`
  "Delete" section now spells out the retry-on-failure behavior.

### 6.4 `restart()` returns a dict, not a tuple

- Plan: [`phase-6-vm-lifecycle.md:90`](./phase-6-vm-lifecycle.md) shows
  `restart() -> tuple[str, str]`.
- Implementation: returns `{"stop_task": str|None, "start_task": str}`. A
  dict survives the JSON round-trip through `frm.call` (tuples don't), keeps
  the two task names individually addressable, and lets `stop_task` be
  `None` when starting from `Stopped`.
- Resolve: nothing.
- **Resolved (phase 8): no change.** Plan paragraph is historical.

### 6.5 `restart()` guards against illegal source states

- Plan doesn't explicitly say restart from Pending/Failed/Archived raises.
- Implementation: restart requires status `Running` or `Stopped`. Anything
  else raises ValidationError. JS button is already hidden in those states;
  the server-side check is defense in depth.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

### 6.6 Test isolation: cleanup of `Virtual Machine` rows in `setUp`

- Spec/plan: doesn't say.
- Implementation: both `test_virtual_machine.py` and
  `test_virtual_machine_lifecycle.py` delete all existing `Virtual Machine`
  rows in `setUp` so the shared `vm-test-server`'s /124 doesn't exhaust
  across test runs. Frappe's `IntegrationTestCase` does not roll back
  inserts between tests.
- Resolve: acceptable for unit tests. Watch for cross-test pollution if more
  test classes are added that touch VMs.
- **Resolved (phase 8): no change.** Reviewed in phase 8; convention is
  documented inline in the test files.

---

## Phase 7

### 7.1 `reboot-server.sh` added as a real script

- Spec: [`spec/02-doctypes.md:96`](../spec/02-doctypes.md#server) says
  "Reboot — `systemctl reboot` over SSH."
- Implementation: a one-line shell script under `scripts/reboot-server.sh`,
  invoked via the standard Task path. Keeps everything uniform.
- Resolve: nothing.
- **Resolved (phase 8): spec updated.** Reboot button bullet in
  `02-doctypes.md` now points at `scripts/reboot-server.sh`.

### 7.2 `scripts_catalog.allowed_scripts()` enumerates the directory

- Spec: [`spec/04-tasks.md:163`](../spec/04-tasks.md) says the dialog has "a
  picker over the scripts directory."
- Implementation: a Python function that lists `.sh` files at
  `scripts/*.sh` (not `scripts/guest/`, not `scripts/systemd/`). Whitelist.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

### 7.3 Reboot Task ends in Failure (SSH drops)

- Spec doesn't address.
- Implementation: expected outcome. Operator-visible Failure with the
  understood meaning "the server is rebooting." E2E asserts the server
  comes back.
- Resolve: amend spec to call this out.
- **Resolved (phase 8): spec updated.** Reboot bullet now states Failure
  or Success are both normal; operators watch for SSH to come back.

### 7.4 Reboot Task may end in Success when SSH proxies the reboot fast enough

- Spec / plan: assumes Failure.
- Implementation: in practice the SSH command sometimes returns 0 because
  `systemctl reboot` exits before the connection is torn down. We accept
  either Success or Failure in the e2e and confirm reboot by polling SSH.
- Resolve: amend the plan paragraph in
  [`phase-7-run-task-and-polish.md`](./phase-7-run-task-and-polish.md) to
  acknowledge either outcome.
- **Resolved (phase 8): folded into 7.3.** Spec bullet (Failure-or-Success)
  is the authoritative statement. Plan paragraph left as-is (historical).

### 7.5 `vm-network-up.sh` self-recreates the `atlas` nftable + sysctls

- Spec: [`spec/06-networking.md`](../spec/06-networking.md) doesn't pin
  where the nftables scaffold lives across reboots.
- Implementation: the bootstrap script's `nft add table inet atlas` is
  process-time only; Ubuntu does not persist nftables tables across a
  host reboot by default. Without persistence, every
  `firecracker-vm@.service` restart after a reboot would fail in
  `vm-network-up.sh`'s `nft add rule ...`. Fix is in
  [`vm-network-up.sh`](../scripts/vm-network-up.sh): create the table +
  chain idempotently at unit-start time, and re-apply the IPv6 forwarding
  / proxy-ndp sysctls defensively. Each VM unit becomes self-sufficient
  on cold boot.
- Resolve: amend [`spec/06-networking.md`](../spec/06-networking.md) to
  note the table is recreated by `vm-network-up.sh` at unit-start, not by
  a persistent `/etc/nftables.conf`.
- **Resolved (phase 8): spec updated.** Host-side configuration section in
  `06-networking.md` now describes the unit-start recreate path.

### 7.6 Reboot e2e leaves the prior phase 6's VM rows stranded in `Running`

- Plan: e2e order is phase 6 (which deletes its VM) → phase 7 (reboot).
- Implementation: on a shared reused server across multiple runs, if a
  phase 6 run crashed mid-flight it leaves a VM row in `Running`. After a
  phase 7 reboot, those stale units don't auto-recover, but `is-active`
  returns 3, which is fine for the next phase 6 run. Added
  `_inspect.archive_all_vms()` as the operator escape hatch. The proper
  cleanup is the "stuck-task / orphaned-VM reaper" item already on
  [`spec/09-roadmap.md`](../spec/09-roadmap.md).
- Resolve: roadmap item; no fix this iteration.
- **Resolved (phase 8): roadmap.** Covered by the "Stuck-task reaper"
  entry added for drift 1.3.

---

## Phase 8

No new drift introduced. Phase 8 is permissions + docs.

### 8.1 Permission block on every DocType is the explicit 12-field form

- Plan: [`phase-8-permissions-and-docs.md:42`](./phase-8-permissions-and-docs.md)
  lists `read/write/create/delete` plus `submit/cancel/amend = 0` and
  `report/export/import/share`.
- Implementation: matches; all five JSONs now carry the full block
  (`amend/cancel/create/delete/export/import/read/report/role/share/submit/write`).
  Task keeps `delete: 0`; everything else `delete: 1`. Frappe defaults the
  missing keys to 0, but spelling them out makes the contract grep-able.
- **Resolved (phase 8): spec wins (no spec change needed).**

### 8.2 `run_all` skips phases 1, 2, 3 (not "every phase")

- Plan: [`phase-8-permissions-and-docs.md:70`](./phase-8-permissions-and-docs.md)
  shows `run_all` as `phase-3 ... phase-7`.
- Implementation: `atlas.tests.e2e.run_all` runs phases 4–7 against a
  shared bootstrapped server. Phases 2 and 3 are not orchestrated — phase
  2 is a pure DO-client smoke test (no Server doc, no SSH) and phase 3 is
  the fresh-provision flow whose whole point is to throw away the droplet.
  Folding either into `run_all` would either dilute their contracts or
  force the shared droplet to be torn down and rebuilt mid-run, which
  defeats the cost-saving the orchestrator exists for.
- **Resolved (phase 8): README explains both paths.** The top-level README
  documents `run_all` for the 4–7 path and `phase_2.run`/`phase_3.run` for
  the dedicated-droplet smoke tests.

### 8.3 Permissions tests use `frappe.has_permission`, not `get_doc` round-trips

- Plan: [`phase-8-permissions-and-docs.md:163`](./phase-8-permissions-and-docs.md)
  shows `frappe.get_doc("Server Provider", ...)` raising `PermissionError`,
  and `task.delete()` raising for System Manager.
- Implementation: `frappe.get_doc` and `frappe.delete_doc` only enforce
  permissions for the *bound* user; the test runs as `Administrator`,
  which bypasses checks. `has_permission(doctype, action, doc=...)` is the
  documented public probe and is what `frappe.client` uses internally.
  Tests assert that.
- **Resolved (phase 8): no spec/plan change.** Documented here so the next
  reviewer understands the deviation.

---

## E2E reliability plan

Drift introduced while implementing [e2e-reliability.md](./e2e-reliability.md).

### E1. Phase 4 image fixture moved to `_shared.py` and refreshes stale rows

- Plan: [`e2e-reliability.md`](./e2e-reliability.md) fix 3 specifies the
  v1.12 URLs + checksums and the `_ensure_image` refresh.
- Implementation: `DEFAULT_IMAGE` lives in
  [`atlas/tests/e2e/_shared.py`](../atlas/tests/e2e/_shared.py); phases
  4/5/6 import it. `_ensure_image` calls `doc.update(DEFAULT_IMAGE)` on
  existing rows so a stale fixture cannot silently win.
- Resolve: nothing.
- **Resolved (phase 8): no change.**

### E2. `ensure_bootstrapped_server` is the shared entry-point for phases 4/5/6

- Plan: fix 1 calls for a `(server_doc, do_client, created_now)` helper.
- Implementation: matches. Phases 4/5/6 now share this; phase 3 still owns
  the throwaway-provision path because that's the thing it tests.
- Resolve: amend [`spec/00-overview.md`] entry for "no auto retries" to
  also note "no auto-reuse outside e2e" — the reuse policy is e2e-only.
- **Resolved (phase 8): plan updated.** Note added to
  [`plan/00-overview.md`](./00-overview.md) "Non-goals across all phases."
  (The drift entry pointed at `spec/00-overview.md`, which doesn't exist;
  the analogous landing spot is the plan overview.)

### E3. `derive_tap` off-by-one — was 16 chars, must be ≤15 (IFNAMSIZ)

- Spec: not explicitly addressed; comment claimed "Length 16, IFNAMSIZ-safe."
- Implementation: this was a real bug. Linux `IFNAMSIZ` is 16 *bytes*
  including the null terminator, so the usable interface-name length is
  15. Firecracker's TAP-open returned `InvalidIfname` when handed a 16-char
  name. Changed to `atlas-` + 9 hex (= 15 chars). Adjusted
  [`test_networking.py`](../atlas/tests/test_networking.py) and
  [`test_virtual_machine.py`](../atlas/atlas/doctype/virtual_machine/test_virtual_machine.py).
- Resolve: spec doesn't pin this; nothing to update. Note for future:
  reducing to 9 hex chars still gives 16^9 ≈ 6.9e10 distinct taps per
  collision-class, more than enough.
- **Resolved (phase 8): spec updated.** Both
  [`spec/05-virtual-machine-lifecycle.md`](../spec/05-virtual-machine-lifecycle.md)
  and [`spec/06-networking.md`](../spec/06-networking.md) now spell out
  the 6+9=15 formula and the IFNAMSIZ-byte caveat.

### E4. Bootstrap chmods uploaded helper scripts

- Spec: [`spec/03-bootstrapping.md`](../spec/03-bootstrapping.md) describes
  helper scripts uploaded by the caller. Permissions not pinned.
- Implementation: `scp` preserves source permissions, and Python doesn't
  enforce +x on `.sh` checked into the repo. Without +x, systemd's
  `ExecStartPost=/var/lib/atlas/bin/vm-network-up.sh` failed with
  `status=203/EXEC`, killing the Firecracker process. Added
  `sudo chmod 0755 /var/lib/atlas/bin/*.sh` to
  [`bootstrap-server.sh`](../scripts/bootstrap-server.sh) so the bit is
  guaranteed regardless of how the local copy was checked out.
- Resolve: nothing; the bootstrap script is the right place to enforce
  this invariant.
- **Resolved (phase 8): no change.**

### E5. `teardown_all` lists by Server-name prefix, not by `atlas-e2e` tag

- Plan: fix 1 says `teardown_all` "lists `atlas-e2e`-tagged droplets."
- Implementation: droplets created by `ensure_bootstrapped_server` go
  through phase 3's `provision_server`, which tags them `atlas` (shared
  with production droplets). We can't filter the whole `atlas` tag safely.
  Instead we union (a) tag = `atlas-e2e` (older `create_test_droplet`
  path) with (b) Server rows named `atlas-e2e-*` with a
  `provider_resource_id`. Same operator UX: doctl commands printed, never
  auto-deleted.
- Resolve: amend [`e2e-reliability.md`](./e2e-reliability.md) fix 1
  bullet 5 to describe the union, OR change `provision_server` to take a
  `tags=[...]` kwarg and have `ensure_bootstrapped_server` pass
  `atlas-e2e`. Latter is cleaner but out of this iteration's scope.
- **Resolved (phase 8): punted to next iteration.** Code is right;
  `e2e-reliability.md` is historical (the union is documented here, in
  drift.md, which is the contract for this iteration).

### E6. Fix 6's per-script poll table only applies to async sync-image

- Plan: fix 6 lists per-script `timeout`/`poll` pairs and says the 0.5s
  poll "compounds across phase 6 (four lifecycle Tasks per run)."
- Implementation: most lifecycle Tasks (`provision-vm.sh`,
  `start-vm.sh`, `stop-vm.sh`, `delete-vm.sh`, all probes) run
  *synchronously* via `run_task_on_server` — there is no poll loop.
  `wait_for_task` only matters for the one async path (sync-image via
  `Image.sync_to_server`, which enqueues a background job). Consolidated
  `wait_for_task` covers that. Provision-vm subprocess timeout tightened
  from 120s → 30s in [`virtual_machine.py`](../atlas/atlas/doctype/virtual_machine/virtual_machine.py).
- Resolve: amend the plan: drop the per-script poll table for synchronous
  scripts; keep only the sync-image entry.
- **Resolved (phase 8): drift.md is the authoritative correction.** The
  `e2e-reliability.md` plan paragraph is historical and not re-edited.

### E7. `bench execute … ensure_bootstrapped_server` fails to JSON-serialize the return

- Plan: doesn't address direct invocation.
- Implementation: the helper returns `(Document, DigitalOceanClient, bool)`,
  which `bench execute` tries to JSON-print and chokes on the client.
  Phases call this internally and discard the return, so they work fine.
  Operators wanting to smoke-test it can use a console one-liner.
- Resolve: nothing — direct invocation is not the contract.
- **Resolved (phase 8): no change.**

---

## How to use this file

When implementing a phase:

1. Re-read the relevant section here before writing code.
2. If you introduce **new** drift, add an entry to that phase's section
   immediately, before commit.
3. At phase 8, walk through with the operator and either update the spec
   or update the code. Each entry becomes either "spec was right, code
   updated" or "code was right, spec updated."

This file is not a TODO list. It is a contract: every drift must end the
iteration as either resolved or explicitly punted to the next iteration's
roadmap.

## Iteration 1 close-out

Walked in phase 8 (2026-05-26). Every entry above has a bolded
**Resolved (phase 8)** line stating which side moved (spec, plan, roadmap,
or "no change because spec and code already agree"). Items punted to the
next iteration: E5 (`teardown_all` tag refactor), and the roadmap items
("Stuck-task reaper" 1.3/7.6, "Server lock doctype" 4.2). No drift entry
remains unresolved.
