-- title: Certificate Days Left
-- resource_type: Atlas
-- section: Certificates
-- unit: days
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The days until the wildcard certificate and the first metald certificate of a Running host expire, as the tls-certificate-expiring rule reads them.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, if(s.labels['certificate'] = 'metald', 'metald, first to expire', 'Wildcard') AS series,
       min(s.value) / 86400 AS value
FROM datum.samples AS s
WHERE s.metric = 'atlas_tls_certificate_expiry_seconds' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
