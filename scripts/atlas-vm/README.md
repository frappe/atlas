# Atlas control plane VM

These scripts run Atlas itself in a local Firecracker VM. One command builds the guest image, boots the VM, installs Pilot, creates the bench and the site, and leaves the site up.

Use this when you want a clean Atlas host that you can delete, and not a second bench on your workstation.

## Requirements

The host needs `firecracker`, `ip`, `iptables`, `ssh`, `tar`, `git`, and `/dev/kvm`. The image build needs `curl`, `unsquashfs`, `mkfs.ext4`, `truncate`, `zstd`, and `sudo`, because only root can restore file ownership into the root file system.

## Commands

```sh
scripts/atlas-vm/run.sh up        # build, boot, and provision
scripts/atlas-vm/run.sh ssh       # open a shell in the VM
scripts/atlas-vm/run.sh logs      # follow the serial console
scripts/atlas-vm/run.sh status    # report the VM state
scripts/atlas-vm/run.sh down      # stop the VM and remove its host network
```

`up` is safe to run again. It reuses a running VM, copies the repository again, and repeats each provisioning step that is not complete. Use `--rebuild` to build the root file system again.

The Ubuntu download stays in `~/.cache/atlas-vm/downloads`, so a rebuild starts at the extraction step.

| Option | Default | Purpose |
|---|---|---|
| `--name` | `atlas` | VM name, TAP device, and work directory |
| `--vcpu` | `4` | Guest vCPU count |
| `--memory-mib` | `8192` | Guest memory |
| `--disk-gib` | `24` | Guest disk |
| `--vm-address` | `172.16.100.2` | Guest address |
| `--host-address` | `172.16.100.1` | Host side of the link |
| `--bench` | `atlas-bench` | Bench name in the VM |
| `--site` | `atlas.localhost` | Site name in the VM |
| `--admin-password` | `admin` | Site Administrator password |
| `--no-provision` | off | Boot only |

After `up`, open `http://172.16.100.2:8000` and sign in as `Administrator`.

## Network model

The Atlas VM is a control plane VM and not a hosted VM. Its TAP device stays in the host root network namespace with IP forwarding and address translation, so the VM reaches the internet and the host reaches the VM directly. A hosted VM instead gets a per tenant network namespace, an Atlas WG Mesh address, and traffic limits that Metal applies.

```text
host                                     guest
  tap-atlas 172.16.100.1/24  <-------->  eth0 172.16.100.2/24
  MASQUERADE to the default route        default via 172.16.100.1
```

`down` removes the address translation rules and the TAP device. It keeps the disk, so the next `up` boots the same VM.

## What the guest gets

1. Ubuntu 24.04 from the same pinned cloud image the Metal guest image uses, with cloud-init off. The image carries the address, the host keys, and the authorized key, because this VM has no metadata service.
2. The Pilot host stack and the `frappe` user, from the Pilot installer.
3. A bench, the `atlas` app from the repository you ran the script in, and the site.
4. `atlas-bench.service`, which runs `pilot start` and restarts after a failure.

The repository reaches the guest as the files Git tracks plus `.git`, so local build output and caches stay on the host. Pilot clones the last commit, and the working tree is copied over it. A file you deleted without committing stays present in the VM.

## Next steps in the VM

A Metal host downloads `metald` and Atlas WG Mesh from the Atlas site, so the site needs an address that the host can reach.

```sh
scripts/atlas-vm/run.sh ssh
su - frappe
pilot -b atlas-bench --site atlas.localhost set-config atlas_base_url http://172.16.100.2:8000
```

Use a public tunnel address instead when the Metal host is not on this machine.

Then follow [getting started](../../atlas/docs/getting-started.md) from the Atlas Settings step.

## Files

| File | Purpose |
|---|---|
| `run.sh` | Host side. Image, network, VM lifecycle, and the provisioning call. |
| `build-rootfs.sh` | Builds the guest root file system and the Firecracker kernel. Needs root. |
| `provision.sh` | Guest side. Pilot, bench, app, site, and the service. Runs as root in the VM. |
