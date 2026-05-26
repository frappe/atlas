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


class ServerProvider(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping the DigitalOcean account endpoint."""
		account = self.client.account()
		return {"ok": True, "email": account.get("email")}

	@frappe.whitelist()
	def provision_server(self, server_name: str) -> str:
		"""Create a droplet, insert a Server row, enqueue bootstrap."""
		if frappe.db.exists("Server", server_name):
			frappe.throw(f"Server {server_name} already exists")

		droplet = self.client.create_droplet(
			name=server_name,
			region=self.default_region,
			size=self.default_size,
			image=self.default_image,
			ssh_key_ids=[self.ssh_key_id],
			tags=["atlas", server_name],
			ipv6=True,
		)

		server = frappe.get_doc({
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
			server_name=server.name,
			droplet_id=droplet["id"],
		)
		return server.name

	@property
	def client(self) -> DigitalOceanClient:
		token = get_secret("Server Provider", self.name, "api_token")
		return DigitalOceanClient(token=token)


def finish_provisioning(server_name: str, droplet_id: int) -> None:
	"""Background job: wait for droplet active, record addresses, bootstrap."""
	server = frappe.get_doc("Server", server_name)
	provider = frappe.get_doc("Server Provider", server.provider)

	droplet = provider.client.wait_for_active(droplet_id, timeout_seconds=600)
	server.ipv4_address = public_ipv4(droplet)
	server.ipv6_address, server.ipv6_prefix = public_ipv6(droplet)
	server.ipv6_virtual_machine_range = carve_virtual_machine_range(server.ipv6_prefix)
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
