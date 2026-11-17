-- title: Service Nodes Not Ready
-- resource_type: Atlas
-- section: Service Nodes
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The nodes of each regional service that are not ready, as the atlas-service-nodes-failed rule reads them.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, max(not_ready) AS value
FROM (
    SELECT s.ts AS sample_ts, replaceAll(initcap(s.labels['service']), '_', ' ') AS series, countIf(s.value = 0) AS not_ready
    FROM datum.samples AS s
    WHERE s.metric = 'atlas_service_ready' AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
