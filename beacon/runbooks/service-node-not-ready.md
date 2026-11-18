# Service node not ready

A regional service node (proxy, WireGuard gateway or Cargo) does not answer its readiness URL for 3 minutes. Atlas does not probe IPv6 routers.

1. Open the node in Atlas Desk and read its status and last error.
2. Open the VM of the node. If the VM does not run, start it.
3. On the VM, check the service unit with `systemctl status`.
4. If another node of the service is ready, traffic fails over. Fix the node without hurry.
