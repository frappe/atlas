-- title: Disk Throughput
-- resource_type: Metal Host, Virtual Machine
-- requires_metric: system_disk_read_bytes_total
-- section: Disk
-- unit: bytes/s
-- style: Line
-- is_key_chart: 0
-- sort_order: 25
-- The rate of each device, added together.
SELECT ts, series, sum(rate) AS value
FROM (
    SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, device, sum(increase) / nullIf(sum(seconds), 0) AS rate
    FROM (
        SELECT s.ts AS sample_ts, if(s.metric = 'system_disk_read_bytes_total', 'Read', 'Write') AS series, s.labels['device'] AS device,
               s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase,
               dateDiff('millisecond', lagInFrame(s.ts, 1, s.ts) OVER w, s.ts) / 1000 AS seconds
        FROM datum.samples AS s
        WHERE s.metric IN ('system_disk_read_bytes_total', 'system_disk_written_bytes_total') AND s.resource_id = {resource_id:String}
            AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
        WINDOW w AS (PARTITION BY s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
    )
    WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
    GROUP BY ts, series, device
)
GROUP BY ts, series
ORDER BY ts, series
