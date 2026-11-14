# Service nodes failed

One or more nodes of a regional service (proxy, WireGuard gateway, IPv6 router or Cargo) have the status Failed. Atlas does not probe a Failed node, so traffic can be down without a readiness alert.

1. Open the service DocType in Atlas Desk, such as Proxy Server, and filter by the status Failed.
2. Read the error of the last provisioning job of the node.
3. Retry the provisioning, or archive the node and create a new one.
