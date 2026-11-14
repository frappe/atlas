# Atlas cannot reach metald

More than 20% of Atlas calls to metald on one route failed in 10 minutes. Hosts do not get changes.

1. Check the Host down and Atlas cannot sync host alerts. They show which host fails.
2. Open Metal Server in Atlas Desk and run the health check of the host.
3. On the host, run `systemctl status metald` and read `journalctl -u metald -n 200`.
4. Check the WireGuard link between Atlas and the host.
