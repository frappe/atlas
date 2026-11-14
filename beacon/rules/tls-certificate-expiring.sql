-- title: TLS certificate expiring
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/tls-certificate-expiring.md
-- interval: 900
-- pending_for: 900
-- resolve_after: 1800
-- Atlas renews the wildcard certificate and each metald certificate daily from 30 days before expiry. Under 21 days left means renewal fails: Medium. Under 3 days pages, because there is no time left to fix renewal calmly. An expired wildcard certificate breaks every site, and an expired metald certificate cuts Atlas off from the host.
SELECT resource_id, alert_key, days AS value,
       concat('the ', alert_key, ' certificate expires in ', toString(round(days, 1)), ' days') AS detail,
       if(days < 3, 'High', 'Medium') AS severity
FROM (
    SELECT if(labels['certificate'] = 'metald', labels['server'], 'atlas') AS resource_id, labels['certificate'] AS alert_key,
           value / 86400 AS days
    FROM datum.latest_value(metric = 'atlas_tls_certificate_expiry_seconds', window = 900)
)
WHERE days < 21
