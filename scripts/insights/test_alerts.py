import json
import subprocess
import sys
import unittest
from pathlib import Path

from alerts import P1, P1_RUNWAY, P2_PLATFORM, P2_RUNWAY, alert_sql


class TestRunwayAlerts(unittest.TestCase):
	def test_critical_checks_use_the_controller_ttl_and_recent_disk(self):
		ownership, demand, disk = P1_RUNWAY.checks
		self.assertEqual(P1_RUNWAY.cron, P1.cron)
		self.assertIn("toUnixTimestamp(now()) - max(samples.value)", ownership.sql)
		self.assertIn("HAVING value >= 60", ownership.sql)
		self.assertIn("runway_controller_no_progress_seconds", demand.sql)
		self.assertIn("HAVING value >= 300", demand.sql)
		self.assertIn("runway_cache_filesystem_available_bytes", disk.sql)
		self.assertIn("HAVING value < 10", disk.sql)

	def test_warning_checks_gate_fallback_growth_on_volume(self):
		cleanup, fallback, disk = P2_RUNWAY.checks
		self.assertEqual(P2_RUNWAY.cron, P2_PLATFORM.cron)
		self.assertIn("HAVING value >= 600", cleanup.sql)
		self.assertIn("arrayDifference", fallback.sql)
		self.assertIn("INTERVAL 1 HOUR", fallback.sql)
		self.assertIn("HAVING sum(increase) >= 5 AND value > 25", fallback.sql)
		self.assertIn("HAVING value < 25", disk.sql)

	def test_runway_alerts_export_with_the_existing_workbook_webhook_message(self):
		script = Path(__file__).with_name("generate_workbook.py")
		workbook = json.loads(subprocess.check_output([sys.executable, str(script)], text=True))
		alerts = {alert["title"]: alert for alert in workbook["alerts"]}
		for alert in (P1, P2_PLATFORM, P1_RUNWAY, P2_RUNWAY):
			item = alerts[alert.title]
			self.assertEqual(item["cron_format"], alert.cron)
			self.assertEqual(item["condition"], "q['check'] != ''")
			self.assertIn("row.host", item["message"])
			self.assertIn("row.detail", item["message"])
			self.assertEqual(workbook["queries"][item["query"]]["operations"][0]["raw_sql"], alert_sql(alert))


if __name__ == "__main__":
	unittest.main()
