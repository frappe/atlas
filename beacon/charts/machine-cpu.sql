-- title: CPU
-- resource_type: Metal Host, Virtual Machine
-- requires_metric: system_cpu_seconds_total
-- section: CPU
-- unit: %
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- Each mode's share of all CPU time. A host sends the total as cpu "all", a VM sends each CPU. Guest time is already inside user time.
-- When the resource sends "all", the query reads only it. Before 16 Nov 2026 metald also sent each of 128 CPUs, which made long windows time out.
SELECT ts, series, value
FROM (
    SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, mode AS series,
           sum(increase) * 100 / nullIf(sum(sum(increase)) OVER (PARTITION BY ts), 0) AS value
    FROM (
        SELECT s.ts AS sample_ts,
               multiIf(s.labels['mode'] = 'irq', 'IRQ', s.labels['mode'] = 'softirq', 'SoftIRQ', initcap(s.labels['mode'])) AS mode,
               s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase
        FROM datum.samples AS s
        WHERE s.metric = 'system_cpu_seconds_total'
            AND s.labels['mode'] NOT IN ('guest', 'guest_nice') AND s.resource_id = {resource_id:String}
            AND (s.labels['cpu'] = 'all' OR NOT (SELECT count() > 0 FROM datum.samples
                WHERE metric = 'system_cpu_seconds_total' AND resource_id = {resource_id:String} AND labels['cpu'] = 'all'
                    AND ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND ts < {end:DateTime64(3)}))
            AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
        WINDOW w AS (PARTITION BY s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
    )
    WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
    GROUP BY ts, series
)
WHERE series != 'Idle'
ORDER BY ts, series
