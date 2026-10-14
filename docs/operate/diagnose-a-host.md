# Diagnose a host

Use this page when Atlas - Region or Atlas - SLA and SLOs shows a problem and you must find the host and the cause. Go from the region to one host, read its evidence in order, then fix it with the runbook of the component that owns the fault.

```text
Region Health ──> table that names the host ──> Atlas - Host ──> Atlas - Logs ──> shell on the host ──> runbook
    (what)              (where)                  (which part)       (why)            (confirm)          (fix)
```

The [dashboards](dashboards.md) page describes each dashboard. The [telemetry](telemetry.md) page lists the metrics and log fields.

## 1. Find the host

Each Region Health check points to the table that names the host.

| Check | Read next | The host is the row with |
| --- | --- | --- |
| Hosts not reporting for 2 minutes | Atlas - Region, Hosts | A high Last Push s. |
| Hosts with issues | Atlas - Region, Hosts | Issues above 0. The other columns show which problem. |
| Hosts with hardware problems | Atlas - Region, Hardware | Every listed host. The non-zero column is the fault. |
| Hosts Atlas has not synced for 5 minutes | The Metal Server list in the Atlas Desk | The oldest Metal Server Usage sample. See [Atlas cannot reach Metal](atlas.md#atlas-cannot-reach-metal). |
| Failing VMs, Oldest pending VM change | Atlas - Region, Hosts | Failing or Pending above 0. Then read its Warnings and Errors on Atlas - Logs. |
| Service nodes not ready | Atlas - Platform Services, Service Nodes | The host of the node that is not up. |
| An action or boot SLO | Atlas - Logs, VM Actions | The host of the slow actions. |

A problem in Atlas itself, such as unhandled errors or API 5xx, has no host. Start from Atlas - Errors and the Error Log in the Atlas Desk instead.

## 2. Read the host on Atlas - Host

Set the Host filter to the host title. Read the sections in this order, because each one rules out a cause of the next.

| Order | Section | A problem looks like |
| --- | --- | --- |
| 1 | Quick CPU / Mem / Disk | CPU or RAM near 100%, Storage Pool Used above 80%, Failed Units or Unhealthy Disks above 0. |
| 2 | Virtual Machines, metald | Failed VMs, pending changes that stay above 0, or a reconcile loop whose Since Last Pass s keeps growing. |
| 3 | Disk Health | A disk with Healthy 0, growing Reallocated or Pending sectors, a degraded array, or ZFS data errors. |
| 4 | Basic CPU / Mem / Net / Disk | Pressure, swap use, a network spike, or a full file system at the time of the alert. |
| 5 | Network Traffic, Systemd | Interface errors and drops, a link down, or a unit that restarts again and again. |

Move the Period filter to the time of the alert. A graph that changes at that time usually names the cause.

## 3. Read its logs on Atlas - Logs

Set the Host filter to the host title.

1. Read Warnings and Errors. metald, kernel, and unit lines are together, newest first.
2. If one VM is involved, set the VM filter. Trace shows each reconcile step and its error.
3. If one request is involved, set the Request filter to its ID. Trace follows it from the Atlas API to the applied state in metald.

To read more than the dashboard shows, query Datum directly. A host writes under its Metal Server name, not its title:

```sql
SELECT ts, service, level, message, attributes
FROM datum.logs
WHERE resource_id = '<Metal Server name>' AND level IN ('warn', 'error', 'critical') AND ts > now() - INTERVAL 1 HOUR
ORDER BY ts DESC
LIMIT 200
```

## 4. Confirm on the host

Open a shell through [Warpgate](../region/host-access.md#people-access-through-warpgate). Read first. Change nothing until you know the cause.

| Question | Command |
| --- | --- |
| Do metald and Vector run? | `systemctl status metal.service vector.service` |
| Which units failed? | `systemctl --failed` |
| What did metald log? | `journalctl -u metal.service --since "30 minutes ago" -o cat \| jq -cR 'fromjson? \| select(.level != "INFO")'` |
| What did the kernel log? | `journalctl -k -p warning --since "1 hour ago"` |
| What does one VM record say? | `jq . /var/lib/metal/machines/<VM ID>/status.json` |
| Does one VM unit run? | `systemctl status metal-vm@<VM ID>.service` |
| Is the pool healthy? | `zpool status -x` and `zpool list` |
| Is a RAID array degraded? | `cat /proc/mdstat` and `mdadm --detail /dev/<md device>` |
| Is a disk failing? | `smartctl -a /dev/<disk>` |
| Is a link or NIC failing? | `ip -br link` and `ethtool -S <interface> \| grep -iE 'err\|drop\|discard'` |
| Do WireGuard peers answer? | `wg show wg0 latest-handshakes` |
| Is WG Mesh healthy? | `atlas-wg-mesh status` |
| Is the host under load? | `uptime`, `free -h`, and `ps -eo pid,comm,%cpu,%mem --sort=-%cpu \| head` |

## 5. Fix it

Use the runbook of the component that owns the fault:

| Fault | Runbook |
| --- | --- |
| A VM, its unit, storage, snapshot, or upgrade | [Metal checks](metal.md) |
| Atlas cannot reach the host, or placement and sync | [Atlas checks](atlas.md) |
| The host sends no metrics or logs | [Check a host](telemetry.md#check-a-host) |
| Host access, WireGuard, or the host firewall | [How Atlas reaches hosts](../region/host-access.md#recovery) |
| WG Mesh forwarding | [WG Mesh operations](../networking/wg-mesh/operations.md) |
| A failing disk or DIMM | Move the VMs off the host with migration, then replace the part. |

Follow the [safe recovery rule](find-a-problem.md#safe-recovery-rule) and read the [incident reports](../incidents/) before you restart a service. Never restart `metal-vm@<VM ID>.service` by hand, because metald owns it.
