# Atlas ↔ Docker command map

Goal: give Atlas a command vocabulary that *feels* like Docker so a
Docker-literate operator can guess the verbs. **Not a strict mapping** —
Atlas runs Firecracker microVMs, not containers, so several concepts don't
line up. This document maps the *real* Atlas command surface (as it exists
in the code today) onto Docker's, and flags every place the analogy leaks.

The Atlas surface has three layers, all backed by the same operations:

- **Desk buttons** — what an operator clicks on a DocType form (`*.js`).
- **Controller methods** — `@frappe.whitelist()` methods on DocTypes.
- **Host task scripts** — `scripts/*.py`, invoked over SSH as Tasks.

Docker collapses all three into one CLI. The tables below use the Atlas
*controller method / desk button* as the canonical verb, and name the host
script that actually does the work.

---

## 1. Container lifecycle — the clean part

Atlas VMs map almost 1:1 onto `docker` container verbs.

| Docker | Atlas verb | Host script | Notes |
|---|---|---|---|
| `docker create` | `create_vm` (API) | `provision-vm.py` | Creates the VM record + Pilot; provisions in background |
| `docker run` | `create_vm` → `provision` | `provision-vm.py` | Docker fuses create+start; Atlas splits record-create from boot |
| `docker start` | `VM.start` | `start-vm.py` | Resumes from memory snapshot if the last stop captured one |
| `docker stop` | `VM.stop` | `stop-vm.py` / `snapshot-stop-vm.py` | `stop(memory_snapshot=True)` = stop **+ checkpoint** (see §6.2) |
| `docker restart` | `VM.restart` | stop + start | `restart(cold=True)` forces a full reboot vs warm |
| `docker pause` | `VM.pause` | `pause-vm.py` | Freezes vCPUs via Firecracker API socket |
| `docker unpause` | `VM.resume` | `resume-vm.py` | ✓ near-exact |
| `docker rm` | `VM.terminate` | `terminate-vm.py` | Blocked by termination protection flag |
| `docker kill` | — | — | **Gap.** No SIGKILL analog; `stop` takes `stop_timeout_seconds` but no hard-kill verb |
| `docker update` | `VM.resize` | `resize-vm.py` | VM must be **Stopped**; disk is grow-only. Docker updates limits live |
| `docker rename` | — | — | No rename verb; VM title is set at create |

Docker has no analog for these VM-native verbs:

| Atlas verb | Host script | What it does |
|---|---|---|
| `VM.rebuild` | `rebuild-vm.py` | Replace a Stopped VM's disk from a base image or snapshot, keeping identity (IP, host keys) |
| `VM.regenerate_host_keys` | `regenerate-host-keys-vm.py` | Rotate SSH host keys |
| `VM.migrate` | `migration-*.py` (13 scripts) | Live-migrate the disk to another host, keeping the /128 address (see §5) |
| `VM.collapse_forward` | `migration-forward-down.py` | Abort a keep-address migration, fall back to change-address |

---

## 2. Images

Atlas images are LVM base volumes shipped host-to-host over NBD — there is
**no registry**, so Docker's push/pull grammar inverts.

| Docker | Atlas verb | Host script | Notes |
|---|---|---|---|
| `docker commit` | `Snapshot.promote_to_image` | `promote-snapshot-image.py` | Two hops: snapshot the VM, then promote the snapshot to a base image |
| `docker build` | `Image Build.rebake` / Bake Image | (bakes via a provisioned VM) | No Dockerfile — Atlas builds by provisioning a VM and baking it |
| `docker images` | Virtual Machine Image list | — | ✓ |
| `docker rmi` | `Image.archive` | — | Sets `is_active=0`; row stays (soft delete) |
| `docker pull` | — | `migration-receive-base.py` | **Inverted.** No central registry to pull *from* |
| `docker push` | `Image.sync_to_server` / `sync_to_all_servers` | `sync-image.py`, `migration-export-base.py` | Push host→host; the sender serves the image over NBD |
| `docker tag` | — | — | No tag/alias layer; images are named at creation |
| `docker save` / `load` | `Image Export` (`retry`) | `migration-export-base.py` / `migration-receive-base.py` | Export/import a base image between hosts over NBD |
| `docker history` | — | — | No layer history — images are flat LVM volumes |

Snapshot verbs (no direct Docker analog; closest is `docker checkpoint`):

| Atlas verb | Host script | What it does |
|---|---|---|
| `VM.snapshot` | `snapshot-vm.py` | Cold LVM thin CoW snapshot of a Stopped VM's disk |
| `VM.snapshot(live=True)` | `snapshot-vm.py` | Snapshot disk of a Running VM |
| `VM.capture_warm_snapshot` | `warm-snapshot-vm.py` | Memory **+** disk at one paused instant (golden warm image) |
| `Snapshot.clone_to_new_vm` | `provision-vm.py` (clone) | Seed a **new** VM's disk from a snapshot |
| `Snapshot.restore_to_vm` | `rebuild-vm.py` | Roll a VM back onto its own snapshot in place |
| `delete-snapshot-vm.py` | `delete-snapshot-vm.py` | Delete a snapshot LV |

---

## 3. Inspection / interaction — the biggest gap

These are the commands people reach for constantly. Atlas has weak or no
first-class equivalents, and that's what breaks the "feels like Docker"
illusion fastest.

| Docker | Atlas | Status |
|---|---|---|
| `docker exec` | `Server.run_task_dialog` / SSH Console `execute` | **Host-scoped, not VM-scoped.** `run_task_dialog` runs on the *host*; SSH Console fans out over SSH. No `atlas exec <vm>` |
| `docker logs` | — | **Gap.** No log-tail verb |
| `docker inspect` | (open the DocType form) | Data exists in the DocType, but no `inspect` command |
| `docker stats` | dashboard live `/proc` metrics | Dashboard-only; no CLI `stats` |
| `docker ps` | `tenant_vms` (API) / VM list | `tenant_vms` lists tenant VMs; no unfiltered `ps` verb |
| `docker cp` | — | **Gap.** No file-copy verb |
| `docker top` | — | **Gap** |
| `docker attach` | — | **Gap** (SSH into the mesh instead) |
| `docker port` | Firewall / Reserved IP (see §4) | Port exposure is firewall+NAT, not a `port` readout |

---

## 4. Networking

Docker's network model (`docker network`, `-p`, `--link`) maps onto Atlas's
firewall + reserved-IP + tunnel layer, but the shapes differ.

| Docker | Atlas verb | Host script | Notes |
|---|---|---|---|
| `docker run -p` (publish) | `Reserved IP.attach` | `vm-reserved-ip.py` | Attach a public v4 via host 1:1 NAT. VM inbound is otherwise v6-only |
| firewall / `--publish` filtering | `set_firewall` / `Firewall.sync` | `firewall-apply.py` | Per-VM public-ingress rules; empty = deny-all |
| `docker network create` | — (Reserved IP `allocate`/`discover`) | — | No user-defined networks; Atlas has host mesh + tenant prefixes |
| `docker network connect` | `request_tunnel` / `VPN Tunnel.bring_up` | `vm-tunnel.py` | WireGuard tunnel to a VM for a client key |
| — | `tunnel_up` / `tunnel_down` | `tunnel-up.py` / `tunnel-down.py` | Atlas↔Central spoke tunnel (control plane, no Docker analog) |
| — | `Reserved IP.reassign` / `release` | `vm-reserved-ip.py` | Move/destroy a reserved public IP |

Management-plane firewall (`mgmt-firewall-*.py`, `provision_tunnel` /
`confirm_tunnel` / `deprovision_tunnel`) has **no Docker analog** — it's the
host's own control-plane lockdown with armed auto-revert.

---

## 5. Migration — no Docker analog at all

Docker has nothing here. Atlas live-migrates a VM's disk between hosts while
keeping its public /128 address. Surfaced as one verb (`VM.migrate`,
`Migration.retry`) but backed by 13 host scripts:

`migration-export-source`, `migration-export-base`, `migration-clone-target`,
`migration-receive-base`, `migration-poll-hydration`, `migration-inject-identity`,
`migration-cutover-target`, `migration-cleanup-source`, `export-cleanup-source`,
`migration-forward-up/-down`, `migration-source-forward`, `migration-target-receive`.

Closest Docker concept is `docker checkpoint` + a manual `save`/`load` across
hosts — but that's cold and loses the address. Atlas keeps it live and keeps
the IP.

---

## 6. Inconsistencies to resolve before shipping an `atlas` CLI

If the goal is a Docker-familiar CLI, these are the mismatches worth a
decision. Ranked by how much they'll confuse a Docker user.

### 6.1 No `exec` / `logs` / `inspect` / `cp`
The single biggest gap (§3). A Docker user reaches for `exec` and `logs`
within minutes. Options: (a) add `atlas exec <vm>` that SSHes into the VM
over the mesh; (b) add `atlas logs <vm>` tailing the guest journal; (c)
accept the gap and document "SSH in instead." Recommend at least `exec` +
`logs` — they're the muscle-memory verbs.

### 6.2 `stop` silently checkpoints
`VM.stop(memory_snapshot=True)` is `docker stop` **fused with**
`docker checkpoint`. A naïve `atlas stop` should default to a plain stop;
gate the suspend-to-disk behind an explicit flag (`atlas stop --checkpoint`)
so it doesn't surprise anyone.

### 6.3 `run` is split, `rm` is not
`docker run` = `create_vm` + `provision` (two API calls); `docker rm` = one
`terminate`. Either fuse create+provision behind a single `atlas run`, or
lean on Docker's own `create` vs `run` distinction and expose both.

### 6.4 Push-only image model, no registry
`sync_to_server` pushes host→host; there's no registry, so `docker pull` and
`docker tag` have no home (§2). Name this explicitly — "images are pushed,
not pulled" — or a Docker user will hunt for a registry that isn't there.

### 6.5 `resize` requires Stopped; Docker `update` is live
`docker update` changes limits on a running container. `VM.resize` requires
the VM to be **Stopped** and only grows disk. Document the precondition or
the command will feel broken.

### 6.6 Three surfaces, one operation
Desk button, controller method, and host script are three names for the same
verb (e.g. Stop → `VM.stop` → `stop-vm.py`). A CLI should map to the
**controller method** layer, never call host scripts directly — the
controllers own state transitions, precondition checks, and Task enqueueing.

---

## 7. Suggested `atlas` CLI verb set (Docker-shaped)

Only verbs that map to a real controller method today. Nothing here invents
behavior.

```
atlas run       → create_vm + provision        atlas ps       → tenant_vms
atlas start     → VM.start                      atlas images   → Image list
atlas stop      → VM.stop  (--checkpoint)       atlas rmi      → Image.archive
atlas restart   → VM.restart (--cold)           atlas commit   → Snapshot.promote_to_image
atlas pause     → VM.pause                       atlas push     → Image.sync_to_server
atlas unpause   → VM.resume                      atlas save     → Image Export
atlas rm        → VM.terminate                   atlas migrate  → VM.migrate   (Atlas-native)
atlas update    → VM.resize (Stopped only)       atlas snapshot → VM.snapshot  (Atlas-native)
```

Gaps to decide on (no backing method yet): `exec`, `logs`, `inspect`,
`cp`, `top`, `kill`, `pull`, `tag`.
