import json
import os
import sys

import frappe

WORKBOOK_TITLE = "Atlas"


def import_workbook(path: str) -> str:
	"""Update the Atlas workbook in place and return its name. Generated names stay, so URLs stay."""
	workbook = frappe.get_file_json(path)
	alerts = workbook.pop("alerts")
	name = frappe.db.get_value("Insights Workbook", {"title": WORKBOOK_TITLE})
	target = frappe.get_doc("Insights Workbook", name) if name else frappe.new_doc("Insights Workbook")
	if not name:
		target.title = WORKBOOK_TITLE
		target.insert()

	# A saved dashboard starts a headless browser job for its preview. Skip them.
	frappe.flags.in_patch = True
	target.restore_workbook_contents(workbook, target.name, ignore_permissions=True, keep_names=True)
	share_with_organization(target.name)
	import_alerts(alerts)
	frappe.db.commit()  # nosemgrep
	return target.name


def import_alerts(alerts: list[dict]) -> None:
	"""Create or update each alert. A webhook from the environment enables them; without one, each keeps its webhook and state."""
	url = os.environ.get("ATLAS_INSIGHTS_ALERT_WEBHOOK_URL")
	for alert in alerts:
		name = frappe.db.get_value("Insights Alert", {"query": alert["query"]})
		target = frappe.get_doc("Insights Alert", name) if name else frappe.new_doc("Insights Alert")
		target.update({**alert, "frequency": "Cron", "channel": "Webhook", "custom_condition": 1})
		if url:
			target.webhook_url = url
			target.webhook_token = os.environ["ATLAS_INSIGHTS_ALERT_WEBHOOK_TOKEN"]
			target.disabled = 0
		elif not name:
			target.disabled = 1
		target.save(ignore_permissions=True)


def share_with_organization(workbook: str) -> None:
	"""Let everyone in the organization edit the workbook, as Insights' share dialog does."""
	from insights.utils import DocShare

	share = DocShare.get_or_create_doc(share_doctype="Insights Workbook", share_name=workbook, everyone=1)
	share.read = 1
	share.write = 1
	share.notify_by_email = 0
	share.save(ignore_permissions=True)


def get_failed_charts(workbook: str) -> list[str]:
	"""Load every chart on every dashboard once, and return the ones that fail."""
	# Insights reads site config on import, so it loads after frappe.init.
	from insights.api import view

	failed = []
	dashboards = frappe.get_all(
		"Insights Dashboard v3", filters={"workbook": workbook}, fields=["name", "title", "items"]
	)
	for dashboard in dashboards:
		charts = {item["chart"] for item in json.loads(dashboard["items"]) if item.get("type") == "chart"}
		for chart in sorted(charts):
			try:
				view.get_chart_data(chart=chart, dashboard=dashboard.name)
			except Exception as error:
				failed.append(f"{dashboard.title} | {chart} | {repr(error)[:300]}")
	return failed


if __name__ == "__main__":
	site, path = sys.argv[1:3]
	frappe.init(site)
	frappe.connect()
	try:
		name = import_workbook(path)
		failed = get_failed_charts(name)
	finally:
		frappe.destroy()

	print(f"Updated workbook {name}. Charts that fail to load: {len(failed)}.", *failed, sep="\n")
	sys.exit(1 if failed else 0)
