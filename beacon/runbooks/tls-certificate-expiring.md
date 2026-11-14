# TLS certificate expiring

A certificate that Atlas renews expires soon. Atlas tries to renew each one daily from 30 days before expiry, so less than 21 days left means renewal fails (Medium). Less than 3 days left pages (High). The alert key names the certificate:

- `wildcard`: the regional wildcard certificate that the proxies serve. When it expires, every site in the region fails TLS.
- `metald` on a host: the certificate that Atlas uses to talk to metald. When it expires, Atlas cannot manage the host.

1. In Atlas Desk, open the Error Log list and look for renewal errors from the daily jobs `renew_expiring_wildcard_certificate` and `renew_expiring_tls_certificates`.
2. For `wildcard`, open Atlas Settings. Check that wildcard auto renew is on and that the DNS provider credentials work, because Let's Encrypt checks DNS.
3. For `metald`, open the Metal Server and check that it is Running and that its last metald job did not fail. Then queue the renewal again.
4. After the renewal, check that the expiry date moved.
