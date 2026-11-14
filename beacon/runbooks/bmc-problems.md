# BMC problems

The BMC of the host reports a problem. The alert key tells which:

- `critical-events` (High): the BMC logged a critical event, such as a power supply or memory failure.
- `unreachable`, `not-read`, `unhealthy-component` (Medium).
- `warning-events` (Low).

1. Read the event in Atlas Logs for the host (source `bmc`), or in the BMC web console.
2. For a failed power supply, fan or memory module, open a hardware ticket with the provider. Set **In Maintenance** on the Metal Server when it has no redundancy left.
3. For `unreachable` or `not-read`, check the BMC network and credentials in Atlas Desk. Without a BMC, you cannot power cycle a hung host.
