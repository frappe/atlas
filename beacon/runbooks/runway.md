# Runway runner pools and cache

For an overdue ownership renewal, check the controller logs for its region and pool. Confirm that the controller can reach Central and that the pool is enabled. Check controller health and its Datum forwarding if scrapes stopped.

For waiting demand, check the controller logs for VM provisioning, guest setup, and GitHub runner registration errors. Check Central for the pool's current runner lease and its phase.

For low journal or spool space, run `df -h` on the cache VM and identify the affected mount. Check the Runway cache logs for journal or spool growth. Increase the affected filesystem's capacity if required.
