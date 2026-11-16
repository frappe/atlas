# guestagent: the atlas-guest-agent binary

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · overview: [Change SSH keys](../../../docs/compute/ssh-keys.md)

## Purpose

The `guestagent` package runs inside each VM as `cmd/atlas-guest-agent`. It is a static binary, so it does not depend on the guest distribution. The image builder installs it at `/usr/local/lib/atlas/atlas-guest-agent`.

| Command | Started by | Action |
|---|---|---|
| `apply-metadata` | `atlas-metadata.service` | Polls MMDS. Applies the hostname, host time, mesh address, mesh MTU and host routes. |
| `authorized-keys` | sshd `AuthorizedKeysCommand` | Prints the SSH keys from MMDS. |

## Contract

- The agent changes only with a new image. There is no self-update.
- The agent must work on a guest that has no Python and no network tools other than `ip`, `networkctl`, `resolvectl`, `hostnamectl` and `systemctl`.
