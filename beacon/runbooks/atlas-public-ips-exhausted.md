# Public IPv4 pool almost empty

A public IPv4 pool has less than 10% free addresses. New VMs that need a public IPv4 fail.

1. Open Public IP Pool in Atlas Desk and check the pool in the alert key.
2. Release addresses that are Reserved but not attached to a running VM.
3. Add a new pool or a new range from the provider.
