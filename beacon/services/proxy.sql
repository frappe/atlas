-- title: Proxy
-- description: The proxies pass requests to sites and return few errors of their own.
-- scope: Region
-- slo_target: 99.9
-- sort_order: 3
-- major_outage_below: 50
-- Proxies share the traffic, so one proxy of three down gives about 67%. That is a Partial Outage. Two down is Major.
-- Only the proxy VMs that Atlas lists count, because any sender can send proxy_requests_total.
-- Each row names the proxy node, not its VM, so a firing alert on the node, such as Service node not ready, makes a silent proxy count as bad.
-- A proxy keeps its row for 30 minutes after its last sample, so Beacon can count it as silent. A silent row has its average events of a minute, all good: Beacon makes them bad only under a cause alert.
-- A proxy error is a 5xx that the proxy made itself (origin proxy). A 5xx from the site's own code is good here.
SELECT n.node AS resource_id,
       toInt64(if(c.resource_id = '', s.per_minute, c.valid - c.bad)) AS good,
       toInt64(if(c.resource_id = '', s.per_minute, c.valid)) AS valid,
       s.last_ts AS last_ts
FROM (
    SELECT h.resource_id AS resource_id,
           ceil(h.total / greatest(1, dateDiff('second', t.first_ts, t.last_ts) / 60)) AS per_minute,
           t.last_ts AS last_ts
    FROM (
        SELECT resource_id, sum(increase) AS total
        FROM datum.counter_increase(metric = 'proxy_requests_total', window = 1800)
        GROUP BY resource_id
    ) AS h
    INNER JOIN (
        SELECT resource_id, min(ts) AS first_ts, max(ts) AS last_ts
        FROM datum.samples_recent
        WHERE metric = 'proxy_requests_total' AND ts > now() - INTERVAL 30 MINUTE
        GROUP BY resource_id
    ) AS t ON t.resource_id = h.resource_id
) AS s
INNER JOIN (
    SELECT DISTINCT labels['virtual_machine'] AS virtual_machine, labels['node'] AS node
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['service'] = 'proxy' AND ts > now() - INTERVAL 15 MINUTE
) AS n ON n.virtual_machine = s.resource_id
LEFT JOIN (
    SELECT resource_id,
           sumIf(increase, labels['status_class'] = '5xx' AND labels['origin'] = 'proxy') AS bad,
           sum(increase) AS valid
    FROM datum.counter_increase(metric = 'proxy_requests_total', window = 90)
    GROUP BY resource_id
) AS c ON c.resource_id = s.resource_id
