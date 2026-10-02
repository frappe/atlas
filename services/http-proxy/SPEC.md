# HTTP proxy component specification

[Root specification](../../SPEC.md) · Behavior: [HTTP proxy](../../docs/networking/http-proxy/index.md)

## Purpose

The HTTP proxy is a regional cluster of 1 to 5 VMs. Each node routes site traffic to site VMs over IPv6 and keeps a copy of the route maps.

## Layout

```text
control/       Python control daemon and route state on the shared control-cluster package
nginx/         OpenResty and Lua data plane
tests/         Data-plane and package tests
```

## Interfaces

- `proxy.<wildcard-domain>` is the regional control address.
- `proxy-NNN.<wildcard-domain>` is one node address for peer HTTPS.
- The control daemon serves `127.0.0.1:9000` through `atlas-proxy-control.socket`.
- OpenResty and the daemon share private Unix sockets.
- `/etc/atlas/proxy-control.toml` from Atlas is the source for membership, credentials, and TLS.

## Ownership

Atlas owns VM lifecycle, DNS, credentials, peer membership, and the wildcard certificate.

## Authorization

- Each key namespace validates only its own issuer.
- A token grants only its signed scopes and constraints, never its subject.
- A token with a `tenant` claim is an Atlas API credential. The Proxy refuses it.

See [control daemon authentication](../../docs/networking/http-proxy/control-daemon.md#authentication).

## Validation

From this directory, run `python -m pip install --editable ../control-cluster --editable 'control[test]'` and `python -m pytest -q control/tests`. See [development](../../docs/develop/http-proxy.md).
