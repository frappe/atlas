# Warpgate sync failing

Each minute, Atlas makes the Warpgate targets and roles match the Metal Servers, and lets Warpgate log in to each new host. For 15 minutes or more, at least one host did not sync. People cannot reach a new or renamed host through Warpgate. Access to the other hosts continues.

1. In Atlas Desk, open the Error Log list and find the newest `Warpgate target sync failed for` error. It names each host and its error. Atlas logs the same error once each hour.
2. When every host fails, check that Warpgate runs and that the Warpgate API token in Atlas Settings is valid.
3. When one host fails with `Could not reach host`, check that the host is Running and that its wg0 address answers SSH from the Atlas machine.
4. After the fix, the next sync runs within a minute, and the alert resolves.
