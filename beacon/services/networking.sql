-- title: Networking
-- description: The proxies pass requests to sites without errors of their own.
-- scope: Region
-- slo_target: 99.9
-- sort_order: 3
-- A proxy error is a 5xx that the proxy made itself (origin proxy). A 5xx from the site's own code is good here.
SELECT c.resource_id AS resource_id,
       toInt64(c.valid - c.bad) AS good,
       toInt64(c.valid) AS valid,
       t.last_ts AS last_ts
FROM (
    SELECT resource_id,
           sumIf(increase, labels['status_class'] = '5xx' AND labels['origin'] = 'proxy') AS bad,
           sum(increase) AS valid
    FROM datum.counter_increase(metric = 'service_proxy_requests_total', window = 90)
    GROUP BY resource_id
) AS c
INNER JOIN (
    SELECT resource_id, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'service_proxy_requests_total' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY resource_id
) AS t ON t.resource_id = c.resource_id
