# Atlas Insights workbook

Generate and import the Atlas workbook for [Insights](https://github.com/frappe/insights). It reads regional Datum data in ClickHouse. See [dashboards](../../docs/operate/dashboards.md) for operation.

| File | Owns |
| --- | --- |
| `workbook.py` | `Workbook`, `Folder`, `Layout`, chart config helpers, shared SQL, and number formats |
| `generate_workbook.py` | The entry point. Prints the workbook as JSON. |
| `*_dashboard.py`, `region_tables.py` | Dashboard queries and layouts |
| `uptime_queries.py` | Uptime SQL and charts that the Uptime, SLA and SLOs, and Platform Services dashboards share |
| `host/` | Host dashboard. Each area file lists its sections as `Chart` objects. |
| `service_nodes_dashboard.py` | Atlas - Service Nodes: Vector metrics of proxy, WireGuard gateway, and IPv6 router nodes |
| `alerts.py` | The P1, P2 Hosts, P2 Platform, and P3 alert queries and their Insights Alerts. See [alerts](../../docs/operate/dashboards.md#alerts). |
| `update_workbook.py` | Finds the bench of the Insights site, gets these scripts, generates the workbook, and runs `import_workbook.py` |
| `import_workbook.py` | Runs in the bench: updates the workbook and its alerts, then loads every chart once |

## Update the workbook

Run this on the server of the Insights site, as the user that owns the bench. The site needs a ClickHouse data source for the Datum database, named `datum` unless you pass `--data-source`.

```sh
curl -fsSL https://raw.githubusercontent.com/frappe/atlas/develop/scripts/insights/update_workbook.py | python3 - <insights site>
```

The script finds the bench that has the site under the Pilot `benches` folder, or uses `--bench`, and downloads these scripts from the Atlas `develop` branch. It prints each chart that fails to load and exits with an error when one fails.

| Option | Use |
| --- | --- |
| `--ref <branch, tag, or commit>` | Take the scripts from another Atlas version. |
| `--default-host <host title>` | The host that Atlas - Host opens with. |
| `--skip-chart-check` | Do not load every chart after the update. Use it when the Datum ClickHouse cannot take one query for each chart in a row. |
| `--data-source <name>` | The Insights data source for the Datum database. The default is `datum`. |
| `--bench <folder>` | Use this bench, such as `~/frappe-bench` on a server without Pilot. |
| `--source <folder>` | Use a local `scripts/insights` folder, such as a checkout with changes. |
| `--alert-webhook-url <https URL>` | Send the alerts to this webhook, such as a Raven incoming webhook, and enable them. |

The update changes the workbook named Atlas in place and lets everyone in the organization edit it. Each query, chart, and dashboard keeps its generated name, so a dashboard URL, such as `/insights/dashboards/dashboard-atlas-uptime`, stays the same. The update overwrites edits made in Insights and deletes members that the scripts no longer make. Change the scripts instead.

## Add a chart

Add it in the dashboard file that owns it, with `folder.add_chart(key, title, sql, chart_type, config, info)`. The key names the query `q-<key>` and the chart `c-<key>`. Keep a key after its first import, so links to the chart keep working.
