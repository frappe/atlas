# deploy

Systemd units for metal.

- `metal.service` — the metald daemon (one per host).
- `metal-vm@.service` — template unit for a VM; metald starts instances as
  `metal-vm@<id>.service`. Not enabled directly.

Install:

    cp deploy/metal.service deploy/metal-vm@.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now metal.service

Host prerequisites: `firecracker` and `jailer` on `PATH`, `/dev/kvm`, a thin
LVM pool (`scripts/lvm-setup.sh`), and forwarding/NAT (`scripts/net-setup.sh`).
