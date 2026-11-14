# Atlas API errors

More than 5% of calls to one Atlas API operation failed with a 5xx status in 10 minutes. Central and customers see failures.

1. Open Error Log in Atlas Desk. Filter by the last 15 minutes and look for the operation in the alert key.
2. If the errors started after a deploy, roll back the deploy.
3. If the errors come from metald or a service, follow the alert for that service.
4. Post an update on the incident when customers are affected.
