# Dashboards

The Atlas workbook in Insights shows the health of one region from the [telemetry](telemetry.md) in Datum. It is for Atlas engineers and operators. Generate and import it with the scripts in [scripts/insights](../../scripts/insights/README.md).

Host charts and tables read only the Metal servers that Atlas lists in `atlas_metal_server_info`. Other resources, such as Pilot servers, push to the same Datum and do not show.

| Dashboard | Answers |
| --- | --- |
| Atlas - Region | Is something wrong in the region now, and where? |
| Atlas - Host | What does one host do? Pick the host in the Host filter. |
| Atlas - Host BMC | What hardware health and events does one host's [BMC](telemetry.md#bmc-hardware) report? |
| Atlas - Platform Services | Are the proxy, WireGuard gateway, IPv6 router, and Cargo nodes up? |
| Atlas - Service Nodes | How busy is each proxy, WireGuard gateway, and IPv6 router node, and how does proxy traffic look? Pick nodes in the Node filter. |
| Atlas - Uptime | Which hosts and VMs have the least uptime? |
| Atlas - SLA and SLOs | Are the SLA and the SLOs met, and how much error budget is left? |
| Atlas - Errors | Which Atlas and host errors happen, and how often? |
| Atlas - Weekly Trends | How did hosts, VMs, migrations, and images change in the last 7 days? |
| Atlas - Logs | What happened to one VM or request? |

## Atlas - Region

Read it from the top. Each table sorts its worst row first.

| Section | Shows |
| --- | --- |
| Region Health | Checks for stale telemetry, hardware problems, failing VMs, unhandled Atlas errors, and free public IPv4 addresses. Healthy checks show OK. |
| SLA | VM uptime and Atlas API availability this month. |
| Platform Services, Fleet, Atlas, Signals, Capacity, Public IPs | The newest minute or hour of each value, with its trend. |
| Hosts | Each host's push age, use, allocation, and VMs. |
| Hardware Faults from Host | Host-reported faults: unhealthy disks, degraded RAID, ZFS errors, ECC errors, high temperatures, thermal throttles, NIC faults, or failed units. |
| Hardware Faults from BMC | Unreachable BMCs, no read for 15 minutes, unhealthy components, or critical events in the last day. Read Atlas - Host BMC for details. |
| Hosts Under Stress | Hosts with a stress signal in the last 15 minutes: CPU, memory, or IO pressure, load, RAM use, or allocation. |
| VM Actions | The 95th percentile duration of each VM action. |
| Migrations in Progress | Each migration whose newest step is not completed or aborted. |
| Recent Problems | Unhandled Atlas errors, host error lines, BMC events, and failed migrations of the last day. |

Region Health alerts if Atlas sends no metrics for 2 minutes, a BMC is unreachable, or no BMC read arrives for 15 minutes. **Hosts with hardware problems** counts distinct hosts with host-reported faults, unhealthy BMC components, or critical BMC events.

## SLA and SLOs

The SLA target is 99.9% a month for VM uptime and for the Atlas API. These targets are internal. The SLO targets are in [telemetry](telemetry.md#slos).

VM uptime counts VM-minutes. Each minute, a host that reports counts its unavailable VMs. A host that stops reporting counts every VM that it last reported as wanted running, because its VMs are down with it.

A minute in which no host reported is a telemetry gap. It does not count as downtime.

Host uptime is the share of minutes in which a Running host reported, from its first report.

Atlas - Uptime shows host uptime and VM uptime for each day and each host. Its tables list the hosts with the least uptime this month, and up to 100 VMs below 99.9%. The VM Downtime table on Atlas - SLA and SLOs lists each VM's downtime this month from metald's log.

API availability is the share of tenant API requests without a 5xx response, times the share of minutes in which Atlas pushed telemetry.

## Platform service uptime

Each minute, Atlas probes the readiness URL of each active proxy and WireGuard gateway node (`/readyz`) and of Cargo (`/api/method/ping`). The probes run in parallel and time out after 3 seconds. A node is ready when it answers with a success status.

The IPv6 router has no readiness URL, so its uptime is the uptime of its VM. A VM whose host stopped reporting for 2 minutes counts as down now.

Atlas - Platform Services shows each node's uptime this month. The Platform Services row on Atlas - Region shows the share of nodes that are ready now.

## Alerts

The workbook has 4 Insights Alerts. Each alert runs one query from the Alerts folder and posts the failing checks to a webhook as Markdown. The checks and their limits are in [alerts.py](../../scripts/insights/alerts.py).

| Alert | Runs | Checks |
| --- | --- | --- |
| 🔴 Atlas P1 Critical | Every 5 minutes | Hosts, service nodes, or Atlas not reporting, host sync, unavailable or failing VMs, storage at risk, host, service node, or Atlas disk under 10% free, stalled reconcilers, stuck VM changes, service nodes not ready, uncorrected ECC errors, critical BMC events, API 5xx |
| 🟠 Atlas P2 Hosts | Every hour | Host disk under 25% free, hardware faults, host stress, failed units, clock sync, BMC problems, BMC warning events, repeated host and service node errors, dropped telemetry, RAM allocation |
| 🟠 Atlas P2 Platform | Every hour | Atlas or service node disk under 25% free, service node memory and OOM kills, proxy 5xx over 5%, new or bursting Atlas errors, migrations, jobs, Metal calls, free IPv4 addresses |
| 🔵 Atlas P3 Report | Every 6 hours | SLA and SLO budgets, fleet allocation, pool use, metald version drift, BMC temperature and clock, scrub age, disk wear, old VM drafts |

P1 and P2 check only hosts that Atlas reports as Running, and only active service nodes. A service node that never sent telemetry does not alert as not reporting. P2 is two alerts, because one query is over the Insights SQL size limit. An alert posts when its query returns a row, and posts again on each run while the problem stays.

P1 shows critical BMC events of the last 15 minutes, so one event can post up to 3 times. P2 posts each BMC warning event once.

P3 always posts. Its first line is a heartbeat with the number of hosts that report, so a missing report means that telemetry or alerts are broken.

A new alert stays disabled until the update sets a webhook:

```sh
ATLAS_INSIGHTS_ALERT_WEBHOOK_TOKEN=<token> python3 update_workbook.py <insights site> --alert-webhook-url https://<receiver>
```

Insights sends the token as `Authorization: Bearer <token>`. It posts only to a public `https` URL. A later update without `--alert-webhook-url` keeps the webhook and the enabled state of each alert.
