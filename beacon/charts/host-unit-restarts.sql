-- title: Unit Restarts
-- resource_type: Metal Host
-- section: Services
-- unit: restarts
-- style: Bar
-- is_key_chart: 0
-- sort_order: 20
-- The restarts of the systemd units that metald watches, as the host-systemd-units rule counts them.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, sum(increase) AS value
FROM (
    SELECT s.ts AS sample_ts, s.labels['unit'] AS series,
           s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase
    FROM datum.samples AS s
    WHERE s.metric = 'system_systemd_unit_restarts_total' AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
    WINDOW w AS (PARTITION BY s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
GROUP BY ts, series
ORDER BY ts, series
