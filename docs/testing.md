# Integration test — boot a real VM and SSH in

`TestBootAndSSH` (`internal/firecracker`, build tag `integration`) creates a VM,
boots it, and SSHes in through the VM's netns. It cannot run in CI — it needs a
real host. It **skips** unless run as root with the env vars below set.

## Host prerequisites

- `firecracker` and `jailer` on `PATH` (`/usr/bin/`), `/dev/kvm`, root.
- Thin LVM pool + a base image: `sudo scripts/lvm-setup.sh`, then create
  `base-<image>` and `dd` a rootfs in (see the script's output).
- Forwarding/NAT: `UPLINK=<iface> sudo scripts/net-setup.sh`.
- A kernel at `<METAL_KERNEL_DIR>/<image>/vmlinux`.

## Base image requirements (the part metald can't do for you)

The rootfs in `base-<image>` must:
1. Have **sshd** installed and enabled.
2. On boot, read the SSH key from **MMDS** and install it to
   `~/.ssh/authorized_keys`. metald serves it EC2-style at
   `http://169.254.169.254/latest/meta-data/public-keys/0/openssh-key`
   (MMDS **V1** — plain GET, no token). cloud-init's Ec2 datasource does this,
   or a tiny rc script: `curl -s .../openssh-key >> /root/.ssh/authorized_keys`.
3. Route the metadata IP out eth0: `ip route add 169.254.169.254 dev eth0`.

The guest's own address (`172.16.0.2/24`, gw `172.16.0.1`) is set by the kernel
`ip=` boot arg metald passes, so no DHCP is needed.

## Run

```sh
sudo -E env \
  METAL_IMAGE=ubuntu \
  METAL_KERNEL_DIR=/var/lib/metal/kernels \
  METAL_VG=metalvg \
  METAL_SSH_PUB=$HOME/.ssh/id_ed25519.pub \
  METAL_SSH_KEY=$HOME/.ssh/id_ed25519 \
  go test -tags integration -run TestBootAndSSH -v ./internal/firecracker/
```

Passes when the guest returns `metal-ok` over SSH. The VM is destroyed on
cleanup.

## Reaching a VM by hand

Every guest shares `172.16.0.2` inside its own netns, so address it via the
namespace:

```sh
ip netns exec metal-<id> ssh -i ~/.ssh/id_ed25519 root@172.16.0.2
```
