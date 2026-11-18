# metald calls failing

Atlas calls to metald on this host fail. The host does not get VM changes. The alert key tells the case:

| Alert key | Severity | Meaning |
| --- | --- | --- |
| `<route>` | High | More than 20% of the calls on one route failed in 10 minutes. |
| `hourly:<route>` | Medium | 5 or more calls on one route failed in 1 hour. |
| `auth` | Medium | metald refused calls with 401 or 403. Atlas and metald disagree on certificates or tokens. |

1. Read the Metal Requests and Atlas Sync Age charts of the host. If all calls fail, also check the Host down and Atlas cannot sync host alerts.
2. Open Metal Server in Atlas Desk and run the health check of the host.
3. On the host, run `systemctl status metald` and read `journalctl -u metald -n 200`.
4. Check the WireGuard link between Atlas and the host.
5. For `auth`, compare the metald certificate expiry on the host with the Metal Server in Atlas Desk.
