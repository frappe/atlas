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
# Set pilot.site and pilot.password. Add one [[image]] table for each guest image.
sudo atlas-vm create
```

## Use the VM

```sh
sudo atlas-vm status
sudo atlas-vm ssh
sudo atlas-vm logs --setup --follow
sudo atlas-vm restart
sudo atlas-vm setup                        # run setup.py again
sudo atlas-vm resize --vcpu 8 --disk 60    # reboots the VM; a disk can only grow
```

The VM answers on port 2222, and host ports 80 and 443 reach it.

## Delete the VM

```sh
sudo atlas-vm destroy
```

This deletes the bench, every site, and every database in the VM. There is no backup.
