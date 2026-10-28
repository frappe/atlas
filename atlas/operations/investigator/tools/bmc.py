from __future__ import annotations

import os
import shlex
import subprocess
from typing import Annotated, Any
from urllib.parse import urljoin, urlsplit

import frappe
from frappe.flow import ToolContext

from atlas.atlas.core.server_providers.generic import RedfishClient
from atlas.operations.investigator.tools.decorators import engineer_tool, read_tool
from atlas.operations.investigator.tools.inventory import get_host_name

IPMI_TIMEOUT_SECONDS = 60
# The manager ID differs by vendor, such as Self, 1, or iDRAC.Embedded.1. Follow links from the system.
RedfishPath = Annotated[
	str,
	"An @odata.id from an earlier result. Empty reads the ComputerSystem: Links.ManagedBy is the manager"
	" with the clock and the logs, and Links.Chassis has the sensors.",
]


@read_tool(background=True)
def read_redfish_resource(
	host: Annotated[str, "Metal Server name or title."],
	path: RedfishPath = "",
	*,
	ctx: ToolContext,
) -> dict:
	"""Read one Redfish resource from the BMC of a host, such as the clock, health, or a log."""
	return get_bmc_client(host).read(path)


@engineer_tool(background=True)
def run_redfish_request(
	host: Annotated[str, "Metal Server name or title."],
	path: RedfishPath = "",
	method: Annotated[str, "HTTP method: GET, PATCH, POST, or DELETE."] = "GET",
	body: Annotated[dict[str, Any] | None, "JSON body for PATCH or POST."] = None,
	*,
	ctx: ToolContext,
) -> dict:
	"""Send one Redfish request to the BMC of a host and return the JSON response."""
	client = get_bmc_client(host)
	return client.request(method.upper(), urljoin(client.url, path), json=body)


@engineer_tool(background=True)
def run_ipmitool(
	host: Annotated[str, "Metal Server name or title."],
	arguments: Annotated[
		str, "ipmitool arguments after the connection flags, such as '-L OPERATOR -C 3 sel info'."
	],
	*,
	ctx: ToolContext,
) -> dict:
	"""Run ipmitool over lanplus against the BMC of a host. Times are in UTC.

	Some BMCs need '-L OPERATOR -C 3'. Use 'sel time set "MM/DD/YY HH:MM:SS"' to set the clock.
	"""
	server = frappe.get_doc("Metal Server", get_host_name(host))
	command = [
		"ipmitool",
		"-I",
		"lanplus",
		"-H",
		urlsplit(server.bmc_url).hostname,
		"-U",
		server.bmc_username,
	]
	result = subprocess.run(
		[*command, "-E", *shlex.split(arguments)],
		env={**os.environ, "IPMI_PASSWORD": server.get_password("bmc_password"), "LC_ALL": "C", "TZ": "UTC"},
		capture_output=True,
		text=True,
		timeout=IPMI_TIMEOUT_SECONDS,
	)
	return {"host": server.name, "exit_code": result.returncode, "output": result.stdout + result.stderr}


def get_bmc_client(host: str) -> RedfishClient:
	server = frappe.get_doc("Metal Server", get_host_name(host))
	return server.settings.server_provider_controller.get_bmc_client(server)
