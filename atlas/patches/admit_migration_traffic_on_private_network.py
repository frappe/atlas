import frappe

from atlas.metal_server.core.host_installation import FIREWALL_INSTALL_TIMEOUT_SECONDS, HostInstallation


def execute() -> None:
	"""Queue a host firewall refresh that moves the migration ports from wg0 to the private network."""
	servers = frappe.get_all(
		"Metal Server", filters={"status": "Running", "is_provisioning_completed": 1}, pluck="name"
	)
	for server in servers:
		frappe.enqueue(
			install_host_firewall,
			queue="long",
			timeout=FIREWALL_INSTALL_TIMEOUT_SECONDS,
			job_id=f"atlas||server||firewall||{server}",
			deduplicate=True,
			enqueue_after_commit=True,
			server=server,
		)


def install_host_firewall(server: str) -> None:
	"""Install the current host firewall on one server."""
	HostInstallation(frappe.get_doc("Metal Server", server)).install_host_firewall()
