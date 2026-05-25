"""Throwaway helper to dump recent task stdout/stderr."""

import frappe


def dump_for_server(server_name: str) -> None:
	tasks = frappe.get_all(
		"Task",
		filters={"server": server_name},
		fields=["name", "status", "script"],
		order_by="creation desc",
	)
	print(f"tasks for {server_name}: {tasks}")
	for record in tasks[:3]:
		doc = frappe.get_doc("Task", record.name)
		print(f"\n=== Task {doc.name} ({doc.script}) status={doc.status} ===")
		print("STDOUT (last 2000):")
		print((doc.stdout or "(none)")[-2000:])
		print("\nSTDERR (last 1000):")
		print((doc.stderr or "(none)")[-1000:])


def mark_task_failure(task_name: str, reason: str = "manually marked Failure (worker died)") -> None:
	doc = frappe.get_doc("Task", task_name)
	if doc.status != "Running":
		print(f"task {task_name} status is {doc.status}, not Running; skipping")
		return
	doc.status = "Failure"
	doc.stderr = (doc.stderr or "") + f"\n[atlas e2e] {reason}\n"
	doc.ended = frappe.utils.now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	print(f"task {task_name} -> Failure")


def list_droplets() -> None:
	from atlas.tests.e2e._shared import get_client
	client = get_client()
	for droplet in client.list_droplets_by_tag("atlas-e2e"):
		v4 = [n.get("ip_address") for n in droplet.get("networks", {}).get("v4", [])]
		print(droplet["id"], droplet["name"], droplet["status"], v4)


def archive_all_vms() -> None:
	"""Mark every Virtual Machine row Archived. Use to clean up leaked rows
	from crashed e2e runs. Does NOT touch the server-side systemd state — run
	delete-vm.sh on the host separately if the VMs are still around."""
	rows = frappe.get_all("Virtual Machine", filters={"status": ["!=", "Archived"]}, pluck="name")
	for name in rows:
		frappe.db.set_value("Virtual Machine", name, "status", "Archived")
	frappe.db.commit()
	print(f"archived {len(rows)} VM row(s)")


def rebootstrap(server_name: str) -> None:
	"""Re-run bootstrap on an Active server. Useful after a reboot wiped
	non-persistent host state (nftables tables, sysctls)."""
	server = frappe.get_doc("Server", server_name)
	task = server.bootstrap()
	print(f"bootstrap task: {task}")


def dump_recent_tasks(limit: int = 5) -> None:
	tasks = frappe.get_all(
		"Task",
		fields=["name", "status", "script", "server"],
		order_by="creation desc",
		limit_page_length=limit,
	)
	print(tasks)
	for record in tasks:
		doc = frappe.get_doc("Task", record.name)
		print(f"\n=== Task {doc.name} ({doc.script}) status={doc.status} server={doc.server} ===")
		print("STDOUT (last 2000):")
		print((doc.stdout or "(none)")[-2000:])
		print("\nSTDERR (last 1000):")
		print((doc.stderr or "(none)")[-1000:])
