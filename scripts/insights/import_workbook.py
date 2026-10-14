import json
import sys

import frappe

WORKBOOK_TITLE = "Atlas"


def import_workbook(path: str) -> str:
	"""Update the Atlas workbook in place and return its name. Generated names stay, so URLs stay."""
	with open(path) as file:
		workbook = json.load(file)

	name = frappe.db.get_value("Insights Workbook", {"title": WORKBOOK_TITLE})
	target = frappe.get_doc("Insights Workbook", name) if name else frappe.new_doc("Insights Workbook")
	if not name:
		target.title = WORKBOOK_TITLE
		target.insert()

	# A saved dashboard starts a headless browser job for its preview. Skip them.
	frappe.flags.in_patch = True
	target.restore_workbook_contents(workbook, target.name, ignore_permissions=True, keep_names=True)
	frappe.db.commit()
	return target.name


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
	frappe.set_user("Administrator")
	try:
		name = import_workbook(path)
		failed = get_failed_charts(name)
	finally:
		frappe.destroy()

	print(f"Updated workbook {name}. Charts that fail to load: {len(failed)}.", *failed, sep="\n")
	sys.exit(1 if failed else 0)
