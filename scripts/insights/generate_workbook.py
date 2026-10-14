import json

from bmc_dashboard import add_bmc_dashboard
from errors_dashboard import add_errors_dashboard
from host.dashboard import add_host_dashboard
from logs_dashboard import add_logs_dashboard
from platform_services_dashboard import add_platform_services_dashboard
from region_dashboard import add_region_dashboard
from slo_dashboard import add_slo_dashboard
from uptime_dashboard import add_uptime_dashboard
from weekly_trends_dashboard import add_weekly_trends_dashboard
from workbook import Workbook

if __name__ == "__main__":
	workbook = Workbook("Atlas")
	add_region_dashboard(workbook)
	add_host_dashboard(workbook)
	add_bmc_dashboard(workbook)
	add_platform_services_dashboard(workbook)
	add_uptime_dashboard(workbook)
	add_slo_dashboard(workbook)
	add_errors_dashboard(workbook)
	add_weekly_trends_dashboard(workbook)
	add_logs_dashboard(workbook)
	print(json.dumps(workbook.export(), indent=1))
