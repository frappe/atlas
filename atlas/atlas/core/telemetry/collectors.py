from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from ipaddress import ip_network

import frappe
import requests
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.background_jobs import get_queue, get_queue_list

MEBIBYTE = 1 << 20
# Probes run in parallel, so a slow node delays the minute's push by this much at most.
READINESS_TIMEOUT_SECONDS = 3

# Terminal records are history, not state, so they are not counted.
TERMINAL_MIGRATION_STATUSES = ("completed", "aborted", "failed")

SERVICE_DOCTYPES = {
	"Proxy Server": "proxy",
	"IPv6 Router Server": "ipv6_router",
	"Wireguard Gateway Server": "wireguard_gateway",
}


class AtlasGauges:
	"""Read the current regional state that Atlas owns, as Datum samples."""

	def __init__(self, timestamp: str) -> None:
		self.timestamp = timestamp
		self.now = now_datetime()
		self.samples: list[dict] = []

	def collect(self) -> list[dict]:
		"""Return one sample list for every area. Each query is a small aggregate."""
		self.add_virtual_machines()
		self.add_hosts()
		self.add_migrations()
		self.add_images()
		self.add_public_ips()
		self.add_operations()
		self.add_services()
		self.add_service_readiness()
		self.add_jobs()
		return self.samples

	def add(self, metric: str, value: float, **labels: str) -> None:
		"""Add one sample at the collection time."""
		self.samples.append({"metric": metric, "value": float(value), "ts": self.timestamp, "labels": labels})

	def add_counts(self, metric: str, counts: Counter, label: str) -> None:
		"""Add one sample for each counted value, labeled with that value."""
		for value, count in counts.items():
			self.add(metric, count, **{label: str(value or "unknown")})

	def add_virtual_machines(self) -> None:
		"""Count VMs by host and last reported state. Virtual Machine State caches the host reports."""
		states = frappe.get_all("Virtual Machine State", fields=["server", "status"])
		for (server, state), count in Counter(
			(row.server or "none", row.status or "unknown") for row in states
		).items():
			self.add("atlas_virtual_machines", count, server=server, state=state)

		rows = frappe.get_all("Virtual Machine", fields=["is_draft", "is_terminating", "creation"])
		drafts = [row for row in rows if row.is_draft]
		self.add("atlas_virtual_machine_drafts", len(drafts))
		self.add(
			"atlas_virtual_machine_oldest_draft_seconds", self.oldest_age([row.creation for row in drafts])
		)
		self.add("atlas_virtual_machines_terminating", sum(1 for row in rows if row.is_terminating))

	def add_hosts(self) -> None:
		"""Count hosts by status, name each host, and report its latest capacity and use."""
		servers = frappe.get_all(
			"Metal Server", fields=["name", "title", "status", "is_sleepy_vm_host", "architecture", "bmc_url"]
		)
		self.add_counts("atlas_metal_servers", Counter(server.status for server in servers), "status")
		has_bmc_driver = frappe.get_cached_doc("Atlas Settings").server_provider_controller.has_bmc_driver
		for server in servers:
			self.add(
				"atlas_metal_server_info",
				1,
				server=server.name,
				title=server.title or server.name,
				status=server.status,
				architecture=server.architecture or "",
				sleepy=str(bool(server.is_sleepy_vm_host)).lower(),
				bmc=str(has_bmc_driver and bool(server.bmc_url)).lower(),
			)

		for usage in self.latest_usages():
			server = usage.server
			self.add(
				"atlas_host_sync_age_seconds",
				(self.now - get_datetime(usage.creation)).total_seconds(),
				server=server,
			)
			self.add("atlas_host_cpu_millicores", usage.total_cpu_millicores, server=server, kind="total")
			self.add(
				"atlas_host_cpu_millicores", usage.available_cpu_millicores, server=server, kind="available"
			)
			self.add(
				"atlas_host_memory_bytes", usage.total_memory_mib * MEBIBYTE, server=server, kind="total"
			)
			self.add(
				"atlas_host_memory_bytes",
				usage.available_memory_mib * MEBIBYTE,
				server=server,
				kind="available",
			)
			self.add(
				"atlas_host_storage_bytes", usage.total_storage_mib * MEBIBYTE, server=server, kind="total"
			)
			self.add(
				"atlas_host_storage_bytes",
				usage.available_storage_mib * MEBIBYTE,
				server=server,
				kind="available",
			)
			self.add("atlas_host_virtual_machines", usage.virtual_machine_count, server=server)

	def add_migrations(self) -> None:
		"""Count active migrations by status and report the oldest one's age."""
		migrations = frappe.get_all(
			"Virtual Machine Migration",
			filters={"status": ["not in", TERMINAL_MIGRATION_STATUSES]},
			fields=["status", "started_at", "creation"],
		)
		self.add_counts("atlas_migrations_active", Counter(row.status for row in migrations), "status")
		self.add(
			"atlas_migration_oldest_active_seconds",
			self.oldest_age([row.started_at or row.creation for row in migrations]),
		)

	def add_images(self) -> None:
		"""Count images and the bytes their rootfs files hold in storage, by status. Kernels are shared."""
		images = frappe.get_all("Virtual Machine Image", fields=["status", "image_stored_size_mib"])
		self.add_counts("atlas_virtual_machine_images", Counter(row.status for row in images), "status")
		stored_mib = Counter()
		for row in images:
			stored_mib[row.status] += row.image_stored_size_mib or 0
		for status, mib in stored_mib.items():
			self.add(
				"atlas_virtual_machine_image_stored_bytes", mib * MEBIBYTE, status=str(status or "unknown")
			)

	def add_public_ips(self) -> None:
		"""Count allocations by pool and status, and how many allocations each enabled pool can hold."""
		for pool in frappe.get_all(
			"Public IP Pool",
			filters={"enabled": 1},
			fields=["name", "prefix", "version", "allocation_prefix_length"],
		):
			prefix_length = ip_network(pool.prefix, strict=False).prefixlen
			capacity = 2 ** max((pool.allocation_prefix_length or prefix_length) - prefix_length, 0)
			self.add("atlas_public_ip_capacity", capacity, pool=pool.name, version=str(pool.version))

		rows = frappe.get_all("Public IP Allocation", fields=["pool", "status", "version"])
		for (pool, status, version), count in Counter(
			(row.pool, row.status, row.version) for row in rows
		).items():
			self.add(
				"atlas_public_ip_allocations", count, pool=pool or "none", status=status, version=str(version)
			)

	def add_operations(self) -> None:
		"""Count active SSH Tasks and Metald Updates, and the Error Logs of the last minute."""
		ssh_tasks = frappe.get_all(
			"SSH Task", filters={"status": ["in", ("Pending", "Running")]}, pluck="status"
		)
		self.add_counts("atlas_ssh_tasks_active", Counter(ssh_tasks), "status")

		updates = frappe.get_all(
			"Metald Update", filters={"status": ["in", ("Planning", "Running", "Paused")]}, pluck="status"
		)
		self.add_counts("atlas_metald_updates_active", Counter(updates), "status")

		minute_ago = add_to_date(self.now, minutes=-1)
		self.add(
			"atlas_error_logs_last_minute", frappe.db.count("Error Log", {"creation": [">=", minute_ago]})
		)

	def add_services(self) -> None:
		"""Count the nodes of each service by status."""
		for doctype, service in SERVICE_DOCTYPES.items():
			statuses = Counter(frappe.get_all(doctype, fields=["status"], pluck="status"))
			for status, count in statuses.items():
				self.add("atlas_service_servers", count, service=service, status=status)

		# A region has one Cargo cluster, so Cargo Server is a Single.
		cargo_status = frappe.db.get_single_value("Cargo Server", "status")
		self.add("atlas_service_servers", 1, service="cargo", status=cargo_status or "Not Provisioned")

	def add_service_readiness(self) -> None:
		"""Name the VM of each active service node, and probe its readiness URL. The IPv6 router has none."""
		nodes = []
		for doctype, service in SERVICE_DOCTYPES.items():
			for name in frappe.get_all(doctype, filters={"status": "Active"}, pluck="name"):
				node = frappe.get_doc(doctype, name)
				url = None if service == "ipv6_router" else f"https://{node.get_domain()}/readyz"
				nodes.append((service, name, node.virtual_machine, url))

		cargo = frappe.get_single("Cargo Server")
		if cargo.status == "Active":
			nodes.append(("cargo", "cargo", cargo.virtual_machine, f"https://{cargo.domain}/api/method/ping"))

		with ThreadPoolExecutor(max_workers=8) as pool:
			readiness = list(pool.map(is_ready, [url for *_, url in nodes]))

		servers = dict(
			frappe.get_all(
				"Virtual Machine",
				filters={"name": ["in", [virtual_machine for _, _, virtual_machine, _ in nodes]]},
				fields=["name", "server"],
				as_list=True,
			)
		)
		for (service, name, virtual_machine, url), ready in zip(nodes, readiness, strict=True):
			labels = {
				"service": service,
				"node": name,
				"virtual_machine": virtual_machine or "",
				"server": servers.get(virtual_machine) or "",
			}
			self.add("atlas_service_node", 1, **labels)
			if url:
				self.add("atlas_service_ready", int(ready), **labels)

	def add_jobs(self) -> None:
		"""Report queue backlogs. A rising failed count means jobs keep failing."""
		for queue_name in get_queue_list():
			queue = get_queue(queue_name)
			self.add("atlas_job_queue_length", queue.count, queue=queue_name)
			self.add("atlas_job_failed", queue.failed_job_registry.count, queue=queue_name)
			self.add("atlas_job_started", queue.started_job_registry.count, queue=queue_name)

	def latest_usages(self) -> list:
		"""Return the newest usage sample of each host from the last hour."""
		return frappe.db.sql(
			"""
			SELECT sample.*
			FROM `tabMetal Server Usage` AS sample
			JOIN (
				SELECT server, MAX(creation) AS creation
				FROM `tabMetal Server Usage`
				WHERE creation >= %(cutoff)s
				GROUP BY server
			) AS latest ON latest.server = sample.server AND latest.creation = sample.creation
			""",
			{"cutoff": add_to_date(self.now, hours=-1)},
			as_dict=True,
		)

	def oldest_age(self, times: list[datetime | str | None]) -> float:
		"""Return the age in seconds of the oldest time, or 0."""
		ages = [(self.now - get_datetime(value)).total_seconds() for value in times if value]
		return max(ages, default=0.0)


def is_ready(url: str | None) -> bool:
	"""Return True when url answers with a success status within the timeout."""
	if not url:
		return False
	try:
		return requests.get(url, timeout=READINESS_TIMEOUT_SECONDS).ok
	except requests.RequestException:
		return False
