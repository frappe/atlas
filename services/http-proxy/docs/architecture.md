# Architecture

The proxy VM has two processes:

```text
controller -> control daemon :9000 -> /run/nginx/admin.sock -> OpenResty
                                                        |
                                                        +-> SNI bridge socket
```

OpenResty handles public traffic. The HTTP worker routes wildcard and plaintext requests. The stream worker reads TLS SNI without terminating custom-domain TLS. The HTTP and stream workers use separate shared dictionaries, so a domain update passes through the private `sni-bridge.sock`.

Domain lookups check the exact hostname first. If there is no exact entry, the lookup derives wildcard suffix keys such as `*-something.example.com` from the hostname. It checks the most specific suffix first without scanning the map.

The control daemon is the only network API for configuration. It keeps the desired `sites` and `domains` maps in `/var/lib/nginx/control-state.json` and applies changes to OpenResty. It checks the proxy health endpoint every five seconds, but only sends the complete state on startup, after a failed check, or when the proxy boot ID changes. This makes either process restart safe without repeatedly resending unchanged maps.

Configuration changes do not reload nginx. OpenResty persists its runtime maps to `map.json` and `domains-http-map.json` as an additional fast restart path. The daemon state is the source of truth for reconciliation.
