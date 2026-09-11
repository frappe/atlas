# Proxy Server specification

[Service module specification](../../SPEC.md)

## Purpose

Proxy Server owns one member of the regional HTTP proxy cluster. Atlas supports at most five non-archived members.

## Lifecycle

```text
create VM -> node DNS -> SSH -> package -> local configuration -> peer configuration -> readiness -> regional DNS and wildcard CNAME -> Active
```

Creation needs one Allocated Metal Server IP Address. Atlas creates the Proxy Server before it sends the VM request to Metal. A draft VM keeps the record Pending until VM reconciliation completes. Atlas queues Pending records every minute. A failed setup changes the status to Failed and records the failed phase.

Atlas gives every node a `proxy-NNN.<wildcard-domain>` A record with a 3600-second TTL. It creates a health-checked multivalue A record for each node at `proxy.<wildcard-domain>` with a 120-second TTL.

Atlas creates `*.<wildcard-domain>` as a CNAME to `proxy.<wildcard-domain>` with a 3600-second TTL.

Before Atlas publishes a joining node in regional DNS, it sends the new peer list to each active node. The joining node restores route state from the highest-generation peer.

Archive removes the regional DNS value and health check before it removes the node DNS record and releases the VM. An archived record keeps no `virtual_machine` link. Atlas then sends the remaining membership to active nodes.

## Configuration

Atlas Settings owns the regional password, the earlier password, the Central JWKS URL, the regional signing key, and the wildcard certificate. Atlas sends its merged JWKS URL to each Proxy. The Proxy audience is `atlas-proxy:<region ID>`. Each node trusts `central` and `atlas:<region ID>`. Each node configuration contains the same credentials and peer list. It also contains its node ID and control name.

Atlas writes the secret configuration through `SSHRunner`. It does not put the configuration content in an SSH Task. A digest detects changes to membership, credentials, certificate, control names, and the template. Reconciliation retries changed configurations every minute.

## Access

Only System Managers with System User accounts can create or operate Proxy Server records.

## Package

Atlas creates an archive from `services/http-proxy/` and publishes it as a public File. The source digest skips an unchanged build. Atlas keeps a replaced File until no Atlas Settings link refers to it.

Use the [HTTP proxy specification](../../../../services/http-proxy/SPEC.md) for the packaged component. Use [Proxy Server operations](../../../docs/proxy-server.md) for cluster behavior. Use [Wildcard TLS](../../../docs/wildcard-tls.md) for certificate renewal.
