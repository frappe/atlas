# Atlas queue backlog

100 or more jobs stayed in one Atlas queue for 10 minutes. Provisioning, VM actions and syncs are late.

1. Open RQ Job in Atlas Desk. Filter by the queue in the alert key and the status Queued and Started.
2. If no job is Started, the workers are down. Run `bench doctor` and restart the workers.
3. If one job type fills the queue, find what enqueues it. Look for a loop in recent Error Logs.
4. If the workers are busy and healthy, add workers or wait. The alert resolves after 5 minutes under 100 jobs.
