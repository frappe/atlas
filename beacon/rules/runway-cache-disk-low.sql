-- title: Runway cache disk space low
-- description: The Runway cache journal or spool has less than 25% free space.
-- severity: Low
-- resource_type: Virtual Machine
-- charts: runway-cache-space
-- interval: 3600
-- pending_for: 3600
-- resolve_after: 7200
SELECT resource_id, area AS alert_key, free AS value,
       concat(area, ' has ', toString(round(free, 1)), '% free') AS detail
FROM (
    SELECT resource_id, labels['area'] AS area,
           100 * argMaxIf(value, ts, metric = 'runway_cache_filesystem_available_bytes')
           / nullIf(argMaxIf(value, ts, metric = 'runway_cache_filesystem_size_bytes'), 0) AS free
    FROM datum.samples_recent
    WHERE metric IN ('runway_cache_filesystem_available_bytes', 'runway_cache_filesystem_size_bytes')
      AND labels['area'] IN ('journal', 'spool') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, area
    HAVING countIf(metric = 'runway_cache_filesystem_available_bytes') > 0
)
WHERE free < 25
