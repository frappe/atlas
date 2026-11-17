-- title: Memory
-- resource_type: Metal Host
-- section: Memory
-- unit: bytes
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, pair.1 AS series, avg(pair.2) AS value
FROM (
    SELECT s.ts AS sample_ts,
           sumIf(s.value, s.labels['field'] = 'MemTotal') AS total,
           sumIf(s.value, s.labels['field'] = 'MemFree') AS free,
           sumIf(s.value, s.labels['field'] = 'Buffers') AS buffers,
           sumIf(s.value, s.labels['field'] = 'Cached') AS cached,
           sumIf(s.value, s.labels['field'] = 'Slab') AS slab,
           sumIf(s.value, s.labels['field'] = 'PageTables') AS page_tables,
           sumIf(s.value, s.labels['field'] = 'SwapCached') AS swap_cached,
           sumIf(s.value, s.labels['field'] = 'SwapTotal') - sumIf(s.value, s.labels['field'] = 'SwapFree') AS swap
    FROM datum.samples AS s
    WHERE s.metric = 'system_memory_info' AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts
)
ARRAY JOIN [
    ('Apps', total - free - buffers - cached - slab - page_tables - swap_cached),
    ('Page Tables', page_tables), ('Slab', slab), ('Cache', cached + swap_cached), ('Buffers', buffers),
    ('Unused', free), ('Swap', swap)
] AS pair
GROUP BY ts, series
ORDER BY ts, series
