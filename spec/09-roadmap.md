# Roadmap and deferred decisions

This iteration is a building block. Two flavors of deferral live here:

1. Things we know we'll do later, with a cheap path to add them.
2. Architectural questions we punted on, and how we plan to revisit them.

## Punted decisions

### SSH execution model — "one Task = one shell script"

We picked this because it's the smallest model that lets a VM provision in
one network round-trip and produces a single audit row per operation. It
trades step-level error attribution for speed and simplicity.

The shape we'd eventually want is something like
[`pyinfra`](https://pyinfra.com): declare desired state in Python, get
batched per-host shell commands and structured outputs. We didn't take the
dependency because:

- pyinfra is a substantial framework (operations, facts, connectors,
  gevent). It assumes a deploy-from-CLI workflow we don't have.
- We can grow a 200-line subset ourselves when the pain shows up.

When to revisit: when we have more than ~3 scripts that share large blocks
of "ensure file exists / ensure package installed" logic. At that point,
extract a tiny operations layer in Python, keep each operation idempotent,
keep the Task-per-script contract.

### Bootstrap mechanism — shell script today

Same shape as above. A Bash script is the smallest thing that works. When
servers grow distinct roles (compute, edge, builder) or when we want to
*declare* their state and reconcile it, we will build a small declarative
layer ourselves rather than take pyinfra.

When to revisit: when there are two genuinely different bootstrap paths,
or when an operator wants to make a small surgical change to a running
server without re-running the whole script.

### Address reuse on archive

Today, archived VMs hold their IPv6 address forever; new VMs always get a
fresh address from the /124. With a /124 (15 usable addresses) this caps
the lifetime number of VMs per server at 15.

This is acceptable for the building block but obviously not for production.
The next iteration moves to either:

- Larger usable subnets per server (talk to DO; or move off DO to a
  provider that routes the whole /64 to the droplet), or
- Reusing addresses with a quarantine window (Task audit gets a "this
  address was used by VM X 2026-05-01..2026-05-04" lookup).

### Host-key trust

We use `StrictHostKeyChecking=accept-new`. First connection is
trust-on-first-use. A compromised DigitalOcean control plane could swap a
droplet underneath us between bootstrap and first SSH. Fix is to capture
the host key during `Server.provision()` (right after droplet create, via
the DO API's serial console — or by reading the public key from the
droplet's `/etc/ssh/ssh_host_ed25519_key.pub` over the *first* SSH and
pinning it). Both add a field to `Server` and a one-time write. Not
breaking.

## Near-term hedges

Cheap structural changes to make **near-term** (not now — but before any
production load), because they're much more expensive to retrofit later than
to set up early. These are not on the lists above. None change current
behavior; they just keep doors open.

- **Secret indirection for SSH keys and provider tokens.** Keep the field on
  `Server Provider`, but route reads through a single helper so the storage
  backend can be swapped to an external secret store without touching
  callers. DB-as-keystore is fine for the PoC; not fine once customers exist.
- **SSH scripts call `sudo` explicitly.** No-op as `root` today, but turns
  the planned move to an unprivileged user (see below) from "rewrite every
  script" into "create the user."
- **Spill Task `stdout`/`stderr` over N KB to a file.** The Task row keeps a
  capped excerpt + a pointer. Avoids the DocType becoming a log store.

## Concrete next steps after this iteration

- **Stuck-task reaper**. A scheduled job that looks at Tasks in `Running`
  state older than 2× their declared timeout and marks them `Failure` with
  a synthetic "worker presumed dead" note. The e2e harness already does
  this via `mark_orphan_tasks_failure`; production needs the same
  guarantee. Pair with the "Server lock doctype" if we ever want
  concurrent-sync protection. Additive.

- **Server lock doctype**. A single-row lock keyed by `(server, resource)`
  that long-running mutating Tasks (sync-image, provision) take before
  doing work. Today two concurrent syncs of the same image-on-server are
  a benign race that wastes bandwidth; with more operators it stops being
  benign. Additive.

- **Unprivileged user on the server**. Move from `root` to an `atlas` user
  with `sudo` on a narrow allowlist. Then drop `sudo` for the Firecracker
  binary in favor of the **jailer**. Touches `Server Provider` (the user
  the SSH key is for) and the wrapper that prepends `sudo`. Not breaking.

- **Host-key pinning**. See above.

- **CLI**. A small `atlas` CLI that calls Frappe's REST API. The DocType
  methods we expose for buttons become the CLI's commands. Pure additive.

- **Bare-metal provider**. A second `provider_type` that doesn't call DO —
  the operator enters IP, SSH key, region. Provisioning becomes "type in
  what already exists". Additive.

- **Multi-arch**. Drop the `ARCHITECTURE` hard-coding; allow `aarch64`. The
  Firecracker CI publishes aarch64 artifacts. Additive on
  `Server` and the image record.

## Things on the longer-term list

- **Custom images** (`Virtual Machine Image Build`): build an ext4 from a
  Dockerfile or debootstrap recipe, push to a bucket, point the image
  record at it. Additive.

- **Overlayfs-backed rootfs**: shrink per-VM disk by ~10×. Internal to
  `provision-vm.sh` and `delete-vm.sh`. Additive.

- **Snapshots**: Firecracker supports them. Adds a state and a DocType.
  *Breaking* for code that pattern-matches the current state machine —
  treat the status field as an open set when you write checks today.

- **Health checks**: a scheduled job that runs `systemctl is-active …` per
  VM and reconciles `Virtual Machine.status`. Additive.

- **Metrics**: `firecracker --metrics-path` per VM, shipped to whatever
  metrics store the next layer cares about. Additive.

- **Console access**: signed URL to the serial console via the API socket.
  Needs a small web service. Additive.

- **Quotas / ownership / scheduling**: belongs in the layer above Atlas.
  Atlas gains a `team` field on resources but stays unaware of policy.

## Things we will not do, regardless

- Build our own hypervisor.
- Build a portal. Desk and a future CLI cover what we need.
- Adopt Kubernetes.
- Multi-tenant secrets management in this app.

## Changes

- `v0.1` — initial spec.
- `v0.2` — renamed `Metal Node`→`Server`, `Metal Command`→`Task`,
  `VM Image`→`Virtual Machine Image`. Switched from paramiko to system
  `ssh`. One Task = one shell script. Bumped Firecracker to v1.15.1.
  Documented the DigitalOcean /124 routing constraint. VMs are now UUIDs
  and keep their name on archive. Shell scripts live in `atlas/scripts/`,
  not embedded in markdown.
