# Proxy Server

Atlas manages one regional HTTP proxy cluster with at most five Proxy Server records. Each record owns one proxy virtual machine, its stable DNS name, its DNS health check, and the hashes of the package and configuration that Atlas sent.

## Regional addresses

Each node uses `proxy-NNN.<wildcard-domain>`. Atlas creates an A record with a 3600-second TTL. Proxies use node names for HTTPS peer communication.

The cluster uses `proxy.<wildcard-domain>`. Atlas creates one Route53 multivalue A record per node with a 120-second TTL. Each record has an HTTPS `/healthz` health check, so a node that loses quorum keeps its regional DNS value.

Atlas creates `*.<wildcard-domain>` as a CNAME to `proxy.<wildcard-domain>` with a 3600-second TTL. Thus, all site names use the healthy regional proxy addresses.

## Provisioning

Each node needs one Allocated Metal Server IP Address. Reserve it before you create the Proxy Server.

Atlas uses this order for a new node:

```text
create VM
  -> create node A record
  -> wait for SSH
  -> install proxy package
  -> send local configuration
  -> send new membership to active peers
  -> wait for node readiness
  -> create health check, regional DNS record, and wildcard CNAME
  -> mark node Active
```

The configuration contains all Active and Provisioning nodes. The joining daemon gets route maps from the peer with the highest generation. Atlas sends no route snapshot during provisioning.

Atlas retries a Pending record every minute. A failed step changes the record to Failed. Re-provision runs the complete sequence and skips an unchanged package or configuration.

## Credentials

Atlas Settings owns one regional proxy password and one previous password. Atlas rotates the current password every 6 hours and pushes both values to active nodes. A System Manager can also select **Rotate proxy password** from the Atlas Settings Actions menu.

Public API clients can use the regional Proxy password or a valid JSON Web Token. Each Proxy gets the merged regional key set from Atlas. Central has unrestricted access. A regional token can restrict site names with a signed suffix constraint. Peer requests use the Proxy password in the cluster header. Nodes accept the earlier password for 10 minutes after rotation.

## Configuration reconciliation

Atlas computes a configuration digest from the certificate, credentials, peer membership, control names, and template. It checks active proxies every minute and queues a configuration push when the stored digest differs.

The configuration is written to a temporary file with mode `0600` and moved into place. The apply command validates the wildcard certificate and reloads OpenResty. Atlas then restarts the control daemon so cluster settings take effect.

## Archive

Archive removes the node from `proxy.<wildcard-domain>`, deletes its health check, removes its node A record, terminates its VM, and marks the Proxy Server as Archived. Archive also clears the `virtual_machine` link and puts the machine name in a comment. Atlas then pushes the smaller membership list to the remaining active nodes.

## Constraints

- A region supports at most five non-archived Proxy Server records.
- Create and manage Proxy Servers as a System Manager with a System User account.
- The sites API rejects `proxy` and every `proxy-*` key.
- The domains API rejects the wildcard zone and every name below it.

Use the [Proxy Server specification](../service/doctype/proxy_server/SPEC.md) for ownership details. Use [HTTP proxy setup](../../services/http-proxy/docs/setup.md) and [control daemon behavior](../../services/http-proxy/docs/control-daemon.md) for node operation. Use [Wildcard TLS](wildcard-tls.md) for certificate issuance and renewal.
