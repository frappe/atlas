import frappe
from frappe.model.document import Document

from atlas.atlas.digitalocean import (
	DigitalOceanClient,
	public_ipv4,
	public_ipv6,
)
from atlas.atlas.networking import carve_virtual_machine_range
from atlas.atlas.secrets import get_secret
from atlas.atlas.ssh import connection_for_server, wait_for_ssh


DIGITALOCEAN_REQUIRED = ("api_token", "ssh_key_id", "default_region", "default_size", "default_image")

# Monthly USD price per size, updated by hand when DigitalOcean publishes
# changes — same maintenance model as `default_image`. Not every size we run
# in production is here; missing entries surface as "—" in the dialog rather
# than a wrong number.
DIGITALOCEAN_MONTHLY_COST_USD = {
	"s-1vcpu-1gb": 6,
	"s-1vcpu-2gb": 12,
	"s-2vcpu-2gb": 18,
	"s-2vcpu-4gb-intel": 24,
	"s-2vcpu-4gb": 24,
	"s-4vcpu-8gb": 48,
	"c-2": 40,
	"c-4": 80,
}


class ServerProvider(Document):
	def validate(self) -> None:
		if self.provider_type == "DigitalOcean":
			missing = [field for field in DIGITALOCEAN_REQUIRED if not self.get(field)]
			if missing:
				frappe.throw(
					f"DigitalOcean providers require: {', '.join(missing)}"
				)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping the DigitalOcean account endpoint."""
		if self.provider_type != "DigitalOcean":
			frappe.throw("Test Connection is only supported for DigitalOcean providers")
		account = self.client.account()
		return {"ok": True, "email": account.get("email")}

	@frappe.whitelist()
	def preview_cost(self) -> dict:
		"""Static preview of what `Provision Server` would create.

		The cost number is from a hand-maintained dict (same policy as
		`default_image`). Returns `monthly_cost_usd: None` if the size isn't
		in the dict — the desk dialog renders that as "—" rather than guess.
		"""
		monthly_cost = (
			DIGITALOCEAN_MONTHLY_COST_USD.get(self.default_size)
			if self.provider_type == "DigitalOcean" else None
		)
		return {
			"provider_type": self.provider_type,
			"region": self.default_region,
			"size": self.default_size,
			"image": self.default_image,
			"monthly_cost_usd": monthly_cost,
			"currency": "USD",
		}

	@frappe.whitelist()
	def provision_server(
		self,
		server_name: str,
		ipv4_address: str | None = None,
		ipv6_address: str | None = None,
		ipv6_prefix: str | None = None,
		ipv6_virtual_machine_range: str | None = None,
	) -> str:
		"""Insert a Server row and enqueue bootstrap.

		On `DigitalOcean` providers, this creates a droplet first and then
		enqueues `finish_provisioning` which waits for the droplet to come up
		before writing IPs. On `Self-Managed` providers, the operator supplies
		IPv4 / IPv6 inputs — Atlas writes them straight to the Server row and
		enqueues `finish_provisioning` to bootstrap.
		"""
		if frappe.db.exists("Server", server_name):
			frappe.throw(f"Server {server_name} already exists")

		if self.provider_type == "Self-Managed":
			for label, value in [
				("ipv4_address", ipv4_address),
				("ipv6_address", ipv6_address),
				("ipv6_prefix", ipv6_prefix),
				("ipv6_virtual_machine_range", ipv6_virtual_machine_range),
			]:
				if not value:
					frappe.throw(f"Self-Managed providers require {label}")
			frappe.get_doc({
				"doctype": "Server",
				"server_name": server_name,
				"provider": self.name,
				"status": "Pending",
				"ipv4_address": ipv4_address,
				"ipv6_address": ipv6_address,
				"ipv6_prefix": ipv6_prefix,
				"ipv6_virtual_machine_range": ipv6_virtual_machine_range,
			}).insert(ignore_permissions=True)
		else:
			droplet = self.client.create_droplet(
				name=server_name,
				region=self.default_region,
				size=self.default_size,
				image=self.default_image,
				ssh_key_ids=[self.ssh_key_id],
				tags=["atlas", server_name],
				ipv6=True,
			)
			frappe.get_doc({
				"doctype": "Server",
				"server_name": server_name,
				"provider": self.name,
				"provider_resource_id": str(droplet["id"]),
				"region": self.default_region,
				"size": self.default_size,
				"status": "Pending",
			}).insert(ignore_permissions=True)

		frappe.db.commit()

		frappe.enqueue(
			"atlas.atlas.doctype.server_provider.server_provider.finish_provisioning",
			queue="long",
			timeout=1800,
			server_name=server_name,
		)
		return server_name

	@property
	def client(self) -> DigitalOceanClient:
		token = get_secret("Server Provider", self.name, "api_token")
		return DigitalOceanClient(token=token)


def finish_provisioning(server_name: str) -> None:
	"""Background job: wait for the host to be ready, then bootstrap.

	On DigitalOcean, this waits for the droplet to go active and writes the
	IPv4/IPv6 fields to the Server row. On Self-Managed, those fields were
	already populated when the row was inserted, so the worker goes straight
	to wait_for_ssh + bootstrap.
	"""
	server = frappe.get_doc("Server", server_name)
	provider = frappe.get_doc("Server Provider", server.provider)

	if provider.provider_type == "DigitalOcean":
		droplet = provider.client.wait_for_active(
			int(server.provider_resource_id), timeout_seconds=600
		)
		server.ipv4_address = public_ipv4(droplet)
		server.ipv6_address, server.ipv6_prefix = public_ipv6(droplet)
		server.ipv6_virtual_machine_range = carve_virtual_machine_range(
			server.ipv6_address, server.ipv6_prefix
		)

	server.status = "Bootstrapping"
	server.save(ignore_permissions=True)
	frappe.db.commit()

	wait_for_ssh(connection_for_server(server), timeout_seconds=300)

	try:
		server.bootstrap()
	except Exception:
		server.reload()
		server.status = "Broken"
		server.save(ignore_permissions=True)
		frappe.db.commit()
		raise

	server.reload()
	server.status = "Active"
	server.save(ignore_permissions=True)
	frappe.db.commit()
