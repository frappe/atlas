-- title: Runway cache fallback rate high
-- description: More than 25% of at least five cache routing decisions in the last hour fell back.
-- severity: Medium
-- resource_type: Atlas
-- charts: runway-cache-routing
-- interval: 3600
-- pending_for: 3600
-- resolve_after: 7200
SELECT 'atlas' AS resource_id, pool_key AS alert_key, fallback_percent AS value,
       concat(pool_key, ' fell back on ', toString(round(fallback_percent, 1)), '% of ',
              toString(decisions), ' routing decisions in 1 h') AS detail
FROM (
    SELECT concat(labels['region'], '/', labels['pool']) AS pool_key, sum(increase) AS decisions,
           100 * sumIf(increase, fallback) / nullIf(decisions, 0) AS fallback_percent
    FROM (
        SELECT labels, increase, true AS fallback
        FROM datum.counter_increase(metric = 'runway_cache_fallback_total', window = 3600)
        UNION ALL
        SELECT labels, increase, false AS fallback
        FROM datum.counter_increase(metric = 'runway_cache_routed_total', window = 3600)
    )
    GROUP BY pool_key
)
WHERE decisions >= 5 AND fallback_percent > 25
