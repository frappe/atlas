# Self-serve — session handoff (2026-06-10)

> **RESOLVED 2026-06-10 (later run).** G1 + G2 below are **done** — the rename
> model is host-proven end to end from a clean slate: the golden image was baked
> from scratch by `build.sh` (snapshot `m6tuh2lmou`), and a real signup → verify →
> cloned golden site → deploy → live HTTPS through the proxy on v4 + v6 went green.
> The run surfaced and fixed the one remaining host-only bug — the deploy must
> repoint `default_site` to the FQDN (bench-cli's `frappe serve` ignores the Host
> header on a clone; it resolves by `default_site`). See **DRIFT.md → "Live host
> run (2026-06-10)" L-1..L-3** for the record. The items below are kept for context;
> the forward plan they describe has been consumed.

State after a long manual-verification + bug-fixing session. The self-serve flow
**was proven working end-to-end** (a fresh signup → live site, v4+v6) under the
*old* new-site model; mid-session the code pivoted to the **rename model**, which
needs a fresh golden image that the re-bake is still producing. Several real
host-SSH reliability bugs were found and fixed along the way.

**Read first:** [DRIFT.md](./DRIFT.md) M-1..M-7 — the full blow-by-blow with code
locations and rationale. This file is the *forward* plan; DRIFT is the *record*.

---

## Next-session goals (in order)

1. **Fix the missing gaps** (below — chiefly: finish/verify the golden re-bake,
   then re-prove the rename-model flow end-to-end).
2. **Commit the changes** as multiple focused commits (suggested split below).
3. **Build the features** that make this smooth (the durable follow-ups below).
4. **Update docs + specs** (list below).
5. **Fix tests / e2e / bootstrap** (list below).

---

## 1. The missing gaps (do these first)

### G1 — Finish + verify the golden re-bake (the one blocker)

The rename-model `deploy-site.py` *requires* a golden image carrying a baked
`site.local`. The current `Atlas Settings.default_bench_snapshot = tpm31foak4` is
the **old, site-LESS** snapshot (made last session by snapshotting a hand-fixed
VM) — it will **fail** the new `_rename_site` (`baked site site.local missing`).

A re-bake (`bench_image.run_smoke`) with the new `build.sh` was **running at
handoff** (background task `bdxxcc5jb`, build VM at `2400:...:5206:e008`, started
10:15). On resume:

- **Check it finished.** Look for a new `golden-bench` snapshot (creation > the
  `golden-bench-v2` from 2026-06-09 21:58). `ps aux | grep run_smoke`. The e2e
  leaves the snapshot in place and prints `GOLDEN BENCH IMAGE BAKED`.
- **If it succeeded:** set `default_bench_snapshot` to the NEW snapshot, then
  verify it carries `site.local` (clone it or just trust the e2e's bench-works
  assert), then drive a fresh signup (below) and confirm v4+v6.
- **If it failed:** read the error — keepalive + detached build mean it now
  **fails fast and legibly** (no more 1800s hangs). The likely remaining failure
  is in `build.sh` itself (the `new-site site.local` + `is_setup_complete` steps
  are host-unproven, DRIFT D01-5 open items) — debug on the build VM directly.
- **If still running:** the detached build can legitimately run the full ~15min;
  peek at the guest's `/root/bench-cli/benches/atlas/build.log` over SSH.

### G2 — Re-prove the rename-model flow end-to-end

Once a `site.local`-carrying golden is wired, do a **fresh signup** and confirm
the full chain works under the rename model (it was only ever proven under the
old new-site model — `golden2`, last session):

- Insert a `Site` (or use `/signup` + `/verify`); the worker's `auto_provision`
  drives clone → boot → **rename** `site.local`→`<fqdn>` → reset admin pw →
  `setup production` (+ the v6 listener fix) → HTTP 200 → Subdomain → Running.
- Verify off-droplet HTTPS through the proxy returns `{"message":"pong"}` over
  **both v4 and v6** (the real "is it working" test — see the curl recipe in
  DRIFT M-5 / the OUTCOME header).
- Confirm the rename `os.rename(sites/site.local → sites/<fqdn>)` actually serves
  the renamed dir under the new Host (DRIFT D01-5 open items (a)/(b)/(c): no
  `host_name` cache residue, `set-admin-password` resolves the renamed dir,
  nothing in baked `site_config` pins the old name).

### G3 — Cleanup billable leftovers

Many VMs/snapshots accumulated debugging this session. Audit + terminate what's
not needed:
- Stale build VMs (`e005`, `e008` and whatever the re-bake leaves), the old
  `magicaldeploy`/`golden2`/`fffff` site VMs, the proxy-e2e VMs if unused.
- Old snapshots: `golden-bench` (og... 15:26), `golden-bench-v2` (tpm31foak4) once
  the new bake supersedes them. Keep exactly one golden.
- **Pre-clear recycled-IP host keys** before any new provision:
  `ssh-keygen -R <addr> -f ~/.atlas/known_hosts` (see G3-feature below to automate).

---

## 2. Suggested commit split (all currently uncommitted on `idea/bench`)

The whole self-serve layer + this session's fixes are **uncommitted** (mostly
`??` new files). Branch is `idea/bench`; last commit `d3d8c60`. Split by concern:

1. **`feat: self-serve site layer (Site + Site Request doctypes, signup/verify)`**
   — `atlas/atlas/doctype/site/`, `doctype/site_request/`, `subdomain_label.py`,
   `api/signup.py`, `site_status.py`, `www/{signup,verify,site_status}.*`,
   `templates/emails/`, the `atlas_settings.json` (`default_bench_snapshot`),
   `permissions.py`, `hooks.py`, `placement.py` deltas, + their unit tests
   (`test_site.py` etc., `tests/test_api_signup.py`, `test_site_status.py`).
2. **`feat: golden bench image (build.sh) + in-guest deploy (deploy-site.py, rename model)`**
   — the `bench/` tree, `atlas/atlas/bench_image.py`, `deploy_site.py`,
   `test_bench_image.py`, `test_deploy_site.py`.
3. **`fix: SSH transport reliability (keepalive + detached long-running build)`**
   — `atlas/atlas/_ssh/transport.py` (ServerAliveInterval), `bench_image.py`
   `_run_detached_build` (if not already in commit 2, keep the transport bit
   separate). This is the M-7 work; call it out — it benefits every SSH op.
4. **`fix: test_letsencrypt _StubDns missing upsert_wildcard`** — the stale stub
   (the only non-self-serve code change; from the earlier wildcard-DNS drift).
5. **`docs: self-serve spec (ch.14) + roadmap v0.7/0.8/0.9 + doctype catalogue`**
   — `spec/14-self-serve.md`, `spec/{02-doctypes,08-images,09-roadmap,11-user-ui,README}.md`,
   `CLAUDE.md`, `llm/` docs (`plans/`, `self-serve-parallelism.md`, `ideas.md`,
   `migration-design.md`, the `proxy-design.md`/`proxy-handoff.md` delta).
6. **`test: self-serve e2e (self_serve_site, bench_image use cases)`** —
   `atlas/tests/e2e/use_cases/{self_serve_site,bench_image}.py`.

Adjust to taste; the point is reviewable, single-concern commits. **Per repo rule
([CLAUDE.md](../../../CLAUDE.md)): keep diffs tight, don't let ruff reflow whole
files — `git add -p`.**

---

## 3. Features to build (make it smooth — the durable follow-ups)

These are flagged in DRIFT; they're what turns "works after I babysat it" into
"works unattended":

- **F1 — Auto-clear recycled-IP host keys.** The provision/build path should
  `ssh-keygen -R <addr>` right after creating a VM (or `wait_for_ssh` should treat
  a *changed* key as "recycled IP, re-pin" instead of hard-failing). Bit us
  repeatedly. [[atlas-real-provision-traps]] #1. (DRIFT M-7 follow-up.)
- **F2 — `proxy.build_proxy` has the same foreground-build fragility** as
  `build_bench` did (one long `run_ssh` of build.sh → a connection reset kills the
  build). Extract `_run_detached_build` to a shared `_ssh` helper and use it in
  both. (DRIFT M-7 follow-up.)
- **F3 — Restore-Atlas-Settings helper / guard.** The unit suite clobbers FOUR
  Atlas Settings fields (`ssh_private_key_path`, `ssh_public_key`, `ssh_key_id`,
  DO `api_token`) → real provisioning fails. A `bench execute` helper that
  restores all four from site config (and/or a conftest that snapshots+restores)
  would stop this recurring. (DRIFT M-1, [[atlas-real-provision-traps]] #4.)
- **F4 — Stuck-task / failed-Site reaper + status surfacing.** A Site that fails
  mid-provision sits `Failed` with the backing VM/Tasks needing manual reading.
  The existing `mark_orphan_tasks_failure` (e2e) should become a scheduled job;
  surface the failing step on the Site row. (Roadmap already lists a stuck-task
  reaper.)

---

## 4. Docs + specs to update

Most spec landed already (DRIFT D06; spec/14 + roadmap v0.7/0.8/0.9 are written).
Remaining:

- **`spec/08-images.md`** — the golden section already describes the rename model
  (D01-5). Re-read after G1 succeeds; correct anything the real bake disproved
  (the `new-site site.local` + `is_setup_complete` + rename host facts).
- **`spec/14-self-serve.md`** — confirm the deploy section matches the *rename*
  model (it may still describe `bench new-site` per signup). Update the readiness
  /serving prose if so.
- **`bench/README.md`** — the "Serving model" section: confirm it's the rename
  model end-to-end and matches `build.sh`/`deploy-site.py`.
- **SSH reliability** — `spec/04-tasks.md` (or wherever the SSH execution model
  lives) should note keepalive + the detached-long-build pattern as the contract
  for long guest builds.
- **DRIFT.md** — already current through M-7; keep appending as gaps close.

---

## 5. Tests / e2e / bootstrap to fix

- **Unit tests — currently green** for the touched modules (run them to confirm
  after any change): `test_site` (26), `test_deploy_site` (14), `test_api_signup`
  (7), `test_site_request` (15), `test_bench_image` (5), `test_letsencrypt` (3),
  ssh transport (15) + ssh (5). **Full-suite caveat:** the shared test DB has
  **pre-existing pollution** (DRIFT D02 / D06-5) — `test_placement`,
  `test_virtual_machine` cpu-default, `test_api_server_capacity`,
  `tls_certificate` denorm, and the SPA-build `test_website_route` fail from
  committed Active rows left by past e2e runs. A **test-DB reset** is the real fix
  (out of scope so far). Also: a stuck `long` RQ queue (599 dead jobs) caused a
  `QueueOverloaded` cascade once — drain it if the full suite errors weirdly.
- **e2e — `self_serve_site`** is written but its host run uses the *rename* golden
  now. After G1/G2, run `self_serve_site.run_smoke` and confirm it passes on the
  rename model. **Known e2e gap (DRIFT M-5):** the proxy→site south hop was only
  ever exercised against `proxy_vm`'s echo-server stand-in, never a real bench-cli
  vhost — which is *why* the v6-vhost bug escaped. Consider adding a real-vhost
  assertion so this can't regress.
- **Bootstrap script** — `atlas/bootstrap.py` (`run` / `run_with_proxy`) does NOT
  yet stand up the self-serve layer (golden snapshot bake, `default_bench_snapshot`,
  SMTP/email for verification). Add a self-serve tail (or a `run_with_self_serve`)
  so a fresh site can be brought to "signup works" in one command — mirrors how
  `run_with_proxy` added the TLS tail.

---

## Reference — what was fixed this session (all uncommitted)

Code bugs (all unit-proven):
- **Orchestration deadlock** in `Site.auto_provision` — committed after clone +
  poll VM status (not inline SSH-wait) + commit on Failed. (DRIFT M-4.)
- **Boot-wait timeout** 600→1500s. (DRIFT M-4 follow-up.)
- **IPv4-only site vhost** → v6 404 — `build.sh` removes stock default vhost;
  `deploy-site.py` `_enable_ipv6_listeners` adds `listen [::]:80;`; `_serving`
  probes v6. (DRIFT M-5.)
- **SSH keepalive** (`_ssh/transport.py`), **detached build** (`bench_image.py`
  `_run_detached_build`). (DRIFT M-7.)
- **`test_letsencrypt` `_StubDns`** missing `upsert_wildcard`. (DRIFT D06-4.)

Host facts proven: golden clone serves; worker-driven Running; v4+v6 inbound via
proxy (under the OLD new-site model — `golden2`). The rename model is built +
unit-green but **not yet host-proven** (that's G1/G2).

Operational gotchas (memory [[atlas-real-provision-traps]] now covers 1+4+5):
recycled-IP stale host key (`ssh-keygen -R`), 4-field Atlas Settings clobber,
worker must run with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, `bench execute`
import trap (clear stale `.pyc`).
