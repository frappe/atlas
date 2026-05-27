# Atlas — Specification

Atlas is a Frappe app for managing Firecracker virtual machines on servers.
It is the lowest layer of a Frappe hosting platform. Sites, benches, IAM, and
billing live in separate apps on top.

The spec describes the system as it is and is the source of truth. When the
spec and code disagree, the spec is authoritative: update the code to match,
unless the spec is wrong — in which case update the spec deliberately and
keep it the source of truth.

## Goals

- Track servers (the hosts that run Firecracker).
- Track virtual machines that run on those servers.
- Bootstrap a fresh server so it can host virtual machines.
- Provision, start, stop, and delete Ubuntu 24.04 Firecracker virtual machines.
- Drive everything from Atlas over SSH; record every task.
- Give each virtual machine a public IPv6 address.

## Non-goals (this iteration)

- No sites, benches, apps, databases, or workloads.
- No users, teams, roles, billing, quotas.
- No CLI. We will build one later on top of the same Frappe APIs.
- No private networking, no overlay, no IPv4 to the guest.
- No jailer, no unprivileged user, no SELinux or AppArmor. Root everywhere.
- No image build pipeline. We download Firecracker CI images and use them.
- No snapshots, live migration, or high availability.
- No autoscaling, scheduling, or placement. The operator picks the server.
- No metrics or alerting. `journalctl` is enough.
- No web UI of our own. Desk is the UI.

## Operating principles

1. **Desk is the UI.** Every operation is a DocType, a button on a DocType, or
   a server method on a DocType. No custom pages.
2. **The Frappe site is the source of truth.** A server is a cache; we can
   rebuild its on-disk state from the Frappe database. We do not scrape state
   back from the server.
3. **One task, one shell script.** Atlas uploads a shell script to a server
   over SSH and runs it. The script is the unit of work. We do not chain
   per-step SSH calls. See [04-tasks.md](./04-tasks.md).
4. **One virtual machine per server slot.** The operator picks the server
   when provisioning. No scheduler.
5. **Few dependencies.** Frappe + standard library + the system `ssh` command.
   On the server: `firecracker`, `systemd`, `iproute2`, `nftables`, `curl`,
   `jq`, `e2fsprogs`, `squashfs-tools`. No agent runs on the server.
6. **Don't import — copy.** If a third-party library has a good idea (pyinfra,
   zx), reimplement the small subset we need. We avoid library coupling on a
   foundational layer.
7. **Names are full words.** `Server`, `Task`, `Virtual Machine`,
   `Virtual Machine Image`, `Server Provider`. No `VM`, `Cmd`, or `Metal Node`.

## Read this in order

1. [Architecture](./01-architecture.md)
2. [DocTypes](./02-doctypes.md)
3. [Bootstrapping a server](./03-bootstrapping.md)
4. [Tasks: the SSH execution model](./04-tasks.md)
5. [Virtual machine lifecycle](./05-virtual-machine-lifecycle.md)
6. [Networking](./06-networking.md)
7. [Filesystem layout on the server](./07-filesystem-layout.md)
8. [Images](./08-images.md)
9. [Roadmap and deferred decisions](./09-roadmap.md)

## Operator use cases

Everything Atlas does for an operator falls into one of seven use cases.
The list is the spec's index of operator-visible behavior; the e2e suite
mirrors it exactly (one module per use case, see
[`atlas/tests/e2e/use_cases/`](../atlas/tests/e2e/use_cases)). New
operator-facing features add to this list; new tests follow it.

| Use case                       | Operator action                                         | Spec |
| ------------------------------ | ------------------------------------------------------- | ---- |
| Provision a server             | `Server Provider` → **Provision Server**                | [03-bootstrapping.md](./03-bootstrapping.md) |
| Sync an image to a server      | `Virtual Machine Image` → **Sync to Server / All**      | [08-images.md](./08-images.md) |
| Provision a virtual machine    | `Virtual Machine` → **Provision**                       | [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) |
| Operate a virtual machine      | `Virtual Machine` → **Start / Stop / Restart / Terminate** | [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) |
| Run an ad-hoc task / reboot    | `Server` → **Run Task / Reboot**                        | [04-tasks.md](./04-tasks.md) |
| Talk to DigitalOcean           | (internal) verify the DO HTTP client                    | [01-architecture.md](./01-architecture.md) |
| Run a script over SSH directly | (internal) `run_task(connection=…)` before a `Server` row exists | [04-tasks.md](./04-tasks.md) |

The last two are internal contracts the higher use cases depend on; they
have their own e2e modules because they fail in different ways than the
operator-facing flows do.

## Testing

E2E tests are grouped by the use cases above, not by implementation
detail. The mapping is one module per use case under
[`atlas/tests/e2e/use_cases/`](../atlas/tests/e2e/use_cases); the
filenames mirror the table above (`server_provisioning.py`,
`image_sync.py`, `virtual_machine_provisioning.py`,
`virtual_machine_lifecycle.py`, `run_task.py`, `digitalocean_client.py`,
`ssh_primitive.py`).

Each use-case module is the **single source of truth** for that
operation's end-to-end coverage. It owns:

1. The happy path — one full pass against a real server.
2. The operator-visible negative paths for that use case (e.g. the
   image-missing path lives in `virtual_machine_provisioning` because
   that's where the operator triggers it).
3. The DocType-level validation throws that guard the same method —
   immutability, required fields, JSON shape, state-machine guards.
4. Synchronous-path coverage for the background jobs the use case
   normally enqueues (e.g. `image_sync` calls `execute_task` directly,
   not only via `frappe.enqueue`).

Bias toward adding a check to an existing use case. Add a new use-case
module only when the operator gets a new button.

### Entry points

- `bench --site atlas.local execute atlas.tests.e2e.run_all` — runs
  every use case that takes a server against **one shared bootstrapped
  droplet** (`reuse=True, keep=True`), then deletes it at the end. The
  regression entry point.
- `bench --site atlas.local execute atlas.tests.e2e.run_all_coverage` —
  same, plus the dedicated-droplet use cases
  (`digitalocean_client.run`, `server_provisioning.run`). Cost: three
  billable droplets.
- `bench --site atlas.local execute atlas.tests.e2e.use_cases.<use_case>.run`
  — happy-path-plus-validation for one use case. Use cases that need
  the shared bootstrapped server expose `run_against_shared(reuse=True,
  keep=True)` as well; they use the `phase()` context manager from
  `_droplets.py`.

Every e2e-created droplet is tagged `atlas-e2e`. The harness pre-sweep
prints droplets older than 30 minutes so the operator can delete them
by hand (the DO account also hosts production).

### Shared helpers

`_config.py`, `_droplets.py`, `_image.py`, `_tasks.py` (and the
`_shared.py` re-export shim) under [`atlas/tests/e2e/`](../atlas/tests/e2e)
are the substrate. Add helpers there when at least two use cases would
benefit; single-use helpers stay private to their module.
