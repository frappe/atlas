# Architecture

The proxy VM has two processes:

```text
controller -> control daemon :9000 -> /run/nginx/admin.sock -> OpenResty
                                                        |
                                                        +-> SNI bridge socket
```

OpenResty handles public traffic. The HTTP worker routes wildcard and plaintext
requests. The stream worker reads TLS SNI without terminating custom-domain TLS.
The HTTP and stream workers use separate shared dictionaries, so a domain update
passes through the private `sni-bridge.sock`.

The control daemon is the only network API for configuration. It keeps the
desired `sites` and `domains` maps in
`/var/lib/nginx/control-state.json`, applies changes to OpenResty, and retries
the complete state every five seconds. This makes either process restart safe:
the daemon reloads its state on startup, and its next reconciliation repairs the
proxy state.

Configuration changes do not reload nginx. OpenResty persists its runtime maps
to `map.json` and `domains-http-map.json` as an additional fast restart path.
The daemon state is the source of truth for reconciliation.
