# Environments and access

The auto mode classifier enforces this policy through [settings/auto-mode.json](../settings/auto-mode.json).

## Tiers

| Tier | Examples | Agent permission |
|---|---|---|
| Development | Local sites (`*.localhost`, `test.local`), the developer's own Metal hosts and VMs, local containers, scratch cloud resources they created | Delegated: read, write, deploy, restart, patch. |
| Staging | Staging Atlas region and hosts, staging Central, staging Cargo | Reads fine. Each write or restart needs approval. |
| Production and undeclared | Production regions, production Central, any undeclared host or account | Blocked. Give the developer commands to run by hand. |

An undeclared or unclear target is production.

## Declare your environments

The repo is public, so it names no real target. Declare yours in `.claude/settings.local.json` (Git-ignored) or `~/.claude/settings.json`, one `environment` line per target:

```json
{
  "autoMode": {
    "environment": [
      "$defaults",
      "Development host: 203.0.113.10 and the VMs on it. Agent may act freely.",
      "Development site: atlas.localhost on this laptop. Never run tests on it.",
      "Staging: staging.example.com and every host reached through it.",
      "Production: prod.example.com, every host reached through it, and the production control plane."
    ]
  }
}
```

Keep the shared rules from [settings/auto-mode.json](../settings/auto-mode.json) in the same file. Never commit real hostnames, addresses or credentials.

## Every tier

- Tests only on `test.local`, never on a site with real data.
- Never echo secrets, tokens, keys or `.env` content into chat, logs, commits or docs. Rotate anything pasted into chat. Ops scripts may print a new password once to the operator's terminal.
- Provider calls that create, delete or cost money need approval, even in development.
- Destructive host actions (`zfs destroy`, `zpool`, disk wipes, `wg-quick down` on shared interfaces, firewall flushes) need approval outside development.
- Never widen access: no `0.0.0.0`, firewall relaxing or new SSH keys on shared hosts without approval.
- People write incident reports; agents may suggest one.

## Staging requests

```text
Staging action: <what and why>
Host: <host>
Command: <exact command>
Effect: <what changes, how to undo>
```

Approval covers that command only.

## Production

Never act on production, even when asked, through a jump host or a script. Prepare a runbook (commands, checks, rollback) for a person. The classifier blocks attempts.
