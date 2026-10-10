# Incidents

This folder holds one report for each production or staging incident. A report states the impact, the timeline, the root cause, the fix, and the lessons.

A person files each report. An agent can suggest a report, but it must not add one unless a person asks. A filed report is immutable. Do not edit it. Record only incidents in a deployed environment, such as production or staging. Do not record local development issues.

## Reports

| Date | Environment | Incident | Status |
|---|---|---|---|
| 2026-10-10 | Production | [Host network loss from allmulticast on Broadcom NICs](2026-10-10-host-nic-receive-stall.md) | Fixed |
| 2026-09-24 | Staging | [Guest disk corruption after a restart](2026-09-24-guest-disk-corruption.md) | Fixed |

## Add a report

1. Copy the structure of the newest report.
2. Name the file `YYYY-MM-DD-short-title.md`, with the date of the incident.
3. Record the full hash of the last affected commit in the header table.
4. Reports are public. Do not include personal data or tenant data, such as people's names, email addresses, tenant hostnames, tenant IDs, and tenant IP addresses.
5. Add a row at the top of the table above.
