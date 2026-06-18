# Fake provider — developer guide

A `provider_type = Fake` under which **every Atlas action just works** — it
transitions Frappe DB state without ever touching a real host or a vendor API.
It exists so engineers working on Atlas *integration* (Central, IAM, billing, the
dashboard SPA) have Servers / VMs / Images / Tasks to work against without
spinning up real cloud resources.

It is **`developer_mode`-only**: every mutating Fake method throws on a site
without `developer_mode`, so a stray Fake `Provider` row on production is inert.

- Implementation: [`fake.py`](./fake.py) (the `Provider` ABC) and
  [`fake_tasks.py`](./fake_tasks.py) (the Task/SSH short-circuit).
- Demo populate script: [`../demo.py`](../demo.py) + [`../demo_data.py`](../demo_data.py).
- Design + rationale: [`llm/references/fake-provider-design.md`](../../../llm/references/fake-provider-design.md);
  spec [`09-roadmap.md` § Developer tooling](../../../spec/09-roadmap.md).

## Why it needs two seams

Faking the VM lifecycle takes **two** interception points, not one:

1. The `Provider` ABC covers only *server* creation + reserved IPs. `FakeProvider`
   implements it (`provision()` returns a host already `ready` with synthetic,
   unroutable networking; `destroy()` and the reserved-IP calls are no-ops).
2. Every *Virtual Machine* action, image sync, and `Server.bootstrap()` runs as a
   **Task over SSH** through `run_task()`. So `run_task`/`execute_task` route a
   Fake-backed Server's Tasks to `fake_tasks.run_fake_task`, which finalizes the
   Task (Pending → Running → Success) with no SSH.

Routing is **per-Server** (off the Server's own `provider`), so a Fake provider
and a real Server can coexist on one site — each Task goes the right way.

## Set up the demo data on `fake.local`

The `fake.local` site already exists with `frappe` + `atlas` installed and
`developer_mode` on. Populate it with one command, run from the **bench root**
(`…/benches/v2`):

```bash
bench --site fake.local execute atlas.atlas.demo.run --kwargs "{'reset': True}"
```

`reset=True` wipes any previous demo fleet first, then rebuilds — so the command
is safe to re-run. Drop `--kwargs …` (or pass `{'reset': False}`) to add to what's
already there instead of rebuilding:

```bash
bench --site fake.local execute atlas.atlas.demo.run
```

It prints a row-count summary when it finishes. On a clean site you should see
roughly: 4 Providers, 7 Servers, 12 Virtual Machines, 4 Images, 4 Snapshots,
2 Reserved IPs, ~35 Tasks.

### What gets created

A deliberately varied fleet so every desk/SPA state has something to render:

- **Providers** — `fake-prod` (the active one), `fake-lab` (a second Fake
  provider), `demo-self-managed` (a Self-Managed host), and `do-legacy`
  (an *archived* DigitalOcean row, so a non-Fake vendor is visible too).
- **Servers** — across the whole status spectrum: `Active`, `Bootstrapping`,
  `Broken`, `Draining`, plus a Self-Managed host.
- **Virtual Machines** — every status (`Running`, `Stopped`, `Paused`,
  `Terminated`, `Failed`) and most features: a data disk, stop/termination
  protection, memory-snapshot-on-stop, relaxed-CPU burst, a proxy VM with an
  attached public IPv4.
- **Snapshots** — Cold (Available), Cold (Pending), a Warm golden (Available,
  also set as `Atlas Settings.default_bench_snapshot`), and a Failed one.
- **Reserved IPs** — one attached to the proxy VM, one free in a pool.
- **Tasks** — the real lifecycle Tasks from provisioning the fleet, plus a
  back-dated spread (Success / Failure / Running) so the log looks lived-in.

The rows are produced by the **real controllers** against the Fake provider, so
they are internally consistent — and the script doubles as a smoke test of the
fake seam.

### See it

```bash
# from the bench root, in another terminal:
bench start
```

Then open the desk at `http://fake.local:8007/app/atlas` (operator view) or the
dashboard SPA at `http://fake.local:8007/dashboard` (user view). The port is the
bench's `webserver_port` (8007 here — see the `web:` line in the Procfile). If
`fake.local` doesn't resolve, add it to `/etc/hosts`:

```
127.0.0.1 fake.local
```

### Verify every Desk action works

The `fake_provider_desk` e2e drives **every operator button** through the exact
HTTP layer the desk uses (`run_doc_method` for controller methods, `execute_cmd`
for the Reserved IP module-function buttons), with the desk's real argument
shapes — Provider (Authenticate / Refresh Catalog / Provision Server), Server
(Bootstrap / Run Task), Image (Sync to Server / All), the full VM lifecycle
(Provision / Start / Stop / Pause / Resume / Restart / Snapshot / Restore /
Rebuild / Resize / Clone / Terminate), Reserved IP (Allocate / Discover / Attach
/ Detach / Release), plus the wrong-state and fault-injection negatives:

```bash
bench --site fake.local execute atlas.tests.e2e.use_cases.fake_provider_desk.run
```

It prints `[fake-desk] all Desk buttons OK` on success. It is self-contained —
creates its own `fake-e2e` provider / server / image / VMs, tears them all down,
and restores `Atlas Settings.provider` — so it leaves the demo fleet untouched
and is safe to re-run. No droplet, no SSH; runs in seconds.

## Tear it down

### Remove just the demo rows (keep the site)

`wipe()` deletes every row that hangs off a Fake provider (cascading their
Servers / VMs / Snapshots / Tasks / Reserved IPs) plus the demo images. Real
DigitalOcean / Scaleway / Self-Managed rows are never `type=Fake`, so they are
never touched.

```bash
bench --site fake.local execute atlas.atlas.demo.wipe
```

(Re-running `demo.run` with `reset=True` calls `wipe()` for you first, so you only
need this if you want the site empty without repopulating.)

### Drop the whole site

```bash
bench drop-site fake.local --force --mariadb-root-password root
```

`--mariadb-root-password` is this bench's MariaDB root password (`root`, from
`sites/common_site_config.json`). Omit `--force` to be prompted before the
database is dropped.

## Fault injection — fake a failed action

To exercise error paths (a failed provision, a failed snapshot, a `Broken`
server), make specific scripts fail instead of succeed. Two ways:

1. **Persistent, per-provider.** Set the **Fail Scripts** field on a Fake
   `Provider` row (Desk → Provider, visible only when `provider_type = Fake`) to a
   comma/newline list of script names, or `*` for all:

   ```
   provision-vm.py, snapshot-vm.py
   ```

   Every matching Task on any VM on that provider's servers then fails. A failed
   fake Task is indistinguishable from a real one: the Task row is `Failure`, the
   VM lands `Failed` (or the Server `Broken`), and the retry button returns.

2. **Per-call, for tests.** Set `frappe.flags.fake_fail` to a script name, a set
   of names, or `{"script": "...", "reason": "..."}`, run the action, then clear
   it:

   ```python
   frappe.flags.fake_fail = {"script": "provision-vm.py", "reason": "demo"}
   try:
       vm.provision()      # raises; vm.status -> Failed
   finally:
       frappe.flags.fake_fail = None
   ```

## Use the Fake provider by hand (without the demo script)

On any `developer_mode` site:

1. Create a `Provider` with `provider_type = Fake`, mark it active in
   **Atlas Settings**.
2. On the Provider form, **Refresh Catalog** to seed the Fake `Provider Size` /
   `Provider Image` rows (so a Server's size/image Links resolve).
3. **Provision Server** — the row marches `Pending → Active` through the real
   worker (faked bootstrap), no SSH.
4. Create a `Virtual Machine` against that Server and any active
   `Virtual Machine Image`; provision / start / stop / snapshot / terminate all
   just work.

## Safety notes

- **`developer_mode` gate** on every mutating Fake method — inert on production.
- **Unroutable addresses** — synthetic IPs come from the documentation/test
  ranges (servers: IPv4 `203.0.113.0/24`, IPv6 `2001:db8::/32`; reserved IPs:
  IPv4 `198.51.100.0/24`), so even an accidental real `ssh` can't reach a
  stranger's machine.
- **Per-Server routing** — `wipe()` and the Task short-circuit key off the
  Server's own provider, so they never touch real DO/Scaleway/Self-Managed rows.
