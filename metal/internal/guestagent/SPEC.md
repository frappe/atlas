# guestagent: the atlas-guest-agent binary

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · overview: [Read VM metrics](../../../docs/compute/index.md#read-vm-metrics)

## Purpose

The `guestagent` package runs inside each VM as `cmd/atlas-guest-agent`. It is a static binary, so it does not depend on the guest distribution. The image builder installs it at `/usr/local/lib/atlas/atlas-guest-agent`.

| Command | Started by | Action |
|---|---|---|
| `apply-metadata` | `atlas-metadata.service` | Polls MMDS. Applies the hostname, host time, mesh address, mesh MTU and host routes. |
| `authorized-keys` | sshd `AuthorizedKeysCommand` | Prints the SSH keys from MMDS. |
| `report-usage` | `atlas-guest-agent-usage.socket`, vsock port 100000 | Writes one `Usage` JSON line. |

systemd starts one process for each vsock connection.

## Contract

- `Usage` is the wire format between the agent and metald. Add only optional fields, because old agents stay in old images.
- metald reads the report in `firecracker/guest_agent.go`. The guest controls the reply, so metald limits its size and time.
- Each job has its own vsock port in `protocol.go`, which matches the socket units in `atlas/vm/scripts/ubuntu_image_functions.sh`. Never reuse a port for a different job.
- The agent changes only with a new image. There is no self-update.
- The agent must work on a guest that has no Python and no network tools other than `ip`, `networkctl`, `resolvectl`, `hostnamectl` and `systemctl`.
