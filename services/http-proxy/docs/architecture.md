# Architecture

The proxy VM has two processes:

```text
controller -> control daemon :9000 -> /run/nginx/admin.sock -> OpenResty
                                                        |
                                                        +-> SNI bridge socket
```

OpenResty handles public traffic. The HTTP worker routes wildcard and plaintext requests. The stream worker reads TLS SNI without terminating custom-domain TLS. The HTTP and stream workers use separate shared dictionaries, so a domain update passes through the private `sni-bridge.sock`.

Domain lookups check the exact hostname first. If there is no exact entry, the lookup derives wildcard suffix keys such as `*-something.example.com` from the hostname. It checks the most specific suffix first without scanning the map.

The control daemon is the only network API for configuration. It authenticates callers and forwards map reads and updates to OpenResty over the admin socket. It has no configuration state of its own.

Configuration changes do not reload nginx. OpenResty owns the live maps and persists them to `map.json`, `domains-http-map.json`, and `sni-map.json`. On restart, OpenResty loads those files before serving traffic. The external controller remains the source of truth and can resend complete maps when needed.
