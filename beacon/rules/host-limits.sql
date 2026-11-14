-- title: Host near a kernel limit
-- severity: Medium
-- resource_type: Host
-- interval: 300
-- pending_for: 600
-- resolve_after: 900
-- A full conntrack table drops new connections. A full open file table stops new files and sockets. An unsynced clock breaks TLS and log order.
SELECT resource_id, alert_key, share AS value, concat(name, ' ', toString(round(share)), '% used') AS detail
FROM (
    SELECT resource_id, 'conntrack' AS alert_key, 'conntrack table' AS name,
           argMaxIf(value, ts, metric = 'system_network_conntrack_entries')
               / nullIf(argMaxIf(value, ts, metric = 'system_network_conntrack_entries_limit'), 0) * 100 AS share
    FROM datum.samples_recent
    WHERE metric IN ('system_network_conntrack_entries', 'system_network_conntrack_entries_limit') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id
    UNION ALL
    SELECT resource_id, 'open-files', 'open file table',
           argMaxIf(value, ts, metric = 'system_open_files') / nullIf(argMaxIf(value, ts, metric = 'system_open_files_limit'), 0) * 100
    FROM datum.samples_recent
    WHERE metric IN ('system_open_files', 'system_open_files_limit') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id
)
WHERE share > 80
UNION ALL
SELECT resource_id, 'clock', 0, 'the clock is not synced'
FROM datum.latest_value(metric = 'system_timex_sync_status', window = 300)
WHERE value = 0
