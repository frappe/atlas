# HTTP proxy component specification

[Root specification](../../SPEC.md)

## Purpose

The HTTP proxy runs as a regional cluster of one to five virtual machines. Each node routes site traffic to site VMs over IPv6 and keeps a local copy of the regional route maps.

## Layout

```text
control/       Python control daemon and cluster coordinator
nginx/         OpenResty and Lua data plane
tests/         Data-plane and package tests
docs/          Setup, operation, and development guides
```

## Interfaces

- `proxy.<wildcard-domain>` is the health-checked regional control address.
- `proxy-NNN.<wildcard-domain>` is one node address. Peers use these addresses for HTTPS communication.
- The control daemon serves `127.0.0.1:9000`. `atlas-proxy-control.socket` holds the port during daemon restarts.
- OpenResty sends both control names to the daemon before it reads the site map.
- OpenResty and the daemon use private Unix sockets for map operations.
- Atlas writes `/etc/atlas/proxy-control.toml`. The file is the source for node membership, credentials, and TLS configuration.
- Atlas supplies one merged JSON Web Key Set. It contains Central keys and the regional Atlas key.

## Ownership

Keep control and cluster code in `control/`. Keep data-plane code in `nginx/`. Atlas owns VM lifecycle, DNS records, credentials, peer membership, and the regional wildcard certificate.

## Authorization

The Proxy binds each key namespace to one issuer. A `central:*` key can validate only `iss=central`. An `atlas:<region ID>:*` key can validate only the matching regional issuer.

A token grants only its signed scopes and resource constraints. The subject is an identity label and does not change the authority. The known scopes are `site:*`, `domain:*`, and `*`. A constraint limits the site names or the domain names of the token with a prefix, a suffix, or an exact name list. A scope without a constraint applies to all names for that resource. A constrained resource cannot replace its complete map. A token that carries a `tenant` claim is an Atlas API credential, and the Proxy refuses it.

## Validation

From this directory, run `python -m pip install --editable 'control[test]'` and `python -m pytest -q control/tests`.

## Documentation

Read [`README.md`](README.md), then the relevant guide in [`docs/`](docs/). See [`docs/high-availability.md`](docs/high-availability.md) for the cluster design and state.
