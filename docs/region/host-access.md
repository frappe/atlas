# How Atlas reaches hosts

Atlas reaches each Metal host through WireGuard. SSH, the Metal control API, and the browser console use the host `wg0` interface. The host public address accepts no management traffic.

## The Atlas peer

Atlas has one WireGuard identity for each region. **Atlas Settings** holds its address and public key.

The address is a tenant-0 VM address, `fdaa:<region>::ffff:ffff:ffff:ffff`. No VM can take it. WG Mesh trusts only `fdab` senders as hosts, so Atlas cannot send mesh tunnels.

Hosts can reach Atlas. Among VMs, only tenant-0 VMs can reach its mesh address.

Atlas Settings also holds the private key in an encrypted, hidden field, so a site backup keeps it. Atlas never copies it to a host.

Atlas writes `sites/<site>/private/wireguard/atlas0.conf`. Each host peer allows the host `fdab` address and the tenant-0 VMs on that host. A scheduler job writes the file again within 10 seconds when a host or a tenant-0 VM changes, for example after a migration.

A root systemd timer applies the file every 10 seconds. When the file changed, it copies it to `/etc/wireguard/atlas0.conf` and runs `wg syncconf`, or `wg-quick up` for the first start. A timer is used because SELinux can hide a home directory from a path unit.

Atlas uses each host private IPv4 address as the endpoint. The Atlas VM reaches it through the masquerade on its parent host.

Site config `atlas_wireguard_mtu` sets the `atlas0` MTU.

## Set up the Atlas machine

Run these commands once on the machine that runs the Atlas bench:

```sh
pilot --site SITE atlas-wireguard
sudo scripts/install-atlas-wireguard.sh <bench>/sites/SITE/private/wireguard/atlas0.conf
wg show atlas0
```

`atlas-wireguard` creates the identity once and writes the peer file. Run it before the first host setup, which refuses to start without it. The install script adds `atlas-wireguard-atlas0.timer` and `atlas-wireguard-atlas0.service`.

The service also applies table `inet atlas_atlas0`. It admits TCP `22`, `80`, `443`, and `2222` on `atlas0`, plus ICMPv6 and replies.

To change the TCP ports, run the install script again with `ATLAS_WIREGUARD_TCP_PORTS`. For a development listener on port `8000`, set the value to `"22, 80, 443, 2222, 8000"`.

WireGuard needs no port on `atlas0`, because Atlas starts every handshake from the uplink.

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

## Recovery

| Problem | Action |
| --- | --- |
| No handshake on `atlas0` | Check `wg show atlas0` and UDP 51820 on the host. |
| New host stops at `wireguard-link` | Check that `atlas0.conf` lists the host and that the timer applied it: `systemctl status atlas-wireguard-atlas0.service`. |
| Atlas key lost | Restore the site backup with its `site_config.json`, because the encryption key is in that file. A new key needs the provider console on every host: clear `wireguard_public_key` in Atlas Settings, run `atlas-wireguard`, then run `configure-wireguard.sh` on each host. |
| Host firewall blocks access | From the private network or console, run `systemctl disable --now atlas-host-firewall && nft delete table inet atlas_host`. |

::: details Source code and tests

- [Atlas peer](../../atlas/metal_server/core/atlas_peer.py) owns the identity and `atlas0.conf`.
- [Timer installer](../../scripts/install-atlas-wireguard.sh) applies a file and its input filter as root.
- [WireGuard script](../../atlas/scripts/configure-wireguard.sh) adds the Atlas peer to a host.
- [Host firewall script](../../atlas/scripts/install-host-firewall.sh) writes the host rules.
- [WG Mesh VM hook](../../services/wg-mesh/bpf/vm.h) routes tenant-0 traffic to the controller.
- [Atlas peer tests](../../atlas/metal_server/core/test_atlas_peer.py) and [provisioning tests](../../atlas/metal_server/core/test_provisioning.py) check identity and setup order.

:::
