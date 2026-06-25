# The Central-managed tunnel — isolating the Atlas management plane

Today a regional Atlas is reachable on the public internet at `base_url` (e.g.
`https://blr.atlas.example.com`). Anyone who can reach `base_url` can hit Atlas's
Desk, its whitelisted API, and its public guest-signup endpoint. This document
specifies how each Atlas **management plane** is locked down so it is reachable
**only by Central**, over an encrypted private network, with Atlas firewalling its
public interface.

The **data plane is unaffected.** Customer traffic flows through **proxy VMs on
Servers** — each proxy VM attaches its own public Reserved IP
([06-networking.md § IPv4 ingress](./06-networking.md#ipv4-ingress-reserved-ip))
— not through the Atlas controller host. Locking down the Atlas host's public
interface does not touch a single hosted site: a customer reaching
`https://app.customer.example` still lands on a proxy VM's Reserved IP, which
routes to the bench VM over IPv6. Only the **operator/Central control surface**
(Desk, the provisioning API, signup) moves behind the tunnel.

## Threat model and posture

- **Before:** Atlas's Desk + API + guest signup are on the public internet at
  `base_url`. The only protection is Frappe auth. A credential leak, an auth-bypass
  bug, or an unauthenticated signup-endpoint flaw is internet-reachable.
- **After:** Atlas's host runs **default-deny** on its public interface. The *only*
  inbound packet the public firewall accepts is the WireGuard UDP handshake on one
  port. Frappe HTTP/HTTPS **and** SSH are reachable **only over `wg0`** — i.e. only
  from Central, the single WireGuard hub. Atlas's *outbound* traffic is unrestricted
  (event reporting to Central, vendor APIs, package mirrors keep working).

The guest signup surface and every other public Atlas endpoint go private with the
rest of the management plane — there is no special-casing
([14-self-serve.md](./14-self-serve.md) signup runs behind the tunnel like
everything else).

## Topology — WireGuard, Central is the hub

We use **WireGuard**. **Central's Frappe host is the hub**; each Atlas is a spoke.

```
            wg0 10.88.0.1/16  (UDP 51820)
        ┌───────────────────────┐
        │   Central (the hub)   │
        └───────────┬───────────┘
                    │ hub dials each spoke's public UDP :51820
       ┌────────────┼─────────────┐
       │            │             │
  10.88.0.2/32  10.88.0.3/32  10.88.0.4/32
   Atlas blr     Atlas fra     Atlas sgp
  (public IP,   (public IP,   (public IP,
   wg :51820)    wg :51820)    wg :51820)
```

- **Tunnel CIDR:** `10.88.0.0/16`. The hub is `10.88.0.1/16` on `wg0`. Each Atlas
  is a single `/32` peer (`10.88.0.2`, `10.88.0.3`, …), allocated sequentially from
  the pool by Central.
- **Ports:** hub `wg0` listens on UDP **51820**; each Atlas `wg0` listens on UDP
  **51820** on its public IP. (Same port number, different hosts.)
- **Who dials:** the **hub dials the spoke.** Atlas keeps a stable public IP and its
  `wg` listens on the public UDP port — and that UDP port is the **only** thing its
  public firewall allows in. The hub holds each Atlas's public endpoint
  (`<atlas_public_ip>:51820`) and `persistent-keepalive 25`, so it establishes and
  keeps the session alive. The spoke is configured with the hub as a peer
  (`allowed-ips` = the tunnel CIDR) and the hub's public endpoint, so either side can
  re-handshake.
- **Addresses on the tunnel:** once up, the hub reaches an Atlas at its `tunnel_ip`
  (e.g. `https://10.88.0.2`), and the Atlas reaches Central at the hub's public URL
  for event reporting (outbound, unaffected by the firewall).

This is the inverse of the provider relationship and reuses the same idioms as the
rest of Atlas: privileged host work runs as **sudoers-pinned scripts** through the
existing Task runners, and the nft ruleset mirrors
[`reserved_ip_nat.py`](../scripts/lib/atlas/reserved_ip_nat.py).

## Reversed registration — Central orchestrates

We **reverse** the registration direction. Today Atlas pushes `register` to Central
([16-central.md](./16-central.md)). From now on the operator feeds **Central** an
Atlas instance's **admin API key/secret** + `base_url` + region, and Central
orchestrates the whole thing — tunnel, firewall, identity — from its side.

The operator does this on Central; the Central side of the flow is specced in
[central/spec/TUNNEL.md](../../central/spec/TUNNEL.md). The Atlas side exposes the
inbound surface Central drives, below.

### Sequence

```
Operator → Central:  admin api_key/secret + base_url + region
Central:             allocate tunnel_ip; generate atlas_id; create scoped service user
Central → Atlas:     provision_tunnel(...)        [over public base_url, admin auth]
Atlas:               tunnel-up.py → mgmt-firewall-apply.py (auto-revert ARMED) → write Central Settings
Atlas → Central:     { wg_public_key, listen_port, tunnel_ip }
Central (hub):       hub-peer-add.py  (Atlas pubkey + endpoint)
Central → Atlas:     ping at tunnel_ip            [over wg0 — proves reachability]
Central → Atlas:     confirm_tunnel()             [over wg0]
Atlas:               mgmt-firewall-confirm.py (cancel auto-revert, persist) → tunnel_status=Active
Central:             tunnel_status=Active; data path switches to tunnel_url
```

The **auto-revert** is the safety spine: `mgmt-firewall-apply.py` arms a timer that
restores the prior (open) ruleset and tears the tunnel after N seconds **unless
`confirm_tunnel` cancels it first**. A failed handoff therefore can never
permanently lock Central — or the operator — out. See *Lockout safety* below.

## Atlas inbound API — `atlas/atlas/api/central_link.py`

The surface Central drives during registration. **Authn = the Atlas admin token
(System Manager only).** These are the only new inbound methods.

| Method | Auth / transport | Payload | Returns |
| --- | --- | --- | --- |
| `provision_tunnel(**payload)` | admin token, over public `base_url` | `atlas_id`, `hub_public_key`, `hub_endpoint`, `tunnel_ip`, `tunnel_cidr`, `central_url`, `service_api_key`, `service_api_secret` | `{ wg_public_key, listen_port, tunnel_ip }` |
| `confirm_tunnel()` | admin token, **over the tunnel** | — | `{ tunnel_status }` |
| `tunnel_status()` | admin token | — | `{ tunnel_status, tunnel_ip, wg_public_key, wg_listen_port }` |

- **`provision_tunnel`** runs `tunnel-up.py` (generate the Atlas keypair locally if
  absent; bring up `wg0`), then `mgmt-firewall-apply.py` with the auto-revert armed, then
  writes the pushed Central service-user creds and tunnel parameters into
  `Central Settings`. It returns the Atlas **public key** + listen port (so the hub
  can add the peer) and the `tunnel_ip` it bound.
- **`confirm_tunnel`** is called by Central *over the tunnel* (proving end-to-end
  reachability): it runs `mgmt-firewall-confirm.py` (cancel the auto-revert, persist the
  locked ruleset) and flips `tunnel_status` to `Active`.
- **`tunnel_status`** is a read-back for diagnostics.

`provision_tunnel` is idempotent: re-running it re-asserts `wg0` and the firewall
(the keypair is generated only if absent, so the Atlas public key is stable across
re-runs).

## Atlas host scripts — `atlas/scripts/`

New sudoers-pinned scripts, invoked via `run_local_task`
([local_task.py](../atlas/atlas/local_task.py)), mirroring `reserved_ip_nat.py`'s
nft idiom and the `ATLAS_RESULT=` contract
([04-tasks.md](./04-tasks.md)). Pure string/argv construction is unit-testable with
bare `python3 -m unittest`; only the apply/teardown functions touch the host.

- **`tunnel-up.py`** — generate the Atlas `wg` keypair **locally if absent** (private
  key never leaves the host, `0600`); write `wg0` (the assigned `/32`, the hub as a
  peer with the hub endpoint, `allowed-ips` = the tunnel CIDR); bring it up; enable
  `wg-quick@wg0` for reboot persistence. Emits the Atlas **public key** + listen port
  in its `ATLAS_RESULT`.
- **`tunnel-down.py`** — tear `wg0` down and disable the unit (the rollback path).
- **`mgmt-firewall-apply.py`** — load an nftables ruleset that on the public iface
  **drops all inbound except** the `wg` UDP port **plus an operator-configurable
  `public_allow_ports` list** (default **empty**), plus loopback, established/related,
  and ICMP; and **allows all on `wg0`**. Applies with an **armed auto-revert** (see
  below). Persisted via `nftables.service`.
- **`mgmt-firewall-revert.py`** — restore the prior ruleset (the rollback path; also what
  the armed timer fires).
- **`mgmt-firewall-confirm.py`** — cancel the armed auto-revert and persist the locked
  ruleset as the boot default.

### The public firewall ruleset

On the public interface, default-deny inbound. Accept only:

1. the WireGuard UDP port (`51820`) — the one thing that lets the hub dial in;
2. any port in `public_allow_ports` (default **empty** — the deliberate extension
   point, below);
3. loopback;
4. established/related;
5. ICMP / ICMPv6 (so the host stays diagnosable).

On `wg0`: **accept all** — Frappe HTTP/HTTPS and SSH are reachable here, and only
here. Nothing in this table touches the `inet atlas` table that carries the VM
data-plane NAT/forward rules ([06-networking.md](./06-networking.md)); the two are
independent.

### Lockout safety — armed auto-revert

`mgmt-firewall-apply.py` never commits a lockdown it can't undo unattended. It:

1. snapshots the current ruleset to a file;
2. loads the new (locked) ruleset;
3. **arms a one-shot revert** — a systemd transient timer (`systemd-run
   --on-active=Ns`) or `at` job — that runs `mgmt-firewall-revert.py` after **N seconds**
   (default 180), restoring the snapshot and tearing `wg0`.

`mgmt-firewall-confirm.py` **cancels** that timer and rewrites the persisted ruleset to
the locked one. So the only way the lockdown becomes permanent is an end-to-end
`confirm_tunnel` arriving **over the tunnel** — which proves Central can already
reach Atlas privately. If anything between `provision_tunnel` and `confirm_tunnel`
fails (the hub can't peer, the tunnel never comes up, the operator's terminal dies),
the timer fires and the host is public again. This is the guarantee that makes the
whole flow safe to drive remotely.

### Break-glass / `public_allow_ports`

v1 is **strict default-deny**: SSH is tunnel-only, `public_allow_ports` ships empty.
The list is the deliberate extension point — adding `22` (or any port) to it re-opens
that port publicly without code change, for operators who want an out-of-band SSH
break-glass. Not surfaced in the UI for v1. If `wg0` itself fails after a reboot and
`public_allow_ports` is empty, the host is reachable only via the provider's serial
console — the accepted v1 trade-off.

### Boot ordering — fail-closed, zero exposure window

The persisted (confirmed, locked) ruleset loads via `nftables.service` ordered
**`Before=network-pre.target`**, so the lockdown is in place **before any service is
reachable** — there is no window where Frappe is public on a reboot. `wg0` comes up
in normal networking via `wg-quick@wg0`; the nft rules reference `wg0` by `iifname`
(name-based), which loads fine **even before the interface exists**, so there is no
fragile cross-dependency and no boot error if the tunnel is slow to establish. Trade
-off accepted: if `wg0` fails to come up post-reboot, the host is reachable only via
serial console until it does (or via `public_allow_ports` if an operator opened one).

## Credentials — two directions

- **Central → Atlas (the admin path).** Central stores the operator-supplied **Atlas
  admin** key/secret (encrypted) and uses it for *all* Central→Atlas calls:
  `provision_tunnel` over the public `base_url` during bootstrap, then everything
  over the tunnel once Active.
- **Atlas → Central (event reporting).** At registration Central creates a
  **dedicated, scoped Central service user per Atlas instance** and **pushes** its
  key/secret into Atlas's `Central Settings` (via `provision_tunnel`). Atlas reports
  events to Central authenticated as that user. These creds are **no longer
  hand-entered** on Atlas — they arrive in the provision payload.

## Central Settings — Tunnel section

`Central Settings` (single, [16-central.md § DocTypes](./16-central.md#doctypes))
gains a Tunnel section, all set by `provision_tunnel` (not hand-entered):

| Field | Meaning |
| --- | --- |
| `tunnel_ip` | this Atlas's `/32` on `wg0` (e.g. `10.88.0.2`) |
| `tunnel_cidr` | the tunnel CIDR (`10.88.0.0/16`) |
| `hub_public_key` | the hub's WireGuard public key (the peer) |
| `hub_endpoint` | the hub's public `ip:port` |
| `wg_public_key` | this Atlas's own WireGuard public key (read-back) |
| `wg_listen_port` | this Atlas's `wg` UDP listen port (`51820`) |
| `tunnel_status` | `Inactive` → `Provisioning` → `Active` (or `Reverting`) |

The existing `url` / `api_key` / `api_secret` now hold the **pushed per-Atlas Central
service-user** creds (written by `provision_tunnel`); `api_secret` is no longer
`set_only_once`, since Central rotates it by re-provisioning.

## Privilege model

Same as the rest of Atlas: a one-time, **operator-installed sudoers drop-in**
(`/etc/sudoers.d/atlas-tunnel`) pins the exact privileged commands the tunnel/firewall
scripts run (`wg`, `wg-quick`, `nft`, `systemctl`, `systemd-run`). The scripts run
through `run_local_task` as Tasks, so every privileged action is an audited `Task`
row, exactly like a host or guest op. The app never edits sudoers; staging the
scripts and the drop-in is an operator install step, documented in the README.

## Retiring the old direction

- The `Central Settings` **Register** button and `CentralClient.register`
  ([central.py](../atlas/atlas/central.py)) are removed — registration is now
  Central-initiated. `ping` / `post_event` stay; events still POST to Central's public
  URL authenticated as the pushed per-Atlas service user (Atlas outbound is
  unrestricted, so this works regardless of the firewall).
- Central's inbound `central.api.atlas.register` becomes a no-op / is removed;
  `event` is unchanged.

## Testing

- **Unit (Atlas):** `provision_tunnel` / `confirm_tunnel` happy path with
  `run_local_task` mocked; the auto-revert fires when `confirm` never arrives (the
  lockout-safety guarantee); the nft/`wg` argv builders are pure and unit-tested like
  `reserved_ip_nat.py`.
- **End-to-end (two hosts):** register a real Atlas → `wg0` up both sides, `ping`
  over the tunnel succeeds, public `base_url` is refused, the public UDP port is
  reachable, a VM command from Central lands over the tunnel, and an Atlas event
  reaches Central. **Lockout drill:** kill the tunnel mid-handoff and confirm the
  auto-revert restores public access with a clean failure (no half state).
