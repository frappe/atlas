# Docker compatibility — `docker run` against Atlas microVMs

> **Status: design / proposal.** Nothing here is built yet. This chapter
> analyses what it takes to make Atlas answer the Docker Engine API so a
> Docker-literate customer runs `docker -H {atlas} run …` / `docker compose up`
> and each "container" is actually a **Firecracker microVM**. It phases the work
> and marks every place the container↔VM analogy leaks.

## The magic moment

```
export DOCKER_HOST=tcp://edge.blr.atlas.example:2376   # (mTLS)
docker run -d -p 8080:80 --name web nginx
docker compose up
```

…and a microVM boots per service, joins the customer's VPC (spec/25), and the
published port is reachable — with **no Atlas-specific concepts** in the
customer's hands. They keep their muscle memory; we run VMs.

## Why Atlas is already 80% of the way there

The hard, proven infrastructure a "containers-as-microVMs" product needs
**already exists** in Atlas and is verified on real hosts. The Docker layer is a
thin **translation shim**, not a new platform:

| Docker needs | Atlas already has | Where |
|---|---|---|
| Instant image → writable rootfs | CoW thin-LV snapshot of a read-only base image LV (`lvcreate -s`), "instant, not a copy" | [08-images.md](./08-images.md), `provision-vm.py` |
| Per-request container create | `create_vm` whitelisted API → Pilot/VM, get-or-create Tenant | [api/provision.py](../atlas/atlas/api/provision.py) |
| Container network / VPC | Per-tenant `fdaa::/48` WireGuard host mesh, one VPC per tenant, nftables isolation + anti-spoof | [25-private-networking.md](./25-private-networking.md) |
| Publish a port (`-p`) | Reserved IP attach (host 1:1 NAT, DNAT in / SNAT out) + per-VM firewall | [06-networking.md](./06-networking.md), [20-firewall.md](./20-firewall.md) |
| Container lifecycle (start/stop/rm/pause) | The full VM lifecycle, 1:1 with docker verbs | [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md), [docker-command-map.md](../llm/docker-command-map.md) |
| Identity injection at create | `inject_identity` writes hostname/keys/env into the rootfs at provision | `scripts/lib/atlas/rootfs.py` |
| Fast boot | Warm-snapshot fan-out (resume a frozen guest in low seconds) | [05](./05-virtual-machine-lifecycle.md) *Warm snapshot fan-out* |
| `exec`/`logs` transport | SSH-in-the-mesh, `SSH Console`, per-VM guest SSH | [04-tasks.md](./04-tasks.md) |

**What is genuinely new** (the whole of this chapter's build):

1. An **OCI image → ext4 base-LV converter** (the `docker pull` / `docker build` path).
2. A **minimal guest init** that presents the Docker *runtime contract*
   (ENTRYPOINT/CMD/ENV/WORKDIR/USER, the process is PID-adjacent, stop signals)
   inside a full VM.
3. A **Docker Engine API server** (the proxy) that speaks the subset of the
   `/v1.4x/…` REST API real clients use and maps each call to a controller method.
4. `exec` / `logs` / `attach` riding SSH — the muscle-memory verbs Atlas lacks today.

Everything else is wiring.

## Component inventory — what we write

### A. The Docker Engine API proxy (`dockerd`-shaped front door)

The Docker CLI and `docker compose` are just HTTP clients of the **Docker Engine
API** (a documented REST API, `/v1.43/containers/create`, `/images/create`,
`/exec`, …). `DOCKER_HOST=tcp://…` points them at any server that answers it.
We write that server.

- **Runs as a VM, not on the host.** Per the spec-25 invariant *"the internet
  touches VMs, never hosts"*, the Docker API endpoint is a new **infra VM role**
  — the sibling of the reverse proxy, TCP proxy, and customer gateway. Call it
  the **Engine VM** (`Virtual Machine.is_engine`), one per region, a fixed
  reserved public v4 on `:2376`, terminating **mTLS** (Docker's native client-cert
  auth — the customer runs `docker --tlsverify`).
- **Stateless translator.** It authenticates the client cert → resolves the
  `Tenant`, then translates each Engine API call into a **whitelisted Atlas
  controller method** over Frappe's HTTP API (the same seam Central uses). It
  invents no state and calls **no host scripts** — controllers own state
  transitions (docker-command-map.md §6.6).
- **Separate service, not inside Frappe.** The user's instinct is right: a
  standalone proxy (Go or Python/`asyncio`) is far cleaner than teaching Frappe
  to emit the Engine wire format. It is a **client** of Atlas, exactly like
  Central. It holds no DB; Atlas stays the source of truth.

### B. OCI image → Atlas base image (`docker-image-import`)

The inverse of today's Ubuntu-cloud-image path. A new host script
`import-oci-image.py` + a `Virtual Machine Image` origin (a **third** origin
beside URL and snapshot-promote):

1. Pull the OCI image (manifest + layers) from a registry — Docker Hub, GHCR, a
   customer registry — into a scratch dir on the host (reuse the auth/registry
   client; this is `docker pull`).
2. **Flatten** the layers (apply each tar in order, honoring whiteouts) into a
   single rootfs tree — a plain `tar` extract loop, no containerd needed.
3. **Graft the microVM contract onto it**: this is the twin of
   `sync-image.py`'s *Image normalization*. A container rootfs has **no init, no
   kernel, no fstab, no networking, no sshd** — a VM needs all of them. So the
   converter injects:
   - the shared Atlas **kernel** (`vmlinux` — the base image's kernel is *free*,
     reused byte-for-byte like promote does; the container image ships none);
   - the **minimal init** (component C) as PID 1, or a systemd unit that runs it;
   - `atlas-network.service` + `/etc/atlas-network.env` (the same guest network
     unit every Atlas VM gets — spec/06 + spec/25);
   - an sshd for `exec`/`logs` (key-only, injected per-VM at provision — the
     existing contract);
   - `/etc/fstab` (`LABEL=atlas-root /`), machine-id, hostname (provision-time).
4. **Record the OCI `config`** (ENTRYPOINT, CMD, ENV, WORKDIR, USER, EXPOSE,
   STOPSIGNAL, volumes) — this is the runtime contract the minimal init consumes.
   Store it on the image row (a new `oci_config` JSON field) so a VM created from
   this image knows what to run without re-reading the image.
5. `dd` the ext4 into a **read-only thin base LV** `atlas-image-<name>` — from
   here it is **identical to every other Atlas base image**: per-VM disk is an
   instant CoW snapshot, kernel is on the host, provision injects identity. The
   entire downstream lifecycle is unchanged.

**This is the keystone.** Once an OCI image is a normal `atlas-image-*` base LV
with a recorded `oci_config`, `docker run` collapses into the existing
`create_vm` path. No new provisioning machinery.

> **Layer caching, later.** v1 flattens per image (like today's per-image ext4).
> A content-addressed layer cache (dedup shared base layers across images, a real
> registry mirror) is a Phase-4 optimisation, not a correctness requirement.

### C. The minimal guest init (the Docker runtime contract in a VM)

A container's PID 1 *is* the workload (`ENTRYPOINT`+`CMD`); a VM's PID 1 is an
init that boots an OS. We bridge that with a tiny init/supervisor
(`atlas-container-init`, a small static binary or a systemd unit) that:

- reads the **recorded `oci_config`** (injected to the guest at provision, the
  same way `atlas-network.env` is — via `inject_identity`);
- sets up the process the way a container runtime does: `WORKDIR`, `ENV`,
  drop to `USER`, apply the entrypoint/cmd;
- runs it as a supervised service, restart policy honoring
  `--restart` (maps to the systemd unit's `Restart=`);
- captures stdout/stderr to the journal (so `docker logs` = `journalctl -u`);
- forwards `docker stop`'s signal (default SIGTERM → STOPSIGNAL, then SIGKILL
  after the grace period) to the workload — **this closes the `docker kill` gap**
  the command-map flags (§1);
- exits/reports status so the "container" has a meaningful exit code.

We deliberately keep a **thin OS underneath** (networking unit, sshd, journald)
rather than pid-1-is-the-app: it is what makes `exec`, `logs`, VPC networking,
and the whole Atlas lifecycle work unchanged. The leak we accept and document:
an Atlas "container" is a **microVM with one supervised main process**, not a
single-process namespace. For "common tasks" (run a service, a web app, a
worker) this is invisible; for "esoteric" ones (sharing a PID namespace,
`--pid=host`, ultra-minimal `FROM scratch` with no libc) it is not Docker.

### D. `exec` / `logs` / `attach` / `cp` over SSH

The command-map's §3 gap and its own recommendation. Each is a controller method
+ an Engine API route, riding the **guest SSH-in-the-mesh** transport that
already exists:

- `docker exec` → open an SSH session/`exec` channel into the VM (over the mesh),
  run the command in the container-init's environment.
- `docker logs [-f]` → `journalctl -u atlas-container [-f]` streamed back.
- `docker attach` → attach to the main process's tty via the init.
- `docker cp` → `scp`/`sftp` over the same channel.

These are the verbs a Docker user reaches for in minutes; they turn "feels
like Docker" from a demo into a workflow.

## Networking — `-p`, `--network`, compose service DNS

Docker's network model maps cleanly onto Atlas's existing layers, **one VPC per
tenant** exactly as the user asked:

- **`docker run -p 8080:80` (publish).** Attach a **Reserved IP** to the VM and a
  per-VM firewall rule mapping the public port → guest port (spec/06 ingress +
  spec/20 firewall). This is the existing DNAT-in/SNAT-out primitive with a port
  map. For v6-only publish it is just a firewall rule (no reserved IP needed).
- **`--network` / inter-service comms.** Every VM in a tenant is already on the
  tenant's `fdaa::/48` **VPC** (spec/25), reachable by its stable private
  address — internal service-to-service traffic needs **zero** extra work. A
  compose project's services all share the tenant VPC by construction. We do
  **not** build Docker user-defined networks; the tenant `/48` *is* the network.
  (Multiple compose "networks" within one tenant collapse to the one VPC — a
  documented leak; it satisfies "internal communication" without per-network
  isolation.)
- **Service DNS (`web` resolves to the web VM).** Compose relies on resolving a
  service name to its container IP. This is exactly the deferred **spec/25 Phase 4
  `<vm>.<tenant>.internal` resolver** — bring it forward: register each VM's
  compose service name → its `fdaa::` address in a per-tenant resolver the VMs
  point at. Until then, inject `/etc/hosts` entries at create time (the compose
  project's service map is known up front).
- **Public + private together.** The user's requirement — "allow private networks
  along with public for internal communication" — is Atlas's **default**: a VM
  has a public `/128` *and* its tenant `fdaa::` address. A `docker run` without
  `-p` gets a VPC-only (effectively `public_networking` still on for egress but no
  ingress) VM; with `-p` it gets a published port. A `--internal` service maps to
  a **dark VM** (spec/25, `public_networking=0`), reachable only inside the VPC.

## How much Docker compatibility — honest scope

Targeting **common tasks, not esoteric commands** (the user's framing):

### Works well (the 80%)
- `docker run` (detached, `-e`, `-p`, `--name`, `--restart`, `-v` for a data disk,
  `-m`/`--cpus` → VM size), `create`, `start`, `stop`, `restart`, `rm`, `pause`,
  `unpause`, `kill`, `ps`, `inspect`, `logs`, `exec`, `images`, `pull`, `rmi`.
- `docker compose up/down/ps/logs` for the common shape: N services, a shared
  network (the tenant VPC), published ports, env, volumes, depends_on.
- Each maps to an existing controller method (command-map §7) or one of the four
  new pieces above.

### Leaks we document (the analogy's edges)
- **One main process per VM**, not a process namespace (component C).
- **`-v` bind-mounts from the client host** can't work (there is no shared
  filesystem) — named volumes map to an Atlas **data disk** (spec/05); host
  bind-mounts are rejected with a clear error.
- **`--network host`, `--pid host`, `--privileged`, capabilities, `--gpus`,
  raw devices** — reject with a clear "not supported on Atlas" message.
- **Build.** `docker build` needs a builder. v1: reject and point at
  `import-oci-image` for pre-built images (or run buildkit on the Engine VM as a
  later phase). `docker compose`'s `build:` directive is the main gap here.
- **Boot latency.** A "container" is a VM boot. Mitigated hard by warm-snapshot
  fan-out (low-seconds resume, spec/05) — a per-image warm golden makes
  `docker run <that image>` feel container-fast. Cold boot is 1–2 min otherwise.
- **Sub-second ephemeral runs** (`docker run --rm alpine echo hi`) are a poor fit
  — VM boot dominates. Fine for services; wrong tool for one-shot CLI fan-out.

## Build in phases

Each phase is independently shippable and demoable. Earlier phases don't depend
on later ones.

**Phase 0 — OCI import (no Docker API yet).**
`import-oci-image.py` + the image row `oci_config` field + the third image
origin. Deliverable: an operator runs "Import OCI Image" (`nginx:latest`) in
Desk, it becomes an `atlas-image-nginx` base LV, and a normal `create_vm`
against it boots a VM **running nginx** via the minimal init (component C, built
here — it is what makes the imported image actually *run*). Proves the keystone
end to end with the existing lifecycle. No proxy.

**Phase 1 — minimal `dockerd` proxy: single-container lifecycle.**
The Engine VM + mTLS + the container lifecycle subset: `create`/`run`/`start`/
`stop`/`rm`/`ps`/`inspect`/`images`/`pull`(→ Phase-0 import)/`rmi`. Client cert →
Tenant. Deliverable: `docker -H {atlas} run -d nginx` boots a VPC'd microVM;
`docker ps`/`stop`/`rm` work. This is the first "it feels like Docker" moment.

**Phase 2 — the muscle-memory verbs + publish.**
`exec`, `logs -f`, `attach`, `cp` over SSH (component D); `-p` publish via
Reserved IP + firewall; `-v` named volume → data disk; `--restart`/`--name`/
`-e`/`-m`/`--cpus`. Deliverable: a real single-service workflow — run it, publish
it, tail its logs, exec in, restart it. The point where a Docker user stops
missing the CLI.

**Phase 3 — `docker compose up` (the headline).**
Multi-service: the proxy accepts a compose project (compose talks the same Engine
API — create N containers + a network), places each service as a VM in **one
tenant VPC**, wires `depends_on` ordering, injects service-name DNS (bring
spec/25 Phase 4 forward, or `/etc/hosts` seeding). Deliverable:
`docker compose up` on a typical web+worker+redis compose file brings up three
VMs that reach each other by service name over the VPC. **This is the demo.**

**Phase 4 — polish & scale.**
Layer-cached / mirrored registry (dedup base layers), warm-golden-per-image for
container-fast `run`, `docker build` via buildkit on the Engine VM, the
`.internal` resolver as a first-class service, `docker stats` → the dashboard
metrics, `docker network`/`docker volume` management verbs.

## Proxy vs. Frappe-native — decided

Build a **standalone Engine-API proxy** (the user's "looks better/easier"
instinct), not an in-Frappe implementation:

- It is a **client** of Atlas's existing whitelisted methods (the Central seam),
  so it inherits state-transition safety, Tasks, and auditing for free.
- It keeps the Docker wire format (chunked streams, hijacked connections for
  `exec`/`attach`, mTLS) out of Frappe's request cycle, which is a poor fit for
  long-lived streaming connections.
- It runs as an **Engine VM** (spec's infra-VM-on-the-mesh pattern), so it gets
  VPC reachability to every tenant VM (for `exec`/`logs`) and the
  "internet-touches-VMs-never-hosts" posture for free.
- Frappe stays the source of truth; the proxy is stateless. Same division of
  labour as Central ([16-central.md](./16-central.md)).

## Open questions for the operator

1. **Tenant ↔ Docker client identity.** One mTLS client cert per tenant (issued
   by Central at signup)? That is the clean mapping: the cert *is* the tenant.
2. **Registry auth passthrough.** For private images, does the customer's
   `docker login` credential flow through the proxy to the upstream registry, or
   do we mirror into an Atlas-side registry per tenant?
3. **Compose network semantics.** Confirm collapsing all compose networks in a
   project to the one tenant VPC is acceptable for v1 (it satisfies "internal
   communication" but drops per-network isolation *within* a tenant).
4. **`docker build`.** Is Phase-0 "import pre-built images only" enough for the
   pilot, or is `compose build:` needed early (pulling buildkit forward)?
