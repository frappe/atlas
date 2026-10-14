# Dashboards

The Atlas workbook in Insights shows the health of one region from the [telemetry](telemetry.md) in Datum. It is for Atlas engineers and operators. Generate and import it with the scripts in [scripts/insights](../../scripts/insights/README.md).

| Dashboard | Answers |
| --- | --- |
| Atlas - Region | Is something wrong in the region now, and where? |
| Atlas - Host | What does one host do? Pick the host in the Host filter. |
| Atlas - Platform Services | Are the proxy, WireGuard gateway, IPv6 router, and Cargo nodes up? |
| Atlas - Uptime | Which hosts and VMs have the least uptime? |
| Atlas - SLA and SLOs | Are the SLA and the SLOs met, and how much error budget is left? |
| Atlas - Errors | Which Atlas and host errors happen, and how often? |
| Atlas - Weekly Trends | How did hosts, VMs, migrations, and images change in the last 7 days? |
| Atlas - Logs | What happened to one VM or request? |

## Atlas - Region

Read it from the top. Each table sorts its worst row first.

| Section | Shows |
| --- | --- |
| Region Health | Checks that read OK when the region is healthy, such as hosts that stopped reporting, hosts with hardware problems, failing VMs, unhandled Atlas errors, and free public IPv4 addresses. |
| SLA | VM uptime and Atlas API availability this month. |
| Platform Services, Fleet, Atlas, Signals, Capacity, Public IPs | The newest minute or hour of each value, with its trend. |
| Hosts | Each host's push age, use, allocation, and VMs. |
| Hardware | Hosts with a hardware fault: unhealthy disks, degraded RAID, ZFS errors, ECC errors, high temperatures, thermal throttles, NIC links and errors, or failed units. |
| Hosts Under Stress | Hosts with a stress signal in the last 15 minutes: CPU, memory, or IO pressure, load, RAM use, or allocation. |
| VM Actions | The 95th percentile duration of each VM action. |
| Migrations in Progress | Each migration whose newest step is not completed or aborted. |
| Recent Problems | Unhandled Atlas errors, host error lines, and failed migrations of the last day. |

## SLA and SLOs

The SLA target is 99.9% a month for VM uptime and for the Atlas API. These targets are internal. The SLO targets are in [telemetry](telemetry.md#slos).

VM uptime counts VM-minutes. Each minute, a host that reports counts its unavailable VMs. A host that stops reporting counts every VM that it last reported as wanted running, because its VMs are down with it. A minute in which no host reported is a telemetry gap. It does not count as downtime.

Host uptime is the share of minutes in which a Running host reported, from its first report. Atlas - Uptime shows host uptime and VM uptime for each day and each host. Its tables list the hosts with the least uptime this month, and up to 100 VMs below 99.9%. The VM Downtime table on Atlas - SLA and SLOs lists each VM's downtime this month from metald's log.

API availability is the share of tenant API requests without a 5xx response, times the share of minutes in which Atlas pushed telemetry.

## Platform service uptime

Each minute, Atlas probes the readiness URL of each active proxy and WireGuard gateway node (`/readyz`) and of Cargo (`/api/method/ping`). The probes run in parallel and time out after 3 seconds. A node is ready when it answers with a success status. The IPv6 router has no readiness URL, so its uptime is the uptime of its VM. A VM whose host stopped reporting for 2 minutes counts as down now. Atlas - Platform Services shows each node's uptime this month, and the Platform Services row on Atlas - Region shows the share of nodes that are ready now.
