"""`services` — everything that isn't cluster management: sites, the merged
pilot console, proxy / tcp-proxy / front-door, TLS and DNS issuance, subdomain
routing, custom domains, and the customer VPN gateway.

`services` MAY import `core`; the reverse is forbidden (enforced by the
readability lint gate). Services registers its post-lifecycle handlers on
`core.callbacks` at boot so core can invoke them by name without knowing what
they do.
"""
