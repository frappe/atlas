# How Atlas reaches hosts

Atlas reaches each Metal host through WireGuard. SSH, the Metal control API, and the browser console use the host `wg0` interface.

Hosts use this path for file downloads when `atlas_internal_url` is set. After installation, the host firewall rejects new management connections on the public interface.

```text
Atlas atlas0 (fdaa:<region>::ffff:ffff:ffff:ffff)  ==WireGuard==>  host wg0 (fdab:<region>:<host>)
                                                                    ├── sshd :22, metald :9000
                                                                    ├── tenant-0 VMs (fdaa:<region>::<vm>)
                                                                    └── ip netns exec metal-<vm> nc 172.16.0.2 22   (guest SSH)
hosts and tenant-0 VMs  --http-->  atlas_internal_url  (an Atlas listener on its mesh address)
```

## The Atlas peer

Atlas has one WireGuard identity for each region. **Atlas Settings** holds its address and public key.

The address is a tenant-0 VM address, `fdaa:<region>::ffff:ffff:ffff:ffff`. No VM can take it. WG Mesh trusts only `fdab` senders as hosts, so Atlas cannot send mesh tunnels.

Hosts can reach Atlas. Among VMs, only tenant-0 VMs can reach its mesh address.

Atlas Settings also holds the private key in an encrypted, hidden field, so a site backup keeps it. Atlas never copies it to a host.

Atlas writes `sites/<site>/private/wireguard/atlas0.conf`. Each host peer allows the host `fdab` address and the tenant-0 VMs on that host. A scheduler job writes the file again within 10 seconds when a host or a tenant-0 VM changes, for example after a migration.

A root systemd timer applies the file every 10 seconds. When the file changed, it copies it to `/etc/wireguard/atlas0.conf` and runs `wg syncconf`, or `wg-quick up` for the first start. A timer is used because SELinux can hide a home directory from a path unit.

Atlas uses each host private IPv4 address as the endpoint. The Atlas VM reaches it through the masquerade on its parent host. A development Atlas reaches it through the [development gateway](#development-gateway).

Site config `atlas_wireguard_mtu` sets the `atlas0` MTU. Use `1280` through the development gateway.

## Set up the Atlas machine

Run these commands once on the machine that runs the Atlas bench:

```sh
pilot --site SITE configure-atlas-wireguard
sudo scripts/install-atlas-wireguard.sh <bench>/sites/SITE/private/wireguard/atlas0.conf
wg show atlas0
```

`configure-atlas-wireguard` creates the identity once and writes the peer file. Run it before the first host setup, which refuses to start without it. The install script adds `atlas-wireguard-atlas0.timer` and `atlas-wireguard-atlas0.service`.

The service also applies table `inet atlas_atlas0`. It admits TCP `22`, `80`, `443`, and `2222` on `atlas0`, plus ICMPv6 and replies.

To change the TCP ports, run the install script again with `ATLAS_WIREGUARD_TCP_PORTS`. For a development listener on port `8000`, set the value to `"22, 80, 443, 2222, 8000"`.

WireGuard needs no port on `atlas0`, because Atlas starts every handshake from the uplink.

## Development gateway

A development Atlas runs outside the provider network. The gateway is atlas-vm in gateway mode on one Metal host. It carries the `atlas0` packets to the host private endpoints, so hosts expose no WireGuard port publicly.

```text
laptop atlas-gateway (172.16.100.3) ==outer WireGuard==> atlas-vm :51821 ── masquerade ──> 10.1.0.x:51820
laptop atlas0 ======================= inner WireGuard, end to end ======================> host wg0
```

1. Create the first Metal Server record. Its setup can stop at `wireguard-link` after 120 seconds. The host firewall is not installed yet.
2. Run `pilot --site SITE deploy-dev-gateway <metal-server-id> --ssh-host <public-IPv4>`. The command installs atlas-vm on that host and writes `atlas-gateway.conf`. When Atlas can reach that host later, omit `--ssh-host` to update the gateway.
3. Run `sudo scripts/install-atlas-wireguard.sh <bench>/sites/SITE/private/wireguard/atlas-gateway.conf` on the Atlas machine.
4. Set the Atlas MTU to `1280` with `pilot --site SITE set-config -p atlas_wireguard_mtu 1280`. Then run `pilot --site SITE configure-atlas-wireguard` to rewrite `atlas0.conf`.
5. Restart `atlas0` with `sudo wg-quick down atlas0 && sudo systemctl start atlas-wireguard-atlas0.service`. `wg syncconf` does not change the MTU of a running interface.
6. If the Metal Server status is `Failed`, use **Setup Metal Server** to retry. Normal setup installs Metal and the host firewall after the WireGuard link works.

The gateway cannot decrypt the `atlas0` sessions. The outer link carries only the provider private network.

## Host side

`configure-wireguard.sh` adds the Atlas public key and address to the host `wg0.conf`. The Atlas peer has no endpoint. Atlas starts the handshake and keeps it alive.

Metal applies only the host peers from [host sync](host-sync.md). It removes only the peers that it added, so the Atlas peer stays. `metald.toml` names the Atlas address as `[wg_mesh] controller_address`, and WG Mesh lets tenant-0 VMs send to it through `wg0`.

`metald` binds its control API to the host `wg0` address. The host certificate names that address.

## SSH

| Target | Path |
| --- | --- |
| Host before its WireGuard setup | Public IPv4 address. Used only during provisioning. |
| Host after its WireGuard setup | Host `fdab` address. |
| Guest | SSH to the host, then `ip netns exec metal-<vm> nc 172.16.0.2 22` as the SSH `ProxyCommand`. |

Provisioning has a `wireguard-link` step. It writes `atlas0.conf` with the new host at once, then waits for root SSH on the `fdab` address before it installs Metal.

Atlas reads the VM host for each connection. The guest needs no public address for Atlas SSH.

## Host firewall

`install_metal` installs table `inet atlas_host` and the `atlas-host-firewall` unit. The rules cover traffic to the host itself.

| Input | Allowed from |
| --- | --- |
| WireGuard UDP | The private network, and the local atlas-vm (`tap-atlas`). |
| SSH and Metal control `9000` | The Atlas address on `wg0`. |
| Migration `9001` and `9002` | Host addresses on `wg0`. |
| SSH for recovery | The private network on the private uplink. |
| ICMP, DHCP, and replies | Any address. |

The Metal network setup, which `metal.service` runs at start, drops guest packets to the private network CIDR, on any interface. Guest public addresses, NAT, and mesh traffic do not change.

## Internal URL

Hosts and tenant-0 VMs download Atlas files from `atlas_internal_url`. Cargo uses it for the Atlas API and JWKS. Proxies use it for JWKS.

Set it to an Atlas listener on the Atlas mesh address, for example `http://[fdaa:1::ffff:ffff:ffff:ffff]:8000`. The listener must send the Atlas site name in `Host`. If the port is not `80` or `443`, run the WireGuard install script with that port in `ATLAS_WIREGUARD_TCP_PORTS`.

Without `atlas_internal_url`, clients use `atlas_base_url` or the site URL.

## People access through Warpgate

People reach hosts through [Warpgate](https://warpgate.null.page/), an SSH bastion. Each region runs one Warpgate in the Atlas VM. Warpgate connects to the host `wg0` address from the Atlas address, so the host firewall needs no new rule. Atlas stores no people and no grants. Warpgate owns them.

```text
person ──ssh -p 2223 <email>:<host title>@warpgate.<wildcard>──> Warpgate (Atlas VM) ──root@<host wg0>:22──> host
person ──https://warpgate.<wildcard>──> nginx (region wildcard certificate) ──> Warpgate 127.0.0.1:8888
Central ──/api/atlas/hosts/{id}/access/grant──> Atlas ──admin API──> Warpgate
```

Example: `ssh -p 2223 alice@frappe.io:metal-par-1-2@warpgate.par-1.frappe.cloud`. Warpgate prints a link. The person opens it, signs in to Central, and approves the login. People need no SSH keys.

### Access

Signing in gives no host access. Each access is a grant of one Warpgate role with an end time:

| Role | Opens | Granted by |
| --- | --- | --- |
| `host:<host title>` | One host | `POST /api/atlas/hosts/{id}/access/grant` |
| `all-hosts` | Every host | The same route with `all` as the ID |
| `warpgate:admin` | The Warpgate admin UI, not hosts | Central role `Atlas Warpgate Admin`, at sign-in |

The grant and revoke routes, and `GET /api/atlas/hosts`, need scope `*` and tenant 0. A grant registers the person in Warpgate when needed, so Central can grant before the first sign-in. `expires_at` must be at most 24 hours away. Set site config `warpgate_grant_max_hours` to change the limit. Warpgate ends the access by itself at `expires_at`. A Warpgate admin can also grant a role in the Warpgate admin UI.

### What Atlas keeps in step

A job runs every minute. It makes the Warpgate targets match the hosts that have a `wg0` key:

- One target for each host, named after the host title, `root` on the host `wg0` address.
- The roles `all-hosts` and `host:<host title>` on each target. A renamed host keeps its target, role, and grants.
- The first time a running host is seen, Atlas adds the Warpgate client keys to root `authorized_keys` and gives Warpgate the host key. Warpgate refuses any other host key.
- Targets and roles of deleted hosts are removed. Targets that Atlas did not create are left alone.

Atlas names a new host `metal-<region name>-<counter>`, such as `metal-waw-3-4`. The title is read-only. It must be one lowercase DNS label and unique across all hosts, including deleted ones.

### Install

Central must provide an OpenID Connect (OIDC) issuer that Warpgate can reach. Register Warpgate as an OIDC client in Central. Set its redirect URI to `https://warpgate.<wildcard>/@warpgate/api/sso/return`.

Add the client details to `atlas-vm.toml` on the host that runs atlas-vm:

```toml
[atlas.warpgate]
issuer_url = "https://central.example.com/oidc"
client_id = "warpgate-region"
client_secret = "change-me"
```

Use Central's issuer URL for `issuer_url`. Use the client ID and secret from the Warpgate client registration. The separate `atlas.central_jwks_url` lets Atlas validate Central API tokens.

To grant Warpgate admin access, Central must send `Atlas Warpgate Admin` in the OIDC `roles` claim. Warpgate maps that value to `warpgate:admin`.

atlas-vm setup then installs a pinned Warpgate. It creates the `atlas` user with target, role, user, and configuration permissions. Configuration permission lets Atlas add known host keys. Setup stores the user's API token in **Atlas Settings**.

Setup also opens TCP 2223 on the Atlas host, adds the nginx server, and creates `warpgate.<wildcard>` as a CNAME to the Atlas site. Atlas copies the region wildcard certificate to nginx when it issues or renews it.

Only the Warpgate SSH and HTTP listeners are on. The HTTP listener is on loopback behind nginx.

The Warpgate `admin` and `atlas` passwords are in `/root/warpgate-admin-password` and `/root/warpgate-atlas-password`. Only root can read them. If setup fails, run it again. It continues from the last completed step.

The Atlas token lasts one year. Atlas Settings stores the token and its ID. A daily job replaces the stored token 30 days before it expires and deletes every other atlas token. If the token expired, run atlas-vm setup again: each run issues a new token.

In development, run Warpgate on the machine that runs Atlas, so it uses the local `atlas0`. Set **URL**, **API Token**, and **API Token ID** in the Warpgate section of Atlas Settings by hand.

## Recovery

| Problem | Action |
| --- | --- |
| No handshake on `atlas0` | Check `wg show atlas0`, the gateway link, and UDP 51820 on the host. |
| New host stops at `wireguard-link` | Check that `atlas0.conf` lists the host and that the timer applied it: `systemctl status atlas-wireguard-atlas0.service`. |
| Atlas key lost | Restore the site backup with its `site_config.json`, because the encryption key is in that file. A new key needs the provider console on every host: clear `wireguard_public_key` in Atlas Settings, run `configure-atlas-wireguard`, then run `configure-wireguard.sh` on each host. |
| Host firewall blocks access | From the private network or console, run `systemctl disable --now atlas-host-firewall && nft delete table inet atlas_host`. |
| Warpgate refuses a person | Check that the grant is active for the right email and host. A target appears within a minute after the host gets its `wg0` key. Read Error Log for "Warpgate target sync failed". |

::: details Source code and tests

- [Atlas peer](../../atlas/metal_server/core/atlas_peer.py) owns the identity and `atlas0.conf`. [Development gateway](../../atlas/metal_server/core/development_gateway.py) owns `atlas-gateway.conf`.
- [Timer installer](../../scripts/install-atlas-wireguard.sh) applies a file and its input filter as root.
- [WireGuard script](../../atlas/scripts/configure-wireguard.sh) adds the Atlas peer to a host.
- [Host firewall script](../../atlas/scripts/install-host-firewall.sh) writes the host rules.
- [Warpgate sync, access, and client](../../atlas/service/core/warpgate/) and the [installer](../../scripts/install-warpgate.py).
- [atlas-vm](../../scripts/atlas-vm/atlas_vm.py) runs the gateway VM.
- [WG Mesh VM hook](../../services/wg-mesh/bpf/vm.h) routes tenant-0 traffic to the controller.
- [Atlas peer tests](../../atlas/metal_server/core/test_atlas_peer.py) and [provisioning tests](../../atlas/metal_server/core/test_provisioning.py) check identity and setup order.

:::
