# Atlas scripts

Shell scripts and systemd units that run on the Server (the host that runs
Firecracker). Each file here is uploaded to the server over SSH and executed
as a single task. See `spec/04-tasks.md`.

Conventions:

- `set -euo pipefail` at the top of every script.
- Every input is a positional argument or an environment variable, validated
  at the top with `: "${VAR:?required}"`. No hard-coded values.
- Idempotent: re-running the script on the same input is safe.
- No external tools beyond what is installed by `bootstrap-server.sh`.
- Output goes to stdout/stderr. Atlas captures both into the Task record.

Files:

- `bootstrap-server.sh` — turn a fresh Ubuntu 24.04 host into a Firecracker host.
- `sync-image.sh` — download a kernel + rootfs pair onto a server.
- `provision-vm.sh` — create per-VM rootfs, write config, set up networking,
  enable the systemd unit.
- `start-vm.sh`, `stop-vm.sh`, `terminate-vm.sh` — lifecycle.
- `vm-network-up.sh`, `vm-network-down.sh` — invoked by the systemd unit on
  start/stop. Laid down by `bootstrap-server.sh`.
- `systemd/firecracker-vm@.service` — systemd unit template, laid down by
  `bootstrap-server.sh`.
- `guest/atlas-network.service` — systemd unit installed *inside* the VM
  rootfs by `sync-image.sh` to bring up the static IPv6 in the guest.
