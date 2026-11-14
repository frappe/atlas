# Host down

Atlas did not sync the host for 5 minutes and metald on the host sends no metrics. The host and its VMs are probably down.

1. Ping the host and open its console in the provider panel.
2. If the host does not answer, power cycle it through the BMC.
3. After the host is back, check that its VMs run. Post an update on the incident.
