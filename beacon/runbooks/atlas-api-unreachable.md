# Atlas API is not responding

Beacon cannot read `/api/atlas/jwks.json` of this region from outside for 3 minutes. Central cannot create, start, stop or resize servers in the region. The alert detail shows the error or the status.

1. Open the URL in a browser. If DNS or TLS fails, check the DNS record and the certificate of the Atlas host.
2. If the request times out or returns 502, sign in to the Atlas host and check that the web workers and nginx run. Restart the bench processes if they do not.
3. If the API returns 5xx, follow the Atlas API errors runbook.
4. Post an update on the incident, because customers cannot change their servers.
