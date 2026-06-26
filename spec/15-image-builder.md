# Image Builder

Two artifacts in Atlas are built the same way: **build a script inside a plain
guest over SSH, then snapshot the result.** The golden bench image
([08-images.md § golden bench image](./08-images.md)) and the reverse-proxy image
([12-proxy.md](./12-proxy.md)) are both produced this way. This chapter is the
**Image Builder**: the operator-facing layer that owns that bake — provision a
scratch VM, run the recipe's `build.sh` in it, snapshot it, optionally register
the snapshot — under one DocType, one button, one audit trail, and one code path.

Before this layer the two bakes lived **out of band**: the build verbs
(`bench_image.build_bench`, `proxy.build_proxy`) were near-identical duplicates
driven only from e2e test modules, with the provision→build→snapshot
orchestration hand-rolled in e2e helpers. There was no operator button, no row
recording *"this snapshot was baked from this recipe,"* and no place for a third
image type to land without a third copy of the build verb. This layer removes all
three gaps.

## The shape

Three pieces, smallest surface that removes the duplication and gives the operator
a button:

```
   Image Recipe registry (code)          Image Build (DocType, operator)
   ────────────────────────────          ───────────────────────────────
   bench  → bench/  build.sh             one row per bake run
   proxy  → proxy/  build.sh             status: Draft → Provisioning →
   (more later)                            Building → Snapshotting →
            │                              Available / Failed
            │  get_recipe(name)            │
            ▼                              ▼  after_insert → enqueue run()
   atlas.atlas.image_builder.run_build(vm, recipe)   ◄── shared seam
   upload tree · run_detached(build.sh) · finalize hook · one Task row
            │
            ▼
   Virtual Machine Snapshot  ──▶  Atlas Settings.default_bench_snapshot
   (the rollable artifact)        (bench) / proxy fleet clone source
```

What this layer is **not**: it does not replace the committed `bench/` and
`proxy/` trees or their `build.sh` scripts — those stay the source of truth for
*what gets installed* (spec taste #15). It owns the **controller-side lifecycle**:
provision, upload, run, snapshot, register, audit. A recipe just *names* an
existing committed tree.

## The recipe registry (code-defined)

[`atlas/atlas/image_recipes.py`](../atlas/atlas/image_recipes.py) is a frozen
`ImageRecipe` dataclass registry (`RECIPES`), keyed by a short recipe name. It is
**code, not a DocType** — a recipe points entirely at committed files and pinned
sizes, and its `finalize` is a callback, so a data row could only mirror it. This
is the same call the spec makes for `sizes.py SIZE_PRESETS` (the canonical source,
mirrored into JS/SPA) and the `DEFAULT_IMAGE` constants in `bootstrap.py`. Adding
an image type is a small reviewable code change beside the tree it bakes — the
same discipline the two `build.sh` files' pinned versions follow.

Each `ImageRecipe` declares: the committed `source_directory` (uploaded verbatim),
the `build_entrypoint` run over guest-SSH, the build-VM sizing
(`vcpus`/`memory_megabytes`/`disk_gigabytes`), the `snapshot_title` stamped on the
output, the `task_script` name for the audit row, top-level `exclude` entries (the
proxy's dev-only `test/` harness), a `finalize` callback, a `registers_as` Atlas
Settings field, `is_proxy`, an optional `warm_entrypoint` (the in-guest script
a **warm bake** runs before the paused capture — see *The warm bake* below; empty
means the recipe only bakes cold), and — for the bench variants — the **per-version
pins** (`frappe_branch`, `erpnext_branch`, `bench_cli_ref`, `python_version`), the
bake `build_mode` (`site`/`admin`), and the `promote_image_name` (the base-image
name a promote defaults to; see *Versioned bench variants* below). The recipes that
ship:

| Recipe | Tree | Build VM | Snapshot | Frappe / ERPNext / Python | Special |
| ------ | ---- | -------- | -------- | ------------------------- | ------- |
| `bench-v16` | `bench/` | 2 vCPU / 2 GB / 28 GB | `Golden bench v16` | `version-16` / `version-16` / `3.14` | site mode; `registers_as = default_bench_snapshot`, `warm_entrypoint = warm.sh` |
| `bench-v15` | `bench/` | 2 vCPU / 2 GB / 28 GB | `Golden bench v15` | `version-15` / `version-15` / `3.11` | site mode, cold-only |
| `bench-nightly` | `bench/` | 2 vCPU / 2 GB / 28 GB | `Golden bench nightly (develop)` | `develop` / `develop` / `3.14` | site mode, cold-only, records resolved SHAs |
| `bench-v16-admin` | `bench/` | 2 vCPU / 2 GB / 28 GB | `Golden bench v16 (admin)` | `version-16` / `version-16` / `3.14` | `build_mode = admin`, cold-only |
| `bench-v15-admin` | `bench/` | 2 vCPU / 2 GB / 28 GB | `Golden bench v15 (admin)` | `version-15` / `version-15` / `3.11` | `build_mode = admin`, cold-only |
| `bench-nightly-admin` | `bench/` | 2 vCPU / 2 GB / 28 GB | `Golden bench nightly admin (develop)` | `develop` / `develop` / `3.14` | `build_mode = admin`, cold-only |
| `proxy` | `proxy/` | 2 vCPU / 1 GB / 10 GB | `proxy-image` | — | `exclude = ("test",)`, `finalize = _finalize_proxy`, `is_proxy` |

The three `*-admin` recipes bake the same `bench/` tree at each Frappe version but in
**`admin` mode**: `build.sh` skips `bench new-site` + ERPNext and leaves only the
bench plus the bench-cli **admin console** (a Flask management app) running for the
snapshot. A clone's first-boot `deploy-site.py --mode admin` sets `[admin].domain =
<fqdn>` + `bench setup production` so the FQDN serves the admin app, and the readiness
probe is the admin app's `/api/status` (it has no Frappe `/api/method/ping`). They
are cold-only and never register — the admin image is a distinct product, not the
self-serve site golden. See *Bake mode (site vs admin)* in
[spec/08-images.md](08-images.md).

A back-compat alias `bench` resolves to `bench-v16` (`get_recipe("bench")`) so older
callers (`bootstrap.py`, the warm-restore e2e) keep working; the alias is **not** a
Select option (`recipe_names()` excludes it) — the operator always picks an explicit
version.

The recipe **subsumes the per-module constants** that used to live in the build
verbs and the e2e modules (`GOLDEN_DISK_GB`, `GOLDEN_MEMORY_MB`,
`REMOTE_*_DIRECTORY`, the `test/` exclude, the proxy finalize block). `finalize`
is a callback because the proxy's post-build step (write `REGION_FILE` with
`placement.atlas_region()` — the single `Atlas Settings.region` —
`systemctl restart nginx.service`, [`_finalize_proxy`](../atlas/atlas/image_recipes.py))
is genuinely code; the bench recipes have `finalize = None`. `registers_as` lets a
successful **v16** bake auto-set `Atlas Settings.default_bench_snapshot` (the field
self-serve already reads); only v16 registers (one warm self-serve golden per
server) — v15/nightly are cold customer goldens that promote to a base image but
don't replace the self-serve default. Proxy snapshots feed a fleet, not a Single, so
they have no `registers_as`.

### Versioned bench variants — one tree, three versions, two modes

The customer-facing bench goldens are baked per Frappe/Bench release and differ
**only in data**: the Frappe/ERPNext branch + Python version, plus the bake
`build_mode` (`site` for a fully-baked site, `admin` for the admin console — see
spec/08). One committed `bench/` tree bakes all six (three versions × two modes) —
the controller injects the version two ways:

- **`bench.toml` is rendered before upload.** `image_builder._render_bench_toml(recipe)`
  reads the committed `bench/bench.toml` and rewrites two lines — `[bench].python`
  ← `python_version`, and the `frappe` app's `[[apps]].branch` ← `frappe_branch` —
  with stdlib line-targeted substitution (no Jinja2: a template format would clash
  with TOML's own `{ }`, and two lines don't earn a dependency). The edit is
  section-aware (a second `[[apps]]` block's branch is never touched) and fails loud
  if a targeted line is missing. `run_build` swaps the committed `bench.toml` upload
  for the rendered temp file (via an `ExitStack` that unlinks it after staging,
  success or throw), so `build.sh` still copies its sibling `bench.toml` verbatim —
  the proven recipe is unchanged.
- **`bench-cli` ref + ERPNext branch ride the build command's env.** Neither lives
  in `bench.toml` (the cli ref is `install.sh`'s checkout target; the ERPNext branch
  is a `get-app --branch` arg), so `_build_command(recipe)` prefixes the detached
  build with `export BENCH_CLI_REF=… ERPNEXT_BRANCH=… && … build.sh <mode>`.
  `build.sh` reads each as `${VAR:-<default>}`, keeping a direct run reproducible at
  v16. `bench-cli` is the build *tool*, not the framework — it reads the branch +
  Python from `bench.toml` and natively knows `version-15`/`version-16`/`develop`, so
  **one pinned cli ref bakes all three** variants (`uv venv --python <X>` fetches the
  interpreter, so the host needn't preinstall 3.11 for v15).

The nightly variant tracks moving `develop`, so `build.sh` stamps the resolved
`frappe`/`erpnext`/`bench-cli` commit SHAs on `ATLAS_BUILD_*=` lines; the controller
harvests them from the build Task into `Image Build.build_inputs` (JSON), so even a
nightly image is traceable to its real inputs.

**Release gate:** v15 + Python 3.11 compatibility is unproven until a real bake (a
host fact, deferred to the e2e ride-along; the unit suite covers the rendering +
pin plumbing).

## The shared builder seam

[`atlas/atlas/image_builder.py`](../atlas/atlas/image_builder.py)'s
`run_build(virtual_machine, recipe, on_task=None)` is the de-duplicated core the
two build verbs collapse into. It:

1. `connection_for_guest(vm)` + `forget_host(host)` — the recycled-IP host-key
   trap (real-provision-traps #1); this path goes straight to scp/ssh with no
   `wait_for_ssh`, so a stale pinned key must be dropped first.
2. `tree_uploads(recipe)` — enumerate the committed tree (`rglob`, skipping
   `recipe.exclude` and `__pycache__`), then `mkdir -p` + `run_scp` every file
   under one staging dir so `build.sh` finds its siblings.
3. `run_detached(build.sh, log, done)` — run the long build (apt/clone/uv for
   bench, an nginx+luajit compile for proxy) **detached**, so a mid-build SSH
   reset doesn't SIGHUP it; poll the marker. ([04-tasks.md](./04-tasks.md),
   `transport.run_detached`.)
4. `recipe.finalize(vm, connection, key_path)` — the post-build guest step, if
   the recipe has one. Its exit status becomes the build's, so a finalize failure
   is a build failure.
5. `_record_guest_task(...)` — one Task row (named by `recipe.task_script`,
   `bench-build` / `proxy-build`) for the audit trail, the same row shape as every
   guest op. `on_task`, if given, is called with the Task name **before** the
   throw, so the Image Build controller links the build Task even on failure.
6. `frappe.throw` on any non-zero exit — fail loud at the boundary (spec taste
   #17); the operator retries by clicking.

`bench_image.build_bench` and `proxy.build_proxy` are now thin wrappers over
`run_build` (proxy keeps its `is_proxy` guard). Their public signatures
are unchanged, so `bootstrap.py`, the e2e modules, and any caller keep working.
`proxy.py` keeps `reconcile_*`, `push_cert`, `canonical_json`,
`wildcard_targets`, and `_record_guest_task` (now returning the Task
name) — only the upload/build half of `build_proxy` moved.

## The `Image Build` DocType

The operator-facing object: one row per bake run, named `IMG-BUILD-#####`. It is
**operator-only** — `Image Build` carries only a System Manager permission and is
not in `_OWNED_DOCTYPES`, so it is invisible and access-denied to the SPA `Atlas
User`, like `Server` / `Task`. Baking images is an operator-fleet
operation, not a per-user one.

Fields and the full table are in
[02-doctypes.md → Image Build](./02-doctypes.md#image-build). The identity tuple
(`recipe`, `server`, `base_image`) is `set_only_once` and guarded in
`validate()` — re-baking with a different recipe/server/base is a new row, not an
in-place edit (the same shape as `Site` / `Virtual Machine`).

### Lifecycle

1. **`before_insert`** resolves the recipe, copies its `title`, defaults
   `base_image` from `placement.default_image()`, and starts `Draft`. The build
   VM is created in the background job, not here — provisioning SSHes and must
   not block the insert.
2. **`after_insert`** enqueues `run` on `queue="long"` (it SSHes and waits
   ~10–20 min — the same queue `Site.auto_provision` and image-sync use). No-op if
   not `Draft`.
3. **`run(image_build_name)`** — the background orchestration. The part that used
   to live only in e2e helpers, now first-class:

   | Step | Action | Status |
   | ---- | ------ | ------ |
   | 1 | Provision a scratch build VM at the recipe's size on `server` from `base_image` (an `is_proxy` recipe stamps `is_proxy`). **Commit**, then wait for its own after_insert provision job to reach Running. | `Provisioning` |
   | 2 | `run_build(vm, recipe)` — upload the tree + run `build.sh` in the guest (+ finalize). Links the `build_task`. | `Building` |
   | 2a | **Sanity gate** (bench recipes only): `bench_image.sanity_check(vm)` SSHes the still-running build VM and proves it actually *works* — site: serves + the baked password logs in; admin: the admin console renders — before it is allowed to become a snapshot. See *The post-build sanity gate* below. A miss raises → the build fails here, never snapshots. | (still `Building`) |
   | 3 | Cold (default): stop the build VM and `snapshot(title=recipe.snapshot_title)`. **Warm** (`warm` checked): run the warm finalize instead — see below. Link it into `snapshot`. | `Snapshotting` → `Available` |
   | 4 | If `auto_register` and the recipe has `registers_as`, write the snapshot into that Atlas Settings field. | (still `Available`) |
   | 5 | If `terminate_build_vm`, terminate the scratch build VM. | |

   Any failure flips `status = Failed`, records the stderr tail in `error`, and
   re-raises (fail loud — the job log carries the traceback). No-op if the build
   has moved past `Draft`. Every transition is committed and pushed to the
   operators' realtime room (`image_build_progress`, doc-scoped) so the desk
   form's live checklist updates without a reload — the same per-transition
   commit-then-report pattern `Site.auto_provision`
   ([14-self-serve.md](./14-self-serve.md)) uses, here pushed to a desk room.

4. **`rebake()`** resets an `Available`/`Failed` row to `Draft` and re-enqueues —
   the operator's retry button. The whole pipeline is idempotent (`build.sh`
   re-runs cleanly, a re-bake reuses a surviving build VM), so retry = re-run
   (spec taste #16).

The **commit-before-wait** in step 1 is load-bearing and copied from
`Site.auto_provision`: the build VM's own `after_insert` enqueued its boot job in a
**separate** transaction that can't run until this one commits. Holding the
transaction open and blocking on the wait would deadlock the boot, time out, and
roll back the VM row — orphaning its boot job.

### The post-build sanity gate

`build.sh` already curls an **unauthenticated** `/api/method/ping` before it lets the
bake finish, which proves the web server is up but says nothing about whether the
baked site's **Administrator password actually works**, or — for an admin bake —
whether the **admin console page actually renders**. A site build with a wrong/empty
baked password serves a clean `pong`; an admin build whose console assets failed to
build serves a `200` from `/api/status` while the page itself is blank — both
snapshot clean and only break in front of a customer. Step 2a closes that gap at the
source. After `run_build` returns (the production stack is up, the build VM still
Running) and *before* the snapshot, `bench_image.sanity_check(vm)` SSHes the guest and
asserts — over a **mode-specific endpoint**, because the two bakes serve differently
on the *build* VM:

**Site mode** — over `:80` with the baked-site `Host: site.local` (the FQDN rename
happens only at deploy):
- **serves** — `/api/method/ping` answers `200` + `pong`.
- **logs in** — the baked Administrator password (`Site.BAKED_ADMIN_PASSWORD`, the one
  place that string lives — imported, not re-spelled) authenticates: `200` + `Logged
  In` + a `sid` session cookie.
- **rejects a wrong password** — the negative control. Without it a login endpoint
  that `200`s on anything would pass the login check falsely; the rejection proves the
  auth is real, not an open door.

**Admin mode** — the admin console is the bench-cli admin Flask app on the **internal
admin gunicorn** at `127.0.0.1:(<[admin].port>+1)`. At bake time `[admin].domain` is
still unset (`deploy-site.py` sets it per clone), so there is **no `:80` admin vhost
yet** — `:80` is just the default nginx server. So the gate reads `[admin].port` from
the guest `bench.toml` and probes the internal port directly:
- **serves** — `/api/status` on the admin gunicorn answers `200`.
- **console renders** — `GET /` returns `200` **and** the HTML carries the `Bench
  Admin` console marker. `/api/status` can be `200` while the page is blank/`500`
  (a bad asset build, a broken template); this asserts the admin URL a customer hits
  after deploy actually renders.

Any failed assertion (or an unreachable guest) `frappe.throw`s, so the build flips to
`Failed` and **never snapshots** — a broken build is caught at bake time, not by the
first customer. Proxy builds bake no Frappe/admin app, so they skip this gate (they
keep their own in-`build.sh` health check). This is the controller's *only* serving
assertion; everything else is `build.sh`'s job (spec taste #15).

### The warm bake (`warm`)

A bench bake with **`warm`** checked produces a `kind=Warm`
`Virtual Machine Snapshot` — the fan-out golden of
[05-virtual-machine-lifecycle.md → Warm snapshot fan-out](./05-virtual-machine-lifecycle.md#warm-snapshot-fan-out-one-golden-n-restored-clones):
clones of it **resume** a pre-warmed, already-serving guest instead of booting
one. Where the cold golden's contract is "bench installed, everything
stopped", the warm bake's is the opposite — whatever is resident in the
guest's RAM at the pause is exactly what every clone wakes into. Step 3
becomes:

1. **Arm the guest** — run the recipe's `warm_entrypoint` (`bench/warm.sh`)
   over guest-SSH, recorded as a `bench-warm` Task. It installs and starts the
   **identity freshen unit** (`atlas-warm-freshen`, which must be alive
   mid-loop at the capture instant), runs `bench setup production` against the
   baked `site.local` **with `listen [::]:80;` added to the vhosts** (the
   clone is probed and served over its /128 — a v4-only frozen nginx fails
   every real probe), **pre-warms with real localhost HTTP on both families**
   (an Administrator login + `/app`, `/login`, pings — so gunicorn workers,
   the MariaDB buffer pool, compiled assets and bootinfo are resident in the
   RAM about to be frozen), deletes the systemd random-seed (clone-entropy
   hygiene), and ends with a **`sync`**: the disk snapshot below is
   crash-consistent, so anything still dirty in the page cache would exist in
   the frozen RAM (restores see it) but not on the captured disk — the
   cold-boot fallback would boot a guest with no freshen unit and never become
   reachable (proven on a real host).
2. **Capture at one paused instant** — `warm-snapshot-vm.py` pauses the vCPUs,
   `PUT /snapshot/create`s the memory pair, takes the LVM thin snapshot of the
   disk **while still paused** (the pair is only valid together), moves the
   pair to the durable `/var/lib/atlas/snapshots/<name>/`, records the host
   signature beside it, and resumes. Fail-loud — a bake step, not an
   opportunistic fast path.
3. **Register + supersede** — the row captures the machine config (vcpus,
   memory) and tap name the vmstate pins; older Warm rows on the same server
   are trashed (one current warm golden per server; their `on_trash` removes
   the LV + memory directory). Then the build VM is stopped — the warmth lives
   in the artifact, not the scratch VM.

Only recipes with a `warm_entrypoint` can bake warm (`before_insert` rejects
the rest); today that is `bench` only. `auto_register` applies as usual: the
warm row is also a perfectly good **cold** golden (its disk carries the baked
site + production config), so registering it as `default_bench_snapshot`
gives one row both roles — the per-server warm resolution and the
single-value cold fallback stay distinct *concepts* either way
([14-self-serve.md](./14-self-serve.md)).

### The build VM is scratch; the snapshot is durable

The **snapshot is the output**; the build VM is scratch. By default
`terminate_build_vm` is **off**, so the build VM is left Stopped for re-bake or
inspection (the e2e's historical behavior) — "scratch" means disposable, not
auto-deleted. The snapshot is a durable artifact that outlives its build VM:
self-serve sites and the proxy fleet clone from it indefinitely via
`Virtual Machine Snapshot.clone_to_new_vm`, which takes the clone's `server` from
the snapshot's own row, not the (possibly-gone) build VM (see
[14-self-serve.md](./14-self-serve.md) and [08-images.md](./08-images.md)).

## Entry points

- **`Image Build` → New** in Desk, or **`Server` → Bake Image** (an `Actions ▾`
  item on an Active server, parity with **Sync Image**) — opens a dialog that
  inserts an `Image Build` on that server and routes to its live-checklist form.
- **`Image Build` → Re-bake** on an Available/Failed row.
- **`Image Build` → Promote to image** on an Available row that has a snapshot —
  see below.

## Promoting a bake into a base image

A bake's output is a `Virtual Machine Snapshot`; new VMs already clone from it via
`clone_to_new_vm`. **Promote** turns that snapshot into a first-class
**base image** new VMs select with the ordinary `image` field — a named thing in
the image picker rather than a one-off snapshot you hand-locate. The mechanics and
the **warm-reject** rule live in
[08-images.md § Two origins for a base image](./08-images.md#two-origins-for-a-base-image-a-url-or-a-snapshot-promote);
this layer just exposes the button.

- **`Virtual Machine Snapshot` → Promote to image** is the primary entry point
  (`promote_to_image(image_name, title)`): on the snapshot's server,
  `promote-snapshot-image.py` `dd`s the snapshot LV into a read-only
  `atlas-image-<name>` LV and materializes the image dir (kernel hard-linked from
  the snapshot's `source_image`, rootfs presence sentinel), then registers a local
  (URL-less) `Virtual Machine Image` row. Same-server scope: the bytes never leave
  the host.
- **`Image Build` → Promote to image** is a thin delegate to the build's
  snapshot's `promote_to_image`, defaulting the image name to the recipe's
  `promote_image_name` (the **series name** — `bench-v15` / `bench-v16` /
  `bench-nightly`) when the recipe sets one, else `<recipe>-<build name>`. Both
  entry points funnel through the one snapshot method, so the warm-reject and every
  guard (not-Available, duplicate/invalid name, missing source kernel) live once.
- **The series name is the load-bearing link to Central.** Central's
  `upsert_central_images` links a `Central Image` to a `Virtual Machine Image` of
  the **exact same name** (name-match, [16-central.md](./16-central.md)). Promoting a
  versioned bench golden to `bench-v15` / `bench-v16` / `bench-nightly` and running
  **Fetch Images** flips the matching `Central Image.bake_status` to **Baked** and
  links `local_image`; customers then pick the version through the ordinary VM
  `image` field. A name mismatch leaves the `Central Image` orphaned at `Expected`,
  so the promote defaulting to the series name is what closes the loop.
- A **warm** bake's snapshot cannot be promoted (its value is the frozen memory
  pair a cold-booting base image discards); the button surfaces the same clean
  refusal from the snapshot method. Promote a cold bake; clone the warm one. (The
  three customer goldens are baked **cold** precisely so they can be promoted; v16
  may *also* be baked warm separately as the self-serve site accelerator.)

## Design decisions

A few choices that aren't obvious from the field list:

- **The recipe is code, not a DocType.** A recipe points entirely at committed
  files (the `bench/` / `proxy/` tree, the pinned `build.sh`) and a `finalize`
  callback, so a data row could only mirror it — the same call `sizes.py
  SIZE_PRESETS` and the `bootstrap.py` image constants already make. A third image
  type is a recipe entry plus a committed tree, no new module.
- **The proxy serves the one Atlas region.** A proxy build no longer takes a
  `region` input and the build VM is no longer stamped with one — Atlas is
  single-region, so the region the proxy serves is read once from
  `Atlas Settings.region` (`placement.atlas_region()`) at finalize, when
  `_finalize_proxy` writes it into the guest `REGION_FILE`. Nothing threads a
  region through the recipe, the dialog, or the build VM.
- **Distinct Task script names.** The audit Task keeps the per-recipe name
  (`bench-build` / `proxy-build`, via `recipe.task_script`) rather than one generic
  `image-build`, so the operator's Task list stays readable.
- **No snapshot back-link.** Provenance rides the `Image Build.snapshot` forward
  link only; `Virtual Machine Snapshot` stays frozen. A `Virtual Machine
  Snapshot.image_build` back-link is a cheap future add if "what baked this
  snapshot?" from the snapshot side becomes a real need.
- **No concurrency lock.** A second `Image Build` on a busy server just provisions
  another VM. Two bakes of the same recipe racing to `auto_register` the same Atlas
  Settings field is last-writer-wins (acceptable).

## Testing

- **Unit (milliseconds):**
  - *Recipe registry + seam* — the recipe shapes, the tree enumeration with
    `exclude`/`__pycache__` filtering, the `run_build` upload→detached-build→Task
    path (SSH plumbing mocked), the `on_task` callback firing before the throw,
    fail-loud, and the proxy finalize running after the build. See
    [`atlas/atlas/test_image_builder.py`](../atlas/atlas/test_image_builder.py).
  - *Controller* — `before_insert` defaults,
    immutability, the `run()` state machine (status transitions, artifact
    linking, auto-register on/off, terminate on/off, fail-loud, the
    not-`Draft` no-op, the **sanity gate** running for bench / skipped for proxy /
    failing the build before any snapshot), and `rebake`. Host steps mocked at the
    module seams. See
    [`atlas/atlas/doctype/image_build/test_image_build.py`](../atlas/atlas/doctype/image_build/test_image_build.py).
  - The two build verbs keep their own thin coverage of what they still own —
    `build_proxy`'s `is_proxy` guard
    ([`test_proxy.py`](../atlas/atlas/test_proxy.py)) and `build_bench`'s
    delegation, plus the **sanity-gate** verdict logic (serve/login/wrong-password
    cases, the site-vs-admin command shape, and the unreachable-guest throw) with
    the guest SSH mocked ([`test_bench_image.py`](../atlas/atlas/test_bench_image.py)).
  - *Promote* — `promote_to_image` guards (not-Available, **warm-reject**,
    invalid/duplicate name, missing source kernel), the local-image row shape
    (URL-less, inherited kernel, `rootfs_filename` = LV name), the URL-less-image
    sync skip + throw, and `Image Build.promote` delegation (Task seam mocked);
    plus a `lib/atlas/lvm` unit for `import_base_image_from_lv` (the local-LV
    import path). See the snapshot / image / image-build / lvm test modules.
- **Host facts (e2e):** the promote host fact — promote a real *cold* snapshot,
  assert the read-only base image LV + image dir on host, then provision a VM that
  selects the promoted image via `image` and boot it — rides along in
  [`virtual_machine_snapshot.py`](../atlas/tests/e2e/use_cases/virtual_machine_snapshot.py)'s
  `run_smoke` (`_check_promote_to_image`), since promote is a snapshot operation.
- **Host facts (e2e):** the bake's host facts — a baked VM has a working `bench`
  over guest-SSH ([`bench_image.py`](../atlas/tests/e2e/use_cases/bench_image.py)),
  the proxy compiles and serves ([`proxy_vm.py`](../atlas/tests/e2e/use_cases/proxy_vm.py)) —
  are unchanged; they exercise the same `build_bench`/`build_proxy` verbs, which
  now route through `run_build`. Driving those e2e modules through the `Image
  Build` DocType (insert a row, assert it reaches `Available`) rather than the
  bare build verbs is a follow-up, host-verifiable on a real droplet.
