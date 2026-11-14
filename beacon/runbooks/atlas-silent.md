# Atlas silent

Datum has no Atlas sample for 5 minutes. Atlas, its scheduler or the push to Datum is down. Other alerts of the region are blind.

1. Open the Atlas site. If it does not load, restart the bench processes.
2. Run `bench doctor`. If the scheduler is off, run `bench enable-scheduler`.
3. Read Error Log for `publish_metrics` failures. A 401 from Datum means the Datum token expired.
4. Check that `datum-api` runs on the regional Datum server.
