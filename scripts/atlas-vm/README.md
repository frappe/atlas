# Atlas VM

`atlas-vm` runs one Atlas bench in a Firecracker VM on a bare metal host. The host needs root, KVM, and a Secure Shell key pair.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/frappe/atlas/develop/scripts/atlas-vm/atlas_vm.py |
	sudo install -m 0755 /dev/stdin /usr/local/bin/atlas-vm
```

## Create the VM

```sh
curl -fsSLo atlas-vm.toml https://raw.githubusercontent.com/frappe/atlas/develop/scripts/atlas-vm/atlas-vm.example.toml
# Set each value that contains change-me. Set the site names and email addresses.
sudo atlas-vm create
```

The configuration must contain the Atlas region, Route53, and Let's Encrypt values. Set `atlas.server_provider` to `Scaleway` or `AWS`. Atlas reads the matching provider table. Route53 must contain an existing public zone for `atlas.wildcard_domain`. Do not add `*.` to the domain.

Atlas uses the `pilot.site` value with HTTPS as its public URL. Set `atlas.base_url` only if the public URL is different.

The `[atlas.vm_scheduling]` table configures VM placement and optional Metal Server auto-spawn. When auto-spawn is enabled, set both machine fields to names from the selected provider catalogs.

The setup generates a temporary password for Pilot and the site Administrator. It does not put this password in the configuration or output.

The setup creates one Secure Shell key for the `pilot.user`. Atlas uses this key to manage Metal Servers. The setup keeps this key when you run it again.

Set `atlas.import_server_id` to import this host as a Metal Server after Atlas setup. The import is optional and continues in the background. Set `atlas.import_storage_pool_device` only if the host needs a different storage device. See [provider server import](../../docs/region/hosts-and-providers.md#import-a-provider-server) for prerequisites and recovery.

In Atlas mode, setup creates the Atlas WireGuard peer and its systemd timer. See [Atlas access to hosts](../../docs/region/host-access.md) for the network path and host SSH access.

Gateway mode runs a development WireGuard gateway instead of Atlas. Deploy it with `pilot --site SITE deploy-dev-gateway <metal-server-id>`. See [development gateway setup](../../docs/region/host-access.md#development-gateway).

In Atlas mode, setup creates the server provider network resources and the Route53 records. It also gets the provider catalogs and a wildcard certificate. Each `[[image]]` table creates one system image in site-file storage. Cargo configures object storage later.

The Atlas mode configuration contains provider secrets. `atlas-vm` stores its copy at `/var/lib/atlas-vm/atlas-vm.toml` with mode `0600`.

## Use the VM

```sh
sudo atlas-vm status
sudo atlas-vm ssh
sudo atlas-vm logs --setup --follow
sudo atlas-vm restart
sudo atlas-vm setup                         # Apply the configuration again.
sudo atlas-vm reset-password pilot          # Use site for the site password.
sudo atlas-vm resize --vcpu 8 --disk 60     # This restarts the VM. A disk can only grow.
```

`atlas-vm setup` updates credentials and other mutable values. It stops if the configuration changes a region or provider value after provider setup.

AWS uses one subnet in one availability zone, so a public IPv6 block can move to any host. An AWS region always uses unicast networking, because a VPC does not carry link-local multicast.

The VM answers on host port 2222. In Atlas mode, host ports 80 and 443 also reach it. Gateway mode forwards UDP port 51821 to its WireGuard interface.

## Delete the VM

```sh
sudo atlas-vm destroy
```

This deletes the bench, every site, and every database in the VM. There is no backup.
